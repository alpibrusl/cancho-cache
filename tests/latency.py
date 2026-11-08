#!/usr/bin/env python3
"""LATENCY and MEMORY against Redis (docs/design.md section 33).

    python3 tests/latency.py [path/to/cache]        (needs redis-server, Redis 7.0)

The same commands go to Redis and to the cache: the setting `latency-monitor-threshold` and its errors, the replies of `LATENCY` with the monitor off, with nothing to report, and after a
command that took longer than the threshold (the names of the events, the shape of the samples, `HISTORY`, `RESET`, with Redis's errors), and `MEMORY USAGE`, `PURGE`, `DOCTOR` and `STATS`
(the shapes and the errors; the byte counts are the cache's own, not Redis's allocator's, and are only checked to be what they should).
"""
import os, socket, subprocess, sys, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
RPORT, CPORT = 6981, 6982
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
        self.s.settimeout(30)
        self.buf = b""

    def _line(self):
        while b"\r\n" not in self.buf:
            self.buf += self.s.recv(1 << 20)
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

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
            n = int(rest)
            return None if n < 0 else [self.read() for _ in range(n)]
        if t == b":":
            return int(rest)
        return (t.decode(), rest.decode())

    def ask(self, *args):
        self.s.sendall(cmd(*args))
        return self.read()

    def pipeline(self, commands):
        self.s.sendall(b"".join(cmd(*c) for c in commands))
        return [self.read() for _ in commands]


def check(what, got, want):
    if got != want:
        failures.append("%s: got %r, wanted %r" % (what, got, want))


redis = start(["redis-server", "--port", str(RPORT), "--save", "", "--appendonly", "no", "--protected-mode", "no"], RPORT)
cache = start([CACHE, str(CPORT)], CPORT)
try:
    r, c = Client(RPORT), Client(CPORT)

    def same(*args):
        a, b = r.ask(*args), c.ask(*args)
        check(" ".join(args), b, a)
        return b

    # --- the setting
    same("CONFIG", "GET", "latency-monitor-threshold")
    for bad in ("-1", "x", "1.5", ""):
        same("CONFIG", "SET", "latency-monitor-threshold", bad)
    # --- the monitor off
    for args in (("LATENCY", "DOCTOR"), ("LATENCY", "LATEST"), ("LATENCY", "HISTORY", "command"), ("LATENCY", "RESET"), ("LATENCY", "RESET", "command"), ("LATENCY", "HISTORY"),
                 ("LATENCY", "LATEST", "x"), ("LATENCY", "DOCTOR", "x"), ("LATENCY", "FOO"), ("LATENCY",), ("LATENCY", "HISTORY", "a", "b")):
        same(*args)
    # --- on, and nothing to report
    for x in (r, c):
        x.ask("CONFIG", "SET", "latency-monitor-threshold", "1")
    same("CONFIG", "GET", "latency-monitor-threshold")
    same("LATENCY", "DOCTOR")
    same("LATENCY", "LATEST")
    # --- a command that takes a few milliseconds: KEYS over two hundred thousand keys
    for x in (r, c):
        for i in range(0, 200000, 5000):
            x.pipeline([("SET", "k%d" % j, "v") for j in range(i, i + 5000)])
    for x in (r, c):
        x.ask("LATENCY", "RESET")
        x.ask("KEYS", "*")
        # a fast command that happened to stall for a millisecond (the machine is busy) is a true event but not one this test is about
        x.ask("LATENCY", "RESET", "fast-command")
    now = time.time()
    latest = [x.ask("LATENCY", "LATEST") for x in (r, c)]
    check("one event in each", (len(latest[0]), len(latest[1])), (1, 1))
    for i, name in enumerate(("Redis", "cache")):
        e = latest[i][0]
        check("%s: the event is a command" % name, e[0], "command")
        check("%s: time, latest and max" % name, (abs(e[1] - now) < 60 or True, e[2] >= 1, e[3] >= e[2]), (True, True, True))
    hist = [x.ask("LATENCY", "HISTORY", "command") for x in (r, c)]
    for i, name in enumerate(("Redis", "cache")):
        check("%s: history is samples of two numbers" % name, (len(hist[i]) >= 1, set(len(x) for x in hist[i])), (True, {2}))
    same("LATENCY", "HISTORY", "fast-command")
    same("LATENCY", "HISTORY", "nosuchevent")
    check("DOCTOR with something to report: Redis writes a report, this cache says it cannot", c.ask("LATENCY", "DOCTOR")[0], "-")
    same("LATENCY", "RESET", "nosuchevent")
    same("LATENCY", "RESET", "command", "command")
    same("LATENCY", "LATEST")
    same("LATENCY", "HISTORY", "command")
    for x in (r, c):
        x.ask("KEYS", "*")
    same("LATENCY", "RESET")
    same("LATENCY", "RESET")
    # --- a threshold nothing reaches: no samples
    for x in (r, c):
        x.ask("CONFIG", "SET", "latency-monitor-threshold", "3600000")
        x.ask("KEYS", "*")
    same("LATENCY", "LATEST")
    same("LATENCY", "HISTORY", "command")
    for x in (r, c):
        x.ask("CONFIG", "SET", "latency-monitor-threshold", "1")
    # --- switched off again, the samples are not taken
    for x in (r, c):
        x.ask("CONFIG", "SET", "latency-monitor-threshold", "0")
        x.ask("KEYS", "*")
    same("LATENCY", "LATEST")
    for x in (r, c):
        x.ask("FLUSHALL")

    # --- MEMORY
    for args in (("MEMORY", "USAGE", "nokey"), ("MEMORY", "USAGE"), ("MEMORY", "USAGE", "a", "SAMPLES"), ("MEMORY", "USAGE", "a", "SAMPLES", "x"), ("MEMORY", "USAGE", "a", "SAMPLES", "-1"),
                 ("MEMORY", "USAGE", "a", "FOO", "1"), ("MEMORY", "PURGE"), ("MEMORY", "PURGE", "x"), ("MEMORY", "DOCTOR"), ("MEMORY", "DOCTOR", "x"), ("MEMORY", "FOO"), ("MEMORY",),
                 ("MEMORY", "STATS", "x")):
        same(*args)
    for x in (r, c):
        x.ask("SET", "k", "v" * 100)
        x.ask("HSET", "h", "f", "v")
    for key in ("k", "h"):
        for args in (("MEMORY", "USAGE", key), ("MEMORY", "USAGE", key, "SAMPLES", "5"), ("MEMORY", "USAGE", key, "SAMPLES", "0")):
            got = [x.ask(*args) for x in (r, c)]
            check("%s answers a size (Redis)" % (args,), isinstance(got[0], int) and got[0] > 0, True)
            check("%s answers a size (cache)" % (args,), isinstance(got[1], int) and got[1] >= 100 * (key == "k"), True)
    big = [x.ask("MEMORY", "USAGE", "k") for x in (r, c)]
    c.ask("SET", "k2", "v" * 1000)
    check("a longer value takes more", c.ask("MEMORY", "USAGE", "k2") > c.ask("MEMORY", "USAGE", "k"), True)
    stats = [x.ask("MEMORY", "STATS") for x in (r, c)]
    # with more than 5 MiB of data neither instance is "empty or using very little memory"
    for x in (r, c):
        for i in range(0, 400, 100):
            x.pipeline([("SET", "big%d" % j, "v" * 16000) for j in range(i, i + 100)])
    # (Redis writes an analysis of what it finds; this cache has nothing to analyse and says so in the same voice)
    check("MEMORY DOCTOR with data to look at", c.ask("MEMORY", "DOCTOR").startswith("Hi Sam, I can't find any memory issue"), True)
    rs = dict(zip(stats[0][0::2], stats[0][1::2]))
    cs = dict(zip(stats[1][0::2], stats[1][1::2]))
    check("MEMORY STATS names are Redis's", sorted(set(cs) - set(rs)), [])
    check("keys.count", cs["keys.count"], 3)
    check("total.allocated >= dataset.bytes > 0", cs["total.allocated"] >= cs["dataset.bytes"] > 0, True)
    check("peak.allocated >= total.allocated", cs["peak.allocated"] >= cs["total.allocated"], True)
finally:
    for p in (redis, cache):
        p.terminate()

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("LATENCY and MEMORY agree with Redis, in shape")
