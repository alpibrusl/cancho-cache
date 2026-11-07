#!/usr/bin/env python3
"""SLOWLOG against Redis (docs/design.md section 31).

    python3 tests/slowlog.py [path/to/cache]        (needs redis-server, Redis 7.0)

The same commands go to Redis and to the cache with `slowlog-log-slower-than` 0, and the entries of `SLOWLOG GET` are compared: the arguments (cut, counted and
redacted as Redis does), the ids, the order, the client name, and the replies of `LEN`, `RESET`, `GET` with every count and every error. The time, the duration and the
client's port are machine matters and are only checked to be of the right kind.
"""
import os, socket, subprocess, sys, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
RPORT, CPORT = 6941, 6942
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
        self.s.settimeout(10)
        self.buf = b""

    def _line(self):
        while b"\r\n" not in self.buf:
            self.buf += self.s.recv(1 << 20)
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def _val(self):
        line = self._line()
        t, rest = line[:1], line[1:]
        if t == b"$":
            n = int(rest)
            if n < 0:
                return None
            while len(self.buf) < n + 2:
                self.buf += self.s.recv(1 << 20)
            v, self.buf = self.buf[:n], self.buf[n + 2:]
            return v
        if t == b"*":
            return [self._val() for _ in range(int(rest))]
        if t == b":":
            return int(rest)
        return (t.decode(), rest.decode())

    def ask(self, *args):
        self.s.sendall(cmd(*args))
        return self._val()


def check(what, got, want):
    if got != want:
        failures.append("%s: got %r, wanted %r" % (what, got, want))


def pairs(flat):
    return dict(zip(flat[0::2], flat[1::2]))


def entries(c, *count):
    """SLOWLOG GET with time, duration and port taken out (and checked to be what they should)"""
    got = c.ask("SLOWLOG", "GET", *count)
    out = []
    if not isinstance(got, list):
        return got
    for e in got:
        check("entry has six fields", len(e), 6)
        check("time is a recent unix time", abs(e[1] - time.time()) < 60, True)
        check("duration is not negative", e[2] >= 0, True)
        addr = e[4].decode()
        check("address is host:port", addr.startswith("127.0.0.1:") and addr[10:].isdigit(), True)
        out.append([e[0], e[3], e[5]])
    return out


redis = start(["redis-server", "--port", str(RPORT), "--save", "", "--appendonly", "no", "--protected-mode", "no"], RPORT)
cache = start([CACHE, str(CPORT)], CPORT)
try:
    r, c = Client(RPORT), Client(CPORT)
    for x in (r, c):
        check("default threshold is 10000 and the log is 128 long", pairs(x.ask("CONFIG", "GET", "slowlog-*")), {b"slowlog-log-slower-than": b"10000", b"slowlog-max-len": b"128"})
        x.ask("CONFIG", "SET", "slowlog-log-slower-than", "0")
        x.ask("SLOWLOG", "RESET")
    both = lambda *a: (r.ask(*a), c.ask(*a))
    runs = [("SET", "a", "1"), ("GET", "a"), ("GET", "nope"), ("MGET", "a", "b"), ("NOSUCH",), ("INCR", "a", "x"), ("SET", "big", "v" * 300), ("CLIENT", "SETNAME", "me"),
            ("SET", "k", "v", "EX", "100"), ("AUTH", "secret"), ("AUTH", "u", "secret"), ("HELLO", "2", "AUTH", "u", "pw"), ("HELLO", "2", "SETNAME", "n"), ("PING",)]
    runs.append(("MSET",) + tuple(v for i in range(20) for v in ("k%d" % i, "v%d" % i)))
    runs.append(("DEL",) + tuple("d%d" % i for i in range(40)))
    runs.append((("GET", "x" * 200)))
    for op in runs:
        a, b = both(*op)
    check("entries after the commands", entries(c), entries(r))
    for n in ("0", "1", "3", "-1", "1000"):
        check("SLOWLOG GET %s" % n, entries(c, n), entries(r, n))
    for bad in (("SLOWLOG", "GET", "-2"), ("SLOWLOG", "GET", "x"), ("SLOWLOG", "GET", "1", "2"), ("SLOWLOG", "LEN", "1"), ("SLOWLOG", "RESET", "x"), ("SLOWLOG", "BOGUS"), ("SLOWLOG",)):
        a, b = both(*bad)
        check("%s" % (bad,), b, a)
    check("SLOWLOG LEN", c.ask("SLOWLOG", "LEN"), r.ask("SLOWLOG", "LEN"))
    check("RESET", both("SLOWLOG", "RESET"), (("+", "OK"), ("+", "OK")))
    # after RESET the ids go on and the log holds the RESET itself only if it is slow (threshold 0: it is)
    check("after RESET", entries(c), entries(r))
    # a threshold that nothing reaches logs nothing; a negative one switches the log off
    for x in (r, c):
        x.ask("CONFIG", "SET", "slowlog-log-slower-than", "10000000")
        x.ask("SLOWLOG", "RESET")
        x.ask("SET", "q", "1")
    check("nothing is slow", (r.ask("SLOWLOG", "LEN"), c.ask("SLOWLOG", "LEN")), (0, 0))
    for x in (r, c):
        x.ask("CONFIG", "SET", "slowlog-log-slower-than", "-1")
        x.ask("SET", "q", "1")
    check("negative threshold: off", (r.ask("SLOWLOG", "LEN"), c.ask("SLOWLOG", "LEN")), (0, 0))
    # the ring keeps the newest max-len
    for x in (r, c):
        x.ask("CONFIG", "SET", "slowlog-log-slower-than", "0")
        x.ask("CONFIG", "SET", "slowlog-max-len", "5")
        x.ask("SLOWLOG", "RESET")
        for i in range(12):
            x.ask("SET", "r%d" % i, "v")
    check("ring of 5", entries(c), entries(r))
    check("CONFIG GET after", pairs(c.ask("CONFIG", "GET", "slowlog-*")), pairs(r.ask("CONFIG", "GET", "slowlog-*")))
    # EXEC logs the commands inside
    for x in (r, c):
        x.ask("SLOWLOG", "RESET")
        x.ask("MULTI"); x.ask("SET", "t", "1"); x.ask("GET", "t"); x.ask("EXEC")
    check("MULTI/EXEC", entries(c), entries(r))
finally:
    for p in (redis, cache):
        p.terminate()

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("SLOWLOG entries, counts and errors agree with Redis")
