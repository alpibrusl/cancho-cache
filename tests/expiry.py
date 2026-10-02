#!/usr/bin/env python3
"""Behaviour that depends on time, and on memory running out: what a byte-for-byte comparison cannot say.

    python3 tests/expiry.py [path/to/cache]

Against Redis as the oracle where Redis has an answer; against stated expectations where the cache is deliberately
not Redis (it has no calendar, and its eviction samples five keys like Redis's but is not Redis's).
  * a key set with a short life is there, then is not: asked for (lazy) and left alone (active, by the sweep);
  * PTTL counts down, within a range, and TTL rounds as Redis does;
  * under `allkeys-lru` a full arena never refuses a write, the store stays near its size, and a key that is used
    all the time survives while the ones written once are what go.
"""
import os, socket, subprocess, sys, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
RPORT, CPORT, LPORT = 6401, 6402, 6403
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
        self.s.settimeout(3)
        self.buf = b""

    def _fill(self):
        d = self.s.recv(65536)
        if not d:
            raise SystemExit("connection closed")
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
        if t in b"+-":
            return (t.decode(), rest)
        if t == b":":
            return int(rest)
        if t == b"$":
            n = int(rest)
            if n < 0:
                return None
            while len(self.buf) < n + 2:
                self._fill()
            v, self.buf = self.buf[:n], self.buf[n + 2:]
            return v
        if t == b"*":
            return [self.read() for _ in range(int(rest))]
        raise SystemExit("bad reply %r" % line)


def check(what, got, want):
    if got != want:
        failures.append("%s: got %r, wanted %r" % (what, got, want))


def check_range(what, got, lo, hi):
    if not (isinstance(got, int) and lo <= got <= hi):
        failures.append("%s: got %r, wanted %d..%d" % (what, got, lo, hi))


redis = start(["redis-server", "--port", str(RPORT), "--save", "", "--appendonly", "no", "--protected-mode", "no"], RPORT)
cache = start([CACHE, str(CPORT)], CPORT)
lru = start([CACHE, str(LPORT), "1", "100000", "allkeys-lru"], LPORT)
try:
    r, c = Client(RPORT), Client(CPORT)
    # --- a short life, asked for
    for name, x in (("redis", r), ("cache", c)):
        check(name + " SET PX", x.ask("SET", "t:a", "v", "PX", "300"), ("+", b"OK"))
        check(name + " there at once", x.ask("GET", "t:a"), b"v")
        check_range(name + " PTTL counts down", x.ask("PTTL", "t:a"), 200, 300)
        check(name + " TTL rounds up to a second", x.ask("TTL", "t:a"), 0)
        check(name + " a longer life: TTL", (x.ask("SET", "t:b", "v", "EX", "100"), x.ask("TTL", "t:b")), (("+", b"OK"), 100))
        check_range(name + " PTTL of a long life", x.ask("PTTL", "t:b"), 99000, 100000)
    time.sleep(0.45)
    for name, x in (("redis", r), ("cache", c)):
        check(name + " gone after its time (asked for)", x.ask("GET", "t:a"), None)
        check(name + " and so is EXISTS", x.ask("EXISTS", "t:a"), 0)
        check(name + " the long one is still there", x.ask("GET", "t:b"), b"v")
    # --- a short life, left alone: the sweep must take it
    for name, x in (("redis", r), ("cache", c)):
        for i in range(2000):
            x.s.sendall(cmd("SET", "s:%d" % i, "v", "PX", "150"))
        for i in range(2000):
            x.read()
    time.sleep(1.5)
    check("the sweep took 2,000 keys nobody asked for again (cache: DBSIZE counts the long-lived t:b only)", c.ask("DBSIZE"), 1)
    # Redis's own active expiry gets there too, which says the comparison is fair.
    check("redis agrees about t:b and the swept keys", r.ask("DBSIZE") <= 3, True)
    # --- GT/LT/NX/XX against a moving clock
    for name, x in (("redis", r), ("cache", c)):
        x.ask("SET", "t:c", "v")
        check(name + " EXPIRE PX then PERSIST", (x.ask("PEXPIRE", "t:c", "100000"), x.ask("PERSIST", "t:c"), x.ask("TTL", "t:c")), (1, 1, -1))
    # --- eviction: a 1 MiB arena that is written to far past its size
    e = Client(LPORT)
    big = b"v" * 1000
    for i in range(20000):
        e.s.sendall(cmd("SET", "w:%d" % i, big))
        if i % 100 == 99:
            e.s.sendall(cmd("GET", "hot"))
    refused = 0
    # (the replies: SET gives OK; GET of `hot` before it exists gives a null)
    e2 = Client(LPORT)
    e.s.settimeout(5)
    n_replies = 20000 + 200
    for _ in range(n_replies):
        rep = e.read()
        if isinstance(rep, tuple) and rep[0] == "-":
            refused += 1
    check("no write is refused under allkeys-lru", refused, 0)
    size = e2.ask("DBSIZE")
    check_range("the store holds about a MiB of 1,000-byte values, not 20 MB", size, 500, 1100)
    # a key that is used all the time survives while the rest churn
    e2.ask("SET", "hot", "alive")
    for i in range(20000):
        e2.s.sendall(cmd("SET", "x:%d" % i, big))
        if i % 10 == 9:
            e2.s.sendall(cmd("GET", "hot"))
    for _ in range(20000 + 2000):
        e2.read()
    check("a key used all the time survives 20,000 writes that evict", e2.ask("GET", "hot"), b"alive")
    check("the oldest of the churn is gone and the newest is there", (e2.ask("EXISTS", "x:0"), e2.ask("EXISTS", "x:19999")), (0, 1))
finally:
    for p in (redis, cache, lru):
        p.terminate()

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("expiry, the sweep, PTTL, and eviction under allkeys-lru behave as stated")
