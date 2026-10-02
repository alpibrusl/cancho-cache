#!/usr/bin/env python3
"""Hit rate on a skewed workload, Redis against the cache, at roughly the same resident memory.

    python3 bench/hitrate.py [path/to/cache]

Cache-aside: a GET, and on a miss a SET of the value. Keys are drawn from a Zipf distribution (exponent 0.99) over 2,000,000
keys, values are 100 bytes; one million requests warm each server and three million are counted. Redis runs with
`--maxmemory 128mb --maxmemory-policy allkeys-lru` and the cache with `allkeys-lru` and an arena chosen so that the two
processes end at about the same resident size (printed). **Their eviction is not the same algorithm** (both sample five keys, but
Redis keeps a 24-bit clock per key and a pool of candidates across evictions, the cache one millisecond stamp and no pool), and
**their memory is not counted the same way**: that is why the comparison is at equal resident size and not at equal settings.
"""
import bisect, itertools, os, random, socket, subprocess, sys, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
KEYS, WARM, COUNT, VAL = 2_000_000, 1_000_000, 3_000_000, b"v" * 100
ARENA_MIB, CACHE_KEYS = 76, 700_000


def cmd(*args):
    out = b"*%d\r\n" % len(args)
    for a in args:
        a = a.encode() if isinstance(a, str) else a
        out += b"$%d\r\n%s\r\n" % (len(a), a)
    return out


def rss_mib(pid):
    for line in open("/proc/%d/status" % pid):
        if line.startswith("VmRSS"):
            return int(line.split()[1]) / 1024


def start(argv, port):
    p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return p
        except OSError:
            time.sleep(0.05)
    raise SystemExit("did not start")


class Reader:
    def __init__(self, s):
        self.s, self.buf = s, b""

    def reply(self):
        while b"\r\n" not in self.buf:
            self.buf += self.s.recv(1 << 20)
        i = self.buf.index(b"\r\n")
        line, rest = self.buf[:i], self.buf[i + 2:]
        if line[:1] == b"$":
            n = int(line[1:])
            if n < 0:
                self.buf = rest
                return None
            while len(rest) < n + 2:
                rest += self.s.recv(1 << 20)
            self.buf = rest[n + 2:]
            return rest[:n]
        self.buf = rest
        return line


def zipf_sampler(n, s, rnd):
    cum = list(itertools.accumulate(1.0 / (k ** s) for k in range(1, n + 1)))
    total = cum[-1]
    return lambda: bisect.bisect_left(cum, rnd.random() * total)


def run(port, pid):
    s = socket.create_connection(("127.0.0.1", port))
    rd = Reader(s)
    rnd = random.Random(2026)
    draw = zipf_sampler(KEYS, 0.99, rnd)
    hits = gets = 0
    batch = 500
    done = 0
    while done < WARM + COUNT:
        ks = [b"k:%d" % draw() for _ in range(batch)]
        s.sendall(b"".join(cmd("GET", k) for k in ks))
        misses = []
        for k in ks:
            v = rd.reply()
            if v is None:
                misses.append(k)
            elif done >= WARM:
                hits += 1
        if done >= WARM:
            gets += batch
        if misses:
            s.sendall(b"".join(cmd("SET", k, VAL) for k in misses))
            for _ in misses:
                rd.reply()
        done += batch
    return hits / gets, rss_mib(pid)


rp = start(["redis-server", "--port", "6441", "--save", "", "--appendonly", "no", "--protected-mode", "no", "--maxmemory", "128mb", "--maxmemory-policy", "allkeys-lru"], 6441)
r_rate, r_rss = run(6441, rp.pid)
rp.terminate()
cp = start([CACHE, "6442", str(ARENA_MIB), str(CACHE_KEYS), "allkeys-lru"], 6442)
c_rate, c_rss = run(6442, cp.pid)
cp.terminate()
print("Zipf 0.99 over %d keys, 100-byte values, %d requests counted after %d warm-up" % (KEYS, COUNT, WARM))
print("redis  maxmemory 128mb allkeys-lru            hit rate %.4f   RSS %6.1f MiB" % (r_rate, r_rss))
print("cache  arena %d MiB, %d keys, allkeys-lru     hit rate %.4f   RSS %6.1f MiB" % (ARENA_MIB, CACHE_KEYS, c_rate, c_rss))
