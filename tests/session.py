#!/usr/bin/env python3
"""The connection and server commands, and real client libraries.

    python3 tests/session.py [path/to/cache]

What a byte-for-byte comparison with Redis cannot say (the values differ: ids, uptimes, the server's own counters), checked against
stated expectations:
  * `COMMAND COUNT` equals the length of `COMMAND LIST`, and every listed command is answered (none is "unknown command");
  * `INFO` has Redis's shape and its numbers move when they should; `INFO <section>` is only that section;
  * `HELLO 2` describes the server and carries the id `CLIENT ID` gives; ids are unique and increasing; `CLIENT SETNAME` is per connection;
  * `QUIT` answers OK and then closes; commands after it are not answered;
and, if the libraries are installed (`pip install redis`; `npm install ioredis` in tests/clients), a real client of each connects, says what it says
on connect, and gets through a short session. A library that is not installed is reported as skipped, not as passed.
"""
import os, re, shutil, socket, subprocess, sys, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 6490
failures, skipped = [], []


def cmd(*args):
    out = b"*%d\r\n" % len(args)
    for a in args:
        a = a.encode() if isinstance(a, str) else a
        out += b"$%d\r\n%s\r\n" % (len(a), a)
    return out


class Client:
    def __init__(self):
        self.s = socket.create_connection(("127.0.0.1", PORT))
        self.s.settimeout(3)
        self.buf = b""

    def _fill(self):
        d = self.s.recv(65536)
        if not d:
            raise EOFError
        self.buf += d

    def _line(self):
        while b"\r\n" not in self.buf:
            self._fill()
        i = self.buf.index(b"\r\n")
        line, self.buf = self.buf[:i], self.buf[i + 2:]
        return line

    def ask(self, *args):
        self.s.sendall(cmd(*args))
        return self.read()

    def read(self):
        line = self._line()
        t, rest = line[:1], line[1:]
        if t == b"+":
            return ("+", rest)
        if t == b"-":
            return ("-", rest)
        if t == b":":
            return int(rest)
        if t == b"$":
            n = int(rest)
            if n < 0:
                return None
            while len(self.buf) < n + 2:
                self._fill()
            v, end, self.buf = self.buf[:n], self.buf[n:n + 2], self.buf[n + 2:]
            if end != b"\r\n":
                raise SystemExit("a bulk string of %d bytes did not end in CRLF but in %r" % (n, end))
            return v
        if t == b"*":
            return [self.read() for _ in range(int(rest))]
        raise SystemExit("bad reply %r" % line)


def check(what, cond, detail=""):
    if not cond:
        failures.append("%s %s" % (what, detail))


def info_fields(text):
    out, section = {}, None
    for line in text.decode().split("\r\n"):
        if line.startswith("# "):
            section = line[2:]
        elif ":" in line:
            k, v = line.split(":", 1)
            out[(section, k)] = v
    return out


p = subprocess.Popen([CACHE, str(PORT), "64", "100000", "allkeys-lru"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", PORT), 0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    c = Client()

    # --- COMMAND COUNT / LIST, and that every listed command is answered
    names = [x.decode() for x in c.ask("COMMAND", "LIST")]
    check("COMMAND COUNT equals the length of COMMAND LIST", c.ask("COMMAND", "COUNT") == len(names), "(%r vs %d)" % (c.ask("COMMAND", "COUNT"), len(names)))
    check("COMMAND LIST has no repeats", len(set(names)) == len(names))
    for n in names:
        probe = Client()
        try:
            r = probe.ask(n)
        except EOFError:
            r = ("+", b"closed")
        bad = isinstance(r, tuple) and r[0] == "-" and r[1].startswith(b"ERR unknown command")
        check("listed command %s is answered" % n, not bad, repr(r))
        probe.s.close()
    check("a command that is not listed is unknown", c.ask("NOSUCHCOMMAND")[1].startswith(b"ERR unknown command"))

    # --- INFO
    text = c.ask("INFO")
    f = info_fields(text)
    for section in ("Server", "Clients", "Memory", "Stats", "Keyspace"):
        check("INFO has a # %s section" % section, ("# " + section).encode() in text)
    check("INFO says what it is", f.get(("Server", "server_name")) == "cancho-cache" and f.get(("Server", "redis_version"), "").startswith("7."))
    check("INFO knows its port", f.get(("Server", "tcp_port")) == str(PORT))
    check("INFO counts this connection", int(f[("Clients", "connected_clients")]) >= 1)
    check("INFO shows the policy", f.get(("Memory", "maxmemory_policy")) == "allkeys-lru")
    before = int(f[("Stats", "total_commands_processed")])
    c.ask("SET", "info:a", "1")
    c.ask("SET", "info:b", "2", "EX", "100")
    f2 = info_fields(c.ask("INFO"))
    check("total_commands_processed moves", int(f2[("Stats", "total_commands_processed")]) >= before + 3, repr((before, f2[("Stats", "total_commands_processed")])))
    check("used_memory grows with data", int(f2[("Memory", "used_memory")]) > int(f[("Memory", "used_memory")]))
    check("keyspace counts keys and expiries", f2.get(("Keyspace", "db0"), "").startswith("keys=") and "expires=1" in f2.get(("Keyspace", "db0"), ""), repr(f2.get(("Keyspace", "db0"))))
    only = c.ask("INFO", "memory")
    check("INFO memory is only that section", b"# Memory" in only and b"# Server" not in only and b"# Clients" not in only)
    check("INFO of an unknown section is empty", c.ask("INFO", "nonsense") == b"")
    check("INFO too many arguments", c.ask("INFO", "a", "b")[1] == b"ERR syntax error")
    c.ask("DEL", "info:a", "info:b")

    # Replies that follow a long one must start exactly where it ends, whatever the scratch buffer held before: INFO and CONFIG GET are written
    # in place and moved, so a count that is one short leaves a stale byte at the end that only a following reply exposes.
    for noise in (b"x" * 600, b"y" * 3000):
        z = Client()
        z.s.sendall(cmd("ECHO", noise) + cmd("INFO") + cmd("PING") + cmd("CONFIG", "GET", "*") + cmd("PING", "after") + cmd("INFO", "stats") + cmd("ECHO", "end"))
        got = [z.read() for _ in range(7)]
        check("ECHO, INFO, PING, CONFIG GET, PING, INFO stats, ECHO in one write: every reply intact", got[0] == noise and got[1].endswith(b"\r\n\r\n") and got[2] == ("+", b"PONG") and len(got[3]) == 20 and got[4] == b"after" and got[5].startswith(b"# Stats") and got[6] == b"end", repr([g if not isinstance(g, bytes) else g[:20] for g in got]))
        z.s.close()

    # --- HELLO, CLIENT
    h = c.ask("HELLO", "2")
    flat = dict(zip([x for x in h[0::2]], h[1::2]))
    check("HELLO 2 describes the server", flat.get(b"server") == b"redis" and flat.get(b"proto") == 2 and flat.get(b"mode") == b"standalone" and flat.get(b"role") == b"master" and flat.get(b"modules") == [], repr(flat))
    check("HELLO's id is CLIENT ID", flat.get(b"id") == c.ask("CLIENT", "ID"))
    ids = []
    for _ in range(5):
        x = Client()
        ids.append(x.ask("CLIENT", "ID"))
        x.s.close()
    check("client ids are unique and increasing", ids == sorted(set(ids)) and len(ids) == 5, repr(ids))
    a, b = Client(), Client()
    a.ask("CLIENT", "SETNAME", "alpha")
    check("a client name is per connection", (a.ask("CLIENT", "GETNAME"), b.ask("CLIENT", "GETNAME")) == (b"alpha", None))
    check("HELLO SETNAME sets it", (b.ask("HELLO", "2", "SETNAME", "beta") is not None, b.ask("CLIENT", "GETNAME")) == (True, b"beta"))
    check("RESET forgets it", (b.ask("RESET"), b.ask("CLIENT", "GETNAME")) == (("+", b"RESET"), None))
    check("a name of 64 bytes is kept", a.ask("CLIENT", "SETNAME", "n" * 64) == ("+", b"OK") and a.ask("CLIENT", "GETNAME") == b"n" * 64)
    check("a longer name is refused, saying why", a.ask("CLIENT", "SETNAME", "n" * 65)[1].startswith(b"ERR client name too long"))
    check("the previous name survives a refusal", a.ask("CLIENT", "GETNAME") == b"n" * 64)

    # --- CONFIG
    check("CONFIG GET maxmemory is the arena", c.ask("CONFIG", "GET", "maxmemory") == [b"maxmemory", b"67108864"])
    check("CONFIG GET maxmemory-policy", c.ask("CONFIG", "GET", "maxmemory-policy") == [b"maxmemory-policy", b"allkeys-lru"])
    allc = c.ask("CONFIG", "GET", "*")
    check("CONFIG GET * lists the ten settings once each", len(allc) == 20 and len(set(allc[0::2])) == 10, repr(allc)[:200])
    two = c.ask("CONFIG", "GET", "save", "appendonly")
    check("CONFIG GET of two settings (Redis's order is its hash seed's, so only the pairs are compared)", sorted(zip(two[0::2], two[1::2])) == [(b"appendonly", b"no"), (b"save", b"")], repr(two))
    check("CONFIG GET port", c.ask("CONFIG", "GET", "port") == [b"port", str(PORT).encode()])

    # --- QUIT closes
    q = Client()
    q.s.sendall(cmd("PING") + cmd("QUIT") + cmd("PING"))
    got = b""
    try:
        while True:
            d = q.s.recv(1000)
            if not d:
                break
            got += d
    except socket.timeout:
        failures.append("QUIT did not close the connection")
    check("QUIT answers OK, closes, and answers nothing after it", got == b"+PONG\r\n+OK\r\n", repr(got))

    # --- real client libraries
    try:
        import redis
    except ImportError:
        redis = None
        skipped.append("redis-py (pip install redis)")
    if redis is not None:
        for proto in (2, 3):
            r = redis.Redis(port=PORT, decode_responses=True, protocol=proto)
            tag = "redis-py RESP%d" % proto
            check(tag + ": ping/set/get/incr", (r.ping(), r.set("rp:a", "1"), r.get("rp:a"), r.incr("rp:a")) == (True, True, "1", 2))
            check(tag + ": a missing key is None, mget keeps the holes", (r.get("rp:none"), r.mget("rp:a", "rp:none")) == (None, ["2", None]))
            check(tag + ": config_get is a dict", r.config_get("save") == {"save": ""}, repr(r.config_get("save")))
            r.close()
        r = redis.Redis(port=PORT, decode_responses=True)
        check("redis-py (default protocol): ping/set/get/incr", (r.ping(), r.set("rp:a", "1"), r.get("rp:a"), r.incr("rp:a")) == (True, True, "1", 2))
        check("redis-py: pipeline(transaction=False), mget, expire, ttl (a transaction=True pipeline sends MULTI/EXEC, which this server does not have)", (lambda pl: (pl.set("rp:b", "x").set("rp:c", "y").mget("rp:b", "rp:c").expire("rp:b", 100).execute()))(r.pipeline(transaction=False)) == [True, True, ["x", "y"], True])
        check("redis-py: ttl, setex, getdel", (r.ttl("rp:b") in (99, 100), r.setex("rp:d", 100, "z"), r.getdel("rp:d")) == (True, True, "z"))
        check("redis-py: client_setname/getname, config_get, info", (r.client_setname("py"), r.client_getname(), r.config_get("maxmemory-policy"), r.info("memory")["maxmemory_policy"]) == (True, "py", {"maxmemory-policy": "allkeys-lru"}, "allkeys-lru"))
    node = shutil.which("node")
    for lib, module, script, ready in (("ioredis", "ioredis", "ioredis_session.js", "ioredis ok"),
                                      ("node-redis", "redis", "node_redis_session.js", "node-redis ok")):
        modules = os.path.join(HERE, "clients", "node_modules", module)
        if node and os.path.isdir(modules):
            run = subprocess.run([node, os.path.join(HERE, "clients", script), str(PORT)], capture_output=True, text=True, timeout=30)
            check(lib + ": becomes ready and gets through a session", run.returncode == 0 and ready in run.stdout, run.stdout + run.stderr)
        else:
            skipped.append(lib + " (npm install " + module + " in tests/clients)")
    rs_bin = os.path.join(HERE, "clients", "redis-rs", "target", "release", "redis-rs-session")
    if os.path.isfile(rs_bin):
        run = subprocess.run([rs_bin, str(PORT)], capture_output=True, text=True, timeout=30)
        check("redis-rs: becomes ready and gets through a session", run.returncode == 0 and "redis-rs ok" in run.stdout, run.stdout + run.stderr)
    else:
        skipped.append("redis-rs (cargo build --release in tests/clients/redis-rs)")
finally:
    p.terminate()

for s in skipped:
    print("SKIPPED", s)
if failures:
    print("\n".join(failures))
    sys.exit(1)
print("the session commands behave as stated; clients that ran: %s" % ("redis-py" if "redis-py (pip install redis)" not in skipped else "none of redis-py") + (", ioredis" if not any(x.startswith("ioredis") for x in skipped) else "") + (", node-redis" if not any(x.startswith("node-redis") for x in skipped) else "") + (", redis-rs" if not any(x.startswith("redis-rs") for x in skipped) else ""))
