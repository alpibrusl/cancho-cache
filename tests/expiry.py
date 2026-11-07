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


others = {}
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
    # The same with the commands sent in bursts, so that a burst is one turn of the loop and one millisecond: the stamps that say "used longest
    # ago" must still tell the keys of a burst apart (docs/design.md section 27). With the stamp a millisecond, `hot` was lost in 82 runs of 200.
    e3 = Client(LPORT)
    e3.ask("SET", "hot", "alive")
    burst = []
    for i in range(20000):
        burst.append(cmd("SET", "y:%d" % i, big))
        if i % 10 == 9:
            burst.append(cmd("GET", "hot"))
        if len(burst) >= 200:
            e3.s.sendall(b"".join(burst))
            burst = []
    if burst:
        e3.s.sendall(b"".join(burst))
    for _ in range(20000 + 2000):
        e3.read()
    check("a key used all the time survives 20,000 writes that evict, sent in bursts", e3.ask("GET", "hot"), b"alive")
    # --- the other policies (docs/design.md section 28), each on a 1 MiB arena written to far past its size
    def churn(x, prefix, n, *options):
        for i in range(n):
            x.s.sendall(cmd("SET", "%s:%d" % (prefix, i), big, *options))
        refused = 0
        for _ in range(n):
            rep = x.read()
            if isinstance(rep, tuple) and rep[0] == "-":
                refused += 1
        return refused

    for k, policy in enumerate(("volatile-lru", "volatile-random", "volatile-ttl", "allkeys-random")):
        port = 6410 + k
        others[policy] = (start([CACHE, str(port), "1", "100000", policy], port), port)
    for policy, (proc, port) in others.items():
        x = Client(port)
        check(policy + " is what CONFIG GET says", x.ask("CONFIG", "GET", "maxmemory-policy"), [b"maxmemory-policy", policy.encode()])
        if policy.startswith("volatile"):
            # 300 keys that never run out, and then far more written that do: the 300 must all survive, and no write is refused.
            check(policy + ": 300 keys with no expiry are written", churn(x, "keep", 300), 0)
            check(policy + ": writes of keys with an expiry are not refused while volatile keys can go", churn(x, "life", 6000, "EX", "100000"), 0)
            kept = sum(x.ask("EXISTS", "keep:%d" % i) for i in range(300))
            check(policy + ": no key without an expiry was taken", kept, 300)
            check_range(policy + ": the store holds about a MiB", x.ask("DBSIZE"), 500, 1100)
            # With nothing that has an expiry to take, a full arena refuses, as with noeviction.
            y = Client(port)
            y.ask("FLUSHALL")
            check(policy + ": filling the arena with keys that have no expiry", churn(y, "fill", 1500) > 0, True)
            check(policy + ": a write that needs room is refused with -OOM when no key has an expiry", y.ask("SET", "more", big)[0], "-")
        else:
            check(policy + ": no write is refused", churn(x, "r", 6000), 0)
            check_range(policy + ": the store holds about a MiB", x.ask("DBSIZE"), 500, 1100)
    # volatile-ttl takes the key whose expiry is soonest: of 300 keys with a short life and 300 with a long one, written first, the short ones go first.
    x = Client(others["volatile-ttl"][1])
    x.ask("FLUSHALL")
    churn(x, "short", 300, "EX", "100")
    churn(x, "long", 300, "EX", "100000")
    churn(x, "newer", 800, "EX", "50000")
    short = sum(x.ask("EXISTS", "short:%d" % i) for i in range(300))
    long_ = sum(x.ask("EXISTS", "long:%d" % i) for i in range(300))
    check("volatile-ttl: the keys that expire soonest are the ones taken (short %d left, long %d left)" % (short, long_), short < long_, True)
    check("volatile-ttl: most of the short-lived are gone, most of the long-lived are there", (short < 100, long_ > 200), (True, True))
    # CONFIG SET changes the policy while the cache runs: a full noeviction cache refuses, and takes writes after allkeys-lru is set.
    port = 6420
    full = start([CACHE, str(port), "1", "100000", "noeviction"], port)
    try:
        x = Client(port)
        check("noeviction: a full arena refuses", churn(x, "n", 3000) > 0, True)
        check("CONFIG SET maxmemory-policy allkeys-lru", x.ask("CONFIG", "SET", "maxmemory-policy", "allkeys-lru"), ("+", b"OK"))
        check("after it, writes are taken and old keys go", (churn(x, "m", 3000), x.ask("EXISTS", "n:0")), (0, 0))
        check("CONFIG SET maxmemory-samples 10", (x.ask("CONFIG", "SET", "maxmemory-samples", "10"), x.ask("CONFIG", "GET", "maxmemory-samples")), (("+", b"OK"), [b"maxmemory-samples", b"10"]))
        check("the setting is in INFO", b"maxmemory_policy:allkeys-lru" in b"".join(x.ask("INFO", "memory").split(b"\n")) if isinstance(x.ask("INFO", "memory"), bytes) else False, True)
    finally:
        full.terminate()
finally:
    for p in [redis, cache, lru] + [v[0] for v in others.values()]:
        p.terminate()

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("expiry, the sweep, PTTL, and eviction under every policy behave as stated")
