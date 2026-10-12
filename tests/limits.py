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

# The longest value, and what a reply of the longest value does to the buffer it is built in. `APPEND` and `SETRANGE` once let a value grow
# past what a reply buffer holds, and a `GET` of one killed the server.
TOO_LONG = b"-ERR string exceeds maximum allowed size (proto-max-bulk-len)\r\n"
p = serve(6399, "64")
try:
    s = socket.create_connection(("127.0.0.1", 6399))
    s.settimeout(10)
    top = 16384

    def read_exactly(n):
        got = b""
        while len(got) < n:
            d = s.recv(1 << 20)
            if not d:
                raise SystemExit("connection closed while reading a reply (%d of %d bytes): %r" % (len(got), n, got[:60]))
            got += d
        return got

    check("APPEND up to the longest value", ask(s, "APPEND", "big", b"a" * 10000), b":10000\r\n")
    check("... and to exactly the longest", ask(s, "APPEND", "big", b"b" * (top - 10000)), b":%d\r\n" % top)
    check("one byte more is refused in Redis's words", ask(s, "APPEND", "big", "c"), TOO_LONG)
    check("and changed nothing", ask(s, "STRLEN", "big"), b":%d\r\n" % top)
    check("SETRANGE ending at the longest", ask(s, "SETRANGE", "r", str(top - 1), "x"), b":%d\r\n" % top)
    check("SETRANGE one past it is refused", ask(s, "SETRANGE", "r", str(top), "x"), TOO_LONG)
    check("SETRANGE far past it is refused (this used to build a 4 MB value)", ask(s, "SETRANGE", "far", "4000000", "x"), TOO_LONG)
    check("and made no key", ask(s, "EXISTS", "far"), b":0\r\n")
    check("GET of the longest value", ask(s, "GET", "big"), b"$%d\r\n" % top + b"a" * 10000 + b"b" * (top - 10000) + b"\r\n")
    # The largest reply one command can make: MGET of 63 keys of the longest value (the command name is the 64th argument).
    for i in range(63):
        # (a longest value does not fit one command, so it is built in two)
        ask(s, "APPEND", "m%d" % i, bytes([65 + i % 26]) * (top // 2))
        check("value %d of 63 longest values" % i, ask(s, "APPEND", "m%d" % i, bytes([65 + i % 26]) * (top // 2)), b":%d\r\n" % top)
    s.sendall(cmd("MGET", *["m%d" % i for i in range(63)]))
    want = b"*63\r\n" + b"".join(b"$%d\r\n" % top + bytes([65 + i % 26]) * top + b"\r\n" for i in range(63))
    check("MGET of 63 longest values, the largest reply", read_exactly(len(want)), want)
    # A transaction of 100 such GETs: 1.6 MB of answers, sent in pieces.
    s.sendall(cmd("MULTI") + b"".join(cmd("GET", "m%d" % (i % 63)) for i in range(100)) + cmd("EXEC"))
    want = b"+OK\r\n" + b"+QUEUED\r\n" * 100 + b"*100\r\n" + b"".join(b"$%d\r\n" % top + bytes([65 + (i % 63) % 26]) * top + b"\r\n" for i in range(100))
    check("EXEC of 100 GETs of the longest value", read_exactly(len(want)), want)
    # Answers larger than anything buffered whole (issue #11): MGET writes its answer in pieces, and goes on from where it was when the
    # client has read what it sent. 300 keys of the longest value is 4.9 MB, more than the buffer an answer is built in holds.
    for i in range(300):
        ask(s, "APPEND", "big%d" % i, bytes([65 + i % 26]) * (top // 2))
        ask(s, "APPEND", "big%d" % i, bytes([65 + i % 26]) * (top // 2))
    keys300 = ["big%d" % i for i in range(300)]
    want300 = b"*300\r\n" + b"".join(b"$%d\r\n" % top + bytes([65 + i % 26]) * top + b"\r\n" for i in range(300))
    s.sendall(cmd("MGET", *keys300))
    check("MGET of 300 longest values (4.9 MB), read as it comes", read_exactly(len(want300)), want300)
    s.sendall(cmd("MGET", *keys300) + cmd("MGET", *keys300[:70]) + cmd("PING"))
    want = want300 + want300[:6].replace(b"300", b"70") + b"".join(b"$%d\r\n" % top + bytes([65 + i % 26]) * top + b"\r\n" for i in range(70)) + b"+PONG\r\n"
    check("two MGETs and a PING in one write, in order", read_exactly(len(want)), want)
    # A client that does not read for a while: the answer waits for it and arrives whole, and others are served meanwhile.
    slow = socket.create_connection(("127.0.0.1", 6399))
    slow.settimeout(10)
    slow.sendall(cmd("MGET", *keys300))
    time.sleep(1.0)
    check("while one client is not reading, another is answered", ask(s, "PING"), b"+PONG\r\n")
    got = b""
    while len(got) < len(want300):
        d = slow.recv(1 << 20)
        if not d:
            break
        got += d
    check("the client that did not read for a second gets all of its 4.9 MB, in order", got, want300)
    slow.close()
    # Many arguments, which a command used to have at most 64 of.
    for i in range(1000):
        pass
    s.sendall(b"".join(cmd("SET", "n%d" % i, "v%d" % i) for i in range(1000)))
    read_exactly(5 * 1000)
    s.sendall(cmd("MGET", *["n%d" % i for i in range(1000)]))
    want1000 = b"*1000\r\n" + b"".join(b"$%d\r\nv%d\r\n" % (len(b"v%d" % i), i) for i in range(1000))
    check("MGET of 1000 keys", read_exactly(len(want1000)), want1000)
    check("DEL of 1000 keys", ask(s, "DEL", *["n%d" % i for i in range(1000)]), b":1000\r\n")
    s.sendall(cmd("MGET", *["n%d" % i for i in range(1000)]))
    check("and they are gone", read_exactly(6 + 5 * 1000), b"*1000\r\n" + b"$-1\r\n" * 1000)
    # The same for a client that does not read at all for a second, whatever the kernel's buffers hold: a pipeline of 200 GETs of the longest
    # value (3.3 MB) and a transaction of 20 MGETs (20 MB). The loop stops when the client's share is waiting and goes on when it has read.
    slow = socket.create_connection(("127.0.0.1", 6399))
    slow.settimeout(20)
    slow.sendall(b"".join(cmd("GET", "big%d" % (i % 60)) for i in range(200)) + cmd("PING"))
    time.sleep(1.0)
    want = b"".join(b"$%d\r\n" % top + bytes([65 + (i % 60) % 26]) * top + b"\r\n" for i in range(200)) + b"+PONG\r\n"
    got = b""
    while len(got) < len(want):
        d = slow.recv(1 << 20)
        if not d:
            break
        got += d
    check("200 pipelined GETs of the longest value, the client reading only after a second", got, want)
    slow.close()
    slow = socket.create_connection(("127.0.0.1", 6399))
    slow.settimeout(30)
    slow.sendall(cmd("MULTI") + b"".join(cmd("MGET", *keys300[:60]) for _ in range(20)) + cmd("EXEC") + cmd("PING"))
    time.sleep(1.5)
    one = b"*60\r\n" + b"".join(b"$%d\r\n" % top + bytes([65 + i % 26]) * top + b"\r\n" for i in range(60))
    want = b"+OK\r\n" + b"+QUEUED\r\n" * 20 + b"*20\r\n" + one * 20 + b"+PONG\r\n"
    got = b""
    while len(got) < len(want):
        d = slow.recv(1 << 20)
        if not d:
            break
        got += d
    check("EXEC of 20 MGETs (20 MB) for a client that reads after 1.5 s: all of it, once, and the PING after", got, want)
    slow.close()
    # A transaction of 100 MGETs of the longest value: 20 MB of answers in pieces, for a client that reads at once.
    s.sendall(cmd("MULTI") + b"".join(cmd("MGET", *keys300[:60]) for _ in range(20)) + cmd("EXEC"))
    one = b"*60\r\n" + b"".join(b"$%d\r\n" % top + bytes([65 + i % 26]) * top + b"\r\n" for i in range(60))
    want = b"+OK\r\n" + b"+QUEUED\r\n" * 20 + b"*20\r\n" + one * 20
    check("EXEC of 20 MGETs of 60 longest values (20 MB)", read_exactly(len(want)), want)
    check("the server is still there", ask(s, "PING"), b"+PONG\r\n")
finally:
    p.terminate()

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("the limits refuse in Redis's words, change nothing, and the server carries on")
