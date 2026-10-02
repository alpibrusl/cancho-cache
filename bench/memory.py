#!/usr/bin/env python3
"""Memory: what a key costs, and what an idle process holds, against Redis.

    python3 bench/memory.py [path/to/cache] [keys]

Loads `keys` keys (16-byte key, 100-byte value; default 500,000) into Redis and into a cache whose arena and key table are
sized to the data (the arena for exactly that many records, `max_keys` = keys), then reads each process's resident set from
/proc. The cache's arena is committed when it starts (its byte slabs are filled when they are made), so its resident set
barely grows as the data arrives: the figure that matters for it is the total at the size you configured, which is what is printed.
"""
import os, socket, subprocess, sys, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
N = int(sys.argv[2]) if len(sys.argv) > 2 else 500000
KEYLEN, VALLEN = 16, 100


def cmd(*args):
    out = b"*%d\r\n" % len(args)
    for a in args:
        a = a.encode() if isinstance(a, str) else a
        out += b"$%d\r\n%s\r\n" % (len(a), a)
    return out


def rss_kb(pid):
    for line in open("/proc/%d/status" % pid):
        if line.startswith("VmRSS"):
            return int(line.split()[1])


def start(argv, port):
    p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return p
        except OSError:
            time.sleep(0.05)
    raise SystemExit("did not start")


def load(port, n):
    s = socket.create_connection(("127.0.0.1", port))
    val = b"v" * VALLEN
    batch = 2000
    for base in range(0, n, batch):
        s.sendall(b"".join(cmd(b"user:%011d" % i, val) if False else cmd("SET", b"user:%011d" % i, val) for i in range(base, min(base + batch, n))))
        need = min(batch, n - base) * 5
        got = 0
        while got < need:
            got += len(s.recv(65536))
    return s


def info_used(port):
    s = socket.create_connection(("127.0.0.1", port))
    s.sendall(cmd("INFO", "memory"))
    time.sleep(0.2)
    text = s.recv(65536).decode()
    for line in text.splitlines():
        if line.startswith("used_memory:"):
            return int(line.split(":")[1])


rp = start(["redis-server", "--port", "6431", "--save", "", "--appendonly", "no", "--protected-mode", "no"], 6431)
idle_r = rss_kb(rp.pid)
base_used = info_used(6431)
load(6431, N)
time.sleep(0.5)
loaded_r = rss_kb(rp.pid)
used_r = info_used(6431)
rp.terminate()

record = 8 + KEYLEN + (VALLEN + 7) // 8 * 8
arena = (N * record) // (1 << 20) + 2
cp = start([CACHE, "6432", str(arena), str(N)], 6432)
idle_c = rss_kb(cp.pid)
load(6432, N)
time.sleep(0.5)
loaded_c = rss_kb(cp.pid)
cp.terminate()

print("%d keys of %d+%d bytes" % (N, KEYLEN, VALLEN))
print("redis  idle RSS %7.1f MiB   loaded RSS %7.1f MiB   used_memory per key %6.1f B   RSS per key %6.1f B" % (idle_r / 1024, loaded_r / 1024, (used_r - base_used) / N, (loaded_r - idle_r) * 1024 / N))
print("cache  idle RSS %7.1f MiB   loaded RSS %7.1f MiB   (arena %d MiB for %d records of %d B, key table for %d keys; arena committed at start)" % (idle_c / 1024, loaded_c / 1024, arena, N, record, N))
print("cache  total RSS per key at this size: %.1f B   (Redis loaded RSS per key: %.1f B)" % (loaded_c * 1024 / N, loaded_r * 1024 / N))
