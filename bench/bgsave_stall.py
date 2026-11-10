#!/usr/bin/env python3
"""The wait of a client while a snapshot is written (docs/design.md section 36).

    python3 bench/bgsave_stall.py [path/to/cache] [keys] [value bytes]

A cache holding `keys` keys of `value bytes` is asked, from one connection, for PINGs one after another while another connection (a) does nothing, (b) runs `BGSAVE` over and over
and (c) runs `SAVE` over and over. For each, the waits of the PINGs: how many, the median, the 99th percentile and the longest, in milliseconds. A blocking `SAVE` stalls everyone
for as long as it takes to write the arena; `BGSAVE` is written a slice a turn, and the longest wait is what is reported, not assumed.
"""
import os, shutil, socket, subprocess, sys, tempfile, threading, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
KEYS = int(sys.argv[2]) if len(sys.argv) > 2 else 300000
SIZE = int(sys.argv[3]) if len(sys.argv) > 3 else 300
PORT = 6931


def cmd(*args):
    out = b"*%d\r\n" % len(args)
    for a in args:
        a = a if isinstance(a, bytes) else a.encode()
        out += b"$%d\r\n%s\r\n" % (len(a), a)
    return out


class Conn:
    def __init__(self):
        self.s = socket.create_connection(("127.0.0.1", PORT))
        self.s.settimeout(60)
        self.buf = b""

    def line(self):
        while b"\r\n" not in self.buf:
            self.buf += self.s.recv(1 << 20)
        l, self.buf = self.buf.split(b"\r\n", 1)
        return l

    def ask(self, *a):
        self.s.sendall(cmd(*a))
        l = self.line()
        if l[:1] == b"$":
            n = int(l[1:])
            while len(self.buf) < n + 2:
                self.buf += self.s.recv(1 << 20)
            self.buf = self.buf[n + 2:]
        return l


def percentile(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))]


work = tempfile.mkdtemp(prefix="bgsave-bench-")
proc = subprocess.Popen([CACHE, str(PORT), "512", "1000000", "noeviction", "1", "--dir=" + work], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(100):
        try:
            c = Conn()
            break
        except OSError:
            time.sleep(0.05)
    loader = Conn()
    for i in range(0, KEYS, 5000):
        loader.s.sendall(b"".join(cmd("SET", "k%d" % j, b"v" * SIZE) for j in range(i, i + 5000)))
        for _ in range(5000):
            loader.line()
    print("%d keys of %d bytes (about %d MiB of records)\n" % (KEYS, SIZE, KEYS * (SIZE + 40) >> 20))
    print("%-28s %8s %9s %9s %9s" % ("while another client", "pings", "median", "p99", "longest"))

    def measure(label, work_fn, seconds=6):
        stop = threading.Event()
        t = threading.Thread(target=work_fn, args=(stop,))
        t.start()
        waits = []
        end = time.time() + seconds
        while time.time() < end:
            t0 = time.perf_counter()
            c.ask("PING")
            waits.append((time.perf_counter() - t0) * 1000)
        stop.set()
        t.join()
        print("%-28s %8d %8.2fms %8.2fms %8.2fms" % (label, len(waits), percentile(waits, 0.5), percentile(waits, 0.99), max(waits)))

    def idle(stop):
        while not stop.is_set():
            time.sleep(0.05)

    def bgsave(stop):
        d = Conn()
        while not stop.is_set():
            d.ask("BGSAVE")
            while b"rdb_bgsave_in_progress:1" in d_info(d) and not stop.is_set():
                time.sleep(0.01)

    def d_info(d):
        d.s.sendall(cmd("INFO", "persistence"))
        l = d.line()
        n = int(l[1:])
        while len(d.buf) < n + 2:
            d.buf += d.s.recv(1 << 20)
        body, d.buf = d.buf[:n], d.buf[n + 2:]
        return body

    def blocking(stop):
        d = Conn()
        while not stop.is_set():
            d.ask("SAVE")
            time.sleep(0.2)

    measure("does nothing", idle)
    measure("runs BGSAVE, again and again", bgsave)
    measure("runs SAVE (blocking)", blocking)
finally:
    proc.kill()
    proc.wait()
    shutil.rmtree(work, ignore_errors=True)
