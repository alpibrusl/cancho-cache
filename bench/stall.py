#!/usr/bin/env python3
"""The worst-case pause: what one command costs when it has to compact the arena (and evict to make the room).

    python3 bench/stall.py [path/to/cache] [arena MiB]

Fills an `allkeys-lru` cache past its arena with 100-byte values, then times each of 200,000 more SETs (one outstanding
at a time, over loopback) and prints the median, 99.9th percentile and the largest. A compaction slides every live record
down, in place, so it takes time in proportion to the arena; it happens once per sixteenth of the arena written, so most
SETs never see it and the few that do see all of it. Redis has no such pause (it frees one value at a time) and the printed
maximum is the price of that difference.
"""
import os, socket, subprocess, sys, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
ARENA = sys.argv[2] if len(sys.argv) > 2 else "64"


def cmd(*args):
    out = b"*%d\r\n" % len(args)
    for a in args:
        a = a.encode() if isinstance(a, str) else a
        out += b"$%d\r\n%s\r\n" % (len(a), a)
    return out


p = subprocess.Popen([CACHE, "6451", ARENA, "4000000", "allkeys-lru"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1", 6451), 0.2).close()
        break
    except OSError:
        time.sleep(0.05)
s = socket.create_connection(("127.0.0.1", 6451))
s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
val = b"v" * 100
BATCH = 20  # small, so no turn of the server is long just because it was handed a lot: its longest turn (INFO max_turn_ms) is then the bound
records = int(ARENA) * (1 << 20) // 128
# fill to the brim in pipelined batches
n = 0
while n < records * 12 // 10:
    s.sendall(b"".join(cmd("SET", "f:%d" % i, val) for i in range(n, n + BATCH)))
    got = 0
    while got < BATCH * 5:
        got += len(s.recv(1 << 16))
    n += BATCH
times = []
for i in range(200000):
    t = time.perf_counter()
    s.sendall(cmd("SET", "g:%d" % i, val))
    s.recv(100)
    times.append((time.perf_counter() - t) * 1000)
times.sort()
print("arena %s MiB, %d records filled then 200,000 more SETs: median %.3f ms   p99.9 %.3f ms   max %.1f ms   (SETs over 2 ms: %d)" % (ARENA, n, times[len(times) // 2], times[int(len(times) * 0.999)], times[-1], sum(1 for t in times if t > 2)))
s.sendall(cmd("INFO", "STATS"))
time.sleep(0.2)
info = s.recv(1 << 16).decode(errors="replace")
print("   " + " ".join(l.strip() for l in info.split("\n") if l.startswith(("compactions", "evicted_keys", "max_turn_ms"))))
p.terminate()
