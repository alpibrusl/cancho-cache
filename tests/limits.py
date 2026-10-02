#!/usr/bin/env python3
"""What the cache does when it is full: refuse in Redis's words, change nothing, and carry on.

    python3 tests/limits.py [path/to/cache]

Two limits, each tried on its own server: the arena (`cache <port> 1`: one MiB) and the key table (`cache <port> 64 100`:
a hundred keys). A refused SET is `-OOM command not allowed when used memory > 'maxmemory'.` (what Redis says under
its default `noeviction` policy), leaves the old value of an overwritten key, and does not stop reads, deletes or
overwrites that fit. Eviction (step C2) replaces the arena refusal; the key-table refusal stays until then.
"""
import os, socket, subprocess, sys, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
OOM = b"-OOM command not allowed when used memory > 'maxmemory'.\r\n"


def cmd(*args):
    out = b"*%d\r\n" % len(args)
    for a in args:
        a = a.encode() if isinstance(a, str) else a
        out += b"$%d\r\n%s\r\n" % (len(a), a)
    return out


def serve(port, *extra):
    p = subprocess.Popen([CACHE, str(port), *extra], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return p
        except OSError:
            time.sleep(0.05)
    raise SystemExit("did not start")


def ask(s, *args):
    """Send one command and read exactly one RESP reply (simple string, error, integer, or bulk)."""
    s.sendall(cmd(*args))
    s.settimeout(2)
    got = b""

    def need(n):
        nonlocal got
        while len(got) < n:
            d = s.recv(65536)
            if not d:
                raise SystemExit("connection closed")
            got += d

    def line_end(start=0):
        nonlocal got
        while True:
            i = got.find(b"\r\n", start)
            if i >= 0:
                return i
            need(len(got) + 1)

    first = line_end()
    if got[0:1] == b"$":
        n = int(got[1:first])
        if n < 0:
            return got[: first + 2]
        need(first + 2 + n + 2)
        return got[: first + 2 + n + 2]
    return got[: first + 2]


failures = []


def check(what, got, want):
    if got != want:
        failures.append("%s: got %r, wanted %r" % (what, got[:80], want[:80]))


# The arena: a MiB, values of 10,000 bytes.
p = serve(6397, "1")
try:
    s = socket.create_connection(("127.0.0.1", 6397))
    stored = 0
    while True:
        r = ask(s, "SET", "a%d" % stored, b"v" * 10000)
        if r == OOM:
            break
        check("SET before the arena is full", r, b"+OK\r\n")
        stored += 1
        if stored > 1000:
            failures.append("a one-MiB arena took %d values of 10,000 bytes" % stored)
            break
    check("how many fit in one MiB", b"%d" % (95 <= stored <= 105), b"1")
    check("a refused SET changed nothing", ask(s, "GET", "a%d" % stored), b"$-1\r\n")
    check("an earlier value is intact", ask(s, "GET", "a0"), b"$10000\r\n" + b"v" * 10000 + b"\r\n")
    check("an overwrite that fits still works", ask(s, "SET", "a0", b"w" * 10000), b"+OK\r\n")
    check("and is read back", ask(s, "GET", "a0"), b"$10000\r\n" + b"w" * 10000 + b"\r\n")
    check("an overwrite that does not fit is refused", ask(s, "SET", "a0", b"x" * 12000), OOM)
    check("and leaves the old value", ask(s, "GET", "a0"), b"$10000\r\n" + b"w" * 10000 + b"\r\n")
    check("DEL still works", ask(s, "DEL", "a0"), b":1\r\n")
    check("PING still works", ask(s, "PING"), b"+PONG\r\n")
finally:
    p.terminate()

# The key table: a hundred keys.
p = serve(6398, "64", "100")
try:
    s = socket.create_connection(("127.0.0.1", 6398))
    for i in range(100):
        check("SET key %d of 100" % i, ask(s, "SET", "k%d" % i, "v"), b"+OK\r\n")
    check("the 101st key is refused", ask(s, "SET", "one-too-many", "v"), OOM)
    check("an overwrite needs no new key", ask(s, "SET", "k5", "w"), b"+OK\r\n")
    check("a deletion makes room", ask(s, "DEL", "k7"), b":1\r\n")
    check("which the next key takes", ask(s, "SET", "one-too-many", "v"), b"+OK\r\n")
    check("and then it is full again", ask(s, "SET", "and-another", "v"), OOM)
    check("reads are unaffected", ask(s, "GET", "k5"), b"$1\r\nw\r\n")
finally:
    p.terminate()

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("both limits refuse in Redis's words, change nothing, and the server carries on")
