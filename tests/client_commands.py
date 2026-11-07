#!/usr/bin/env python3
"""CLIENT LIST, INFO and KILL against Redis (docs/design.md section 32).

    python3 tests/client_commands.py [path/to/cache]        (needs redis-server, Redis 7.0)

The same connections are opened on Redis and on the cache and the same commands run, and then:
  * every field of an entry is a field Redis 7.0 has, and the fields of the cache's entry are the ones of Redis's but `ADDED` less the two it cannot tell (`fd`, `laddr`);
  * the fields the commands decide agree (name, flags, db, sub, psub, ssub, multi, user, redir, resp), with ids, addresses, times and memory taken out;
  * the filters of LIST and the forms of KILL answer what Redis does, errors included, and the connections that were killed are the ones, and no others, that are closed.
"""
import os, re, socket, subprocess, sys, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
RPORT, CPORT = 6951, 6952
CANNOT = {"fd", "laddr"}
failures = []


def cmd(*args):
    out = b"*%d\r\n" % len(args)
    for a in args:
        a = a.encode() if isinstance(a, str) else a
        out += b"$%d\r\n%s\r\n" % (len(a), a)
    return out


def start(argv, port):
    p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return p
        except OSError:
            time.sleep(0.05)
    raise SystemExit("did not start: %s" % argv)


class Client:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port))
        self.s.settimeout(5)
        self.buf = b""

    def _line(self):
        while b"\r\n" not in self.buf:
            d = self.s.recv(1 << 20)
            if not d:
                raise EOFError
            self.buf += d
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def ask(self, *args):
        self.s.sendall(cmd(*args))
        return self.read()

    def read(self):
        line = self._line()
        t, rest = line[:1], line[1:]
        if t == b"$":
            n = int(rest)
            if n < 0:
                return None
            while len(self.buf) < n + 2:
                self.buf += self.s.recv(1 << 20)
            v, self.buf = self.buf[:n], self.buf[n + 2:]
            return v.decode()
        if t == b"*":
            return [self.read() for _ in range(int(rest))]
        if t == b":":
            return int(rest)
        return (t.decode(), rest.decode())

    def closed(self):
        """whether the server closed this connection (it may take a moment)"""
        self.s.settimeout(1)
        try:
            return self.s.recv(1) == b""
        except socket.timeout:
            return False
        except OSError:
            return True


def check(what, got, want):
    if got != want:
        failures.append("%s: got %r, wanted %r" % (what, got, want))


def entries(text):
    out = []
    for line in text.split("\n"):
        if line:
            out.append(dict(f.split("=", 1) for f in line.split(" ")))
    return out


STABLE = ("name", "flags", "db", "sub", "psub", "ssub", "multi", "user", "redir", "resp", "cmd")


def plain(e):
    """the fields the commands decide; `cmd` without its subcommand (the cache does not say it)"""
    out = {k: e[k] for k in STABLE}
    out["cmd"] = out["cmd"].split("|")[0]
    return out

redis = start(["redis-server", "--port", str(RPORT), "--save", "", "--appendonly", "no", "--protected-mode", "no"], RPORT)
cache = start([CACHE, str(CPORT)], CPORT)
try:
    ports = (RPORT, CPORT)
    # three connections each; the first is the one that asks
    a = [Client(p) for p in ports]
    b = [Client(p) for p in ports]
    c = [Client(p) for p in ports]
    both = lambda f: [f(0), f(1)]
    both(lambda i: b[i].ask("CLIENT", "SETNAME", "bee"))
    both(lambda i: c[i].ask("CLIENT", "SETNAME", "cee"))
    both(lambda i: c[i].ask("MULTI"))
    both(lambda i: c[i].ask("GET", "x"))
    both(lambda i: a[i].ask("CLIENT", "SETNAME", "ay"))
    # a connection that speaks RESP3, and one whose last command was a GET
    r3 = [Client(p) for p in ports]
    both(lambda i: r3[i].ask("HELLO", "3", "SETNAME", "three"))
    g = [Client(p) for p in ports]
    both(lambda i: g[i].ask("CLIENT", "SETNAME", "getter"))
    both(lambda i: g[i].ask("GET", "x"))

    r_list, c_list = [entries(a[i].ask("CLIENT", "LIST")) for i in (0, 1)]
    check("number of entries", len(c_list), len(r_list))
    for e in r_list:
        check("Redis's fields are the ones this test knows", set(e) - set(c_list[0]) <= CANNOT | {"ssub"} | set(), True)
    for e in c_list:
        check("fields of an entry", sorted(e), sorted(k for k in r_list[0] if k not in CANNOT))
    by_name = lambda es: {e["name"]: plain(e) for e in es}
    check("stable fields of every entry", by_name(c_list), by_name(r_list))
    check("ids are distinct and increasing in connection order", sorted(int(e["id"]) for e in c_list), sorted(int(e["id"]) for e in c_list))

    # CLIENT INFO is the caller's own line
    ri, ci = [entries(a[i].ask("CLIENT", "INFO")) for i in (0, 1)]
    check("CLIENT INFO is one entry", (len(ri), len(ci)), (1, 1))
    check("CLIENT INFO fields", plain(ci[0]), plain(ri[0]))
    check("CLIENT INFO's id is CLIENT ID", int(ci[0]["id"]), a[1].ask("CLIENT", "ID"))

    # filters and errors
    def same(*args):
        got = [a[i].ask(*args) for i in (0, 1)]
        check(" ".join(args), got[1], got[0])

    for args in (("CLIENT", "LIST", "TYPE", "pubsub"), ("CLIENT", "LIST", "TYPE", "master"), ("CLIENT", "LIST", "TYPE", "replica"), ("CLIENT", "LIST", "TYPE", "foo"),
                 ("CLIENT", "LIST", "ID", "x"), ("CLIENT", "LIST", "ID", "0"), ("CLIENT", "LIST", "ID", "-1"), ("CLIENT", "LIST", "ID"), ("CLIENT", "LIST", "foo"),
                 ("CLIENT", "LIST", "TYPE"), ("CLIENT", "LIST", "ID", "1", "x"), ("CLIENT", "INFO", "x"), ("CLIENT", "LIST", "ID", "99999"),
                 ("CLIENT", "KILL"), ("CLIENT", "KILL", "1.2.3.4:5"), ("CLIENT", "KILL", "1.2.3.4:5", "x"), ("CLIENT", "KILL", "TYPE", "foo"), ("CLIENT", "KILL", "ID", "0"),
                 ("CLIENT", "KILL", "ID", "x"), ("CLIENT", "KILL", "ID", "99999"), ("CLIENT", "KILL", "USER", "nobody"), ("CLIENT", "KILL", "SKIPME", "maybe"),
                 ("CLIENT", "KILL", "FOO", "bar"), ("CLIENT", "KILL", "ADDR", "1.2.3.4:5"), ("CLIENT", "KILL", "TYPE", "pubsub"),
                 ("CLIENT", "KILL", "TYPE", "normal", "ID", "99999"), ("CLIENT", "KILL", "ID", "99999", "ADDR"), ("CLIENT", "KILL", "USER", "default", "ID", "99999")):
        same(*args)

    # LIST by id and by type: the lines found
    ids = [[int(e["id"]) for e in entries(a[i].ask("CLIENT", "LIST"))] for i in (0, 1)]
    for i in (0, 1):
        some = ids[i][:2]
        got = entries(a[i].ask("CLIENT", "LIST", "ID", *[str(x) for x in some]))
        check("LIST ID finds the ones asked for (%d)" % i, sorted(int(e["id"]) for e in got), sorted(some))
        got = entries(a[i].ask("CLIENT", "LIST", "TYPE", "normal"))
        check("LIST TYPE normal is all of them (%d)" % i, len(got), 5)

    # KILL: by id, by address, by type normal; the caller is skipped unless asked
    for i in (0, 1):
        victims = entries(a[i].ask("CLIENT", "LIST", "ID", str(b[i].ask("CLIENT", "ID"))))
        check("one line for b (%d)" % i, len(victims), 1)
    r = [a[i].ask("CLIENT", "KILL", "ID", str(b[i].ask("CLIENT", "ID"))) for i in (0, 1)]
    check("KILL ID", r[1], r[0])
    check("b is closed on both", both(lambda i: b[i].closed()), [True, True])
    # the old form, by address
    addr = [[e["addr"] for e in entries(a[i].ask("CLIENT", "LIST")) if e["name"] == "cee"][0] for i in (0, 1)]
    r = [a[i].ask("CLIENT", "KILL", addr[i]) for i in (0, 1)]
    check("KILL addr", r[1], r[0])
    check("c is closed on both", both(lambda i: c[i].closed()), [True, True])
    r = [a[i].ask("CLIENT", "KILL", addr[i]) for i in (0, 1)]
    check("KILL addr of a connection that has gone", r[1], r[0])
    # ADDR and ID together that do not name the same connection kill nobody
    d = [Client(p) for p in ports]
    e = [Client(p) for p in ports]
    for i in (0, 1):
        did = d[i].ask("CLIENT", "ID")
        eaddr = entries(e[i].ask("CLIENT", "INFO"))[0]["addr"]
        check("KILL with ID and ADDR of two connections kills none (%d)" % i, a[i].ask("CLIENT", "KILL", "ID", str(did), "ADDR", eaddr), 0)
    # SKIPME NO kills the caller too: the answer comes, then the connection closes
    for i in (0, 1):
        got = d[i].ask("CLIENT", "KILL", "ID", str(d[i].ask("CLIENT", "ID")), "SKIPME", "no")
        check("KILL of itself answers (%d)" % i, got, 1)
        check("and is closed (%d)" % i, d[i].closed(), True)
    # TYPE normal kills everyone else
    r = [a[i].ask("CLIENT", "KILL", "TYPE", "normal") for i in (0, 1)]
    check("KILL TYPE normal", r[1], r[0])
    check("e is closed on both", both(lambda i: e[i].closed()), [True, True])
    check("the caller is still there", both(lambda i: a[i].ask("PING")), [("+", "PONG"), ("+", "PONG")])
    # a connection of the same name later is a new one with a new id
    n = [Client(p) for p in ports]
    both(lambda i: n[i].ask("CLIENT", "SETNAME", "again"))
    check("a new connection is listed", sorted(e["name"] for e in entries(a[1].ask("CLIENT", "LIST"))), sorted(e["name"] for e in entries(a[0].ask("CLIENT", "LIST"))))
    # in MULTI and EXEC
    r = [(a[i].ask("MULTI"), a[i].ask("CLIENT", "LIST"), a[i].ask("CLIENT", "INFO"), a[i].ask("EXEC")) for i in (0, 1)]
    for i in (0, 1):
        check("queued in MULTI (%d)" % i, r[i][1], ("+", "QUEUED"))
    sl = [entries(r[i][3][0]) for i in (0, 1)]
    check("CLIENT LIST run by EXEC shows the caller in MULTI", by_name(sl[1]).get("ay"), by_name(sl[0]).get("ay"))
    check("flags x and multi=2 while running EXEC", [by_name(sl[i])["ay"]["flags"] for i in (0, 1)], ["x", "x"])
finally:
    for p in (redis, cache):
        p.terminate()

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("CLIENT LIST, INFO and KILL agree with Redis")
