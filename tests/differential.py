#!/usr/bin/env python3
"""Differential test: the same bytes into Redis and into the cache, replies compared byte for byte.

    python3 tests/differential.py [path/to/cache]        (default build/cache; needs redis-server)

Every case is sent in four framings -- whole, one byte at a time, random splits, and twenty copies in one
write (pipelined) -- because a parser that is right on whole commands and wrong on split ones is the usual
failure. Redis is the oracle. A reply is everything the server sends until it closes the connection or says
nothing for a moment.

KNOWN divergences are listed with the reason, and are compared the other way round: if one stops
diverging the test says so, so the list cannot go stale. Exit status 0 only if every case agrees and
every known divergence still diverges.
"""
import os, random, socket, subprocess, sys, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
REDIS_PORT, CACHE_PORT = 6392, 6393


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
    raise SystemExit("server on %d did not start: %s" % (port, argv))


def talk(port, chunks, quiet=0.25):
    s = socket.create_connection(("127.0.0.1", port))
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    got = b""
    try:
        for c in chunks:
            try:
                s.sendall(c)
            except OSError:
                break
            if len(chunks) > 1:
                time.sleep(0.0003)
        s.settimeout(quiet)
        while True:
            try:
                d = s.recv(65536)
            except socket.timeout:
                break
            except OSError:
                break
            if not d:
                break
            got += d
    finally:
        s.close()
    return got


def framings(data, rnd):
    yield "whole", [data]
    if len(data) <= 300:
        yield "bytewise", [data[i:i + 1] for i in range(len(data))]
    pieces, i = [], 0
    while i < len(data):
        n = rnd.randint(1, 17)
        pieces.append(data[i:i + n])
        i += n
    yield "random splits", pieces
    yield "pipelined x20", [data * 20]


binary = bytes(range(256))
cases = [
    ("PING", cmd("PING")),
    ("ping lower", cmd("ping")),
    ("PiNg mixed", cmd("PiNg")),
    ("PING msg", cmd("PING", "hello")),
    ("PING empty msg", cmd("PING", "")),
    ("PING two args", cmd("PING", "a", "b")),
    ("ECHO", cmd("ECHO", "hello")),
    ("echo empty", cmd("echo", "")),
    ("ECHO all 256 byte values", cmd("ECHO", binary)),
    ("ECHO value of protocol bytes", cmd("ECHO", b"\r\n*1\r\n$4\r\nPING\r\n")),
    ("ECHO 10000 bytes", cmd("ECHO", b"x" * 10000)),
    ("ECHO no argument", cmd("ECHO")),
    ("ECHO two arguments", cmd("ECHO", "a", "b")),
    ("unknown, no args", cmd("FOO")),
    ("unknown, three args", cmd("FOO", "a", "b", "c")),
    ("unknown, empty name", cmd("")),
    ("unknown, long args cut at 128", cmd("FOO", "a" * 100, "b" * 100, "c")),
    ("unknown, one 300-byte arg", cmd("FOO", "z" * 300)),
    ("unknown, long name", cmd("N" * 300, "x")),
    ("unknown, sixteen args", cmd("FOO", *["a%d" % i for i in range(15)])),
    ("empty array *0", b"*0\r\n"),
    ("null array *-1", b"*-1\r\n"),
    ("empty array then PING", b"*0\r\n" + cmd("PING")),
    ("PING PING ECHO pipelined", cmd("PING") + cmd("PING", "a") + cmd("ECHO", "b")),
    ("protocol: bad array length", b"*x\r\n"),
    ("protocol: expected $", b"*1\r\nx\r\n"),
    ("protocol: expected $ got digit", b"*1\r\n7\r\n"),
    ("protocol: bad bulk length", b"*1\r\n$x\r\n"),
    ("protocol: negative bulk length", b"*1\r\n$-1\r\n"),
    ("command after a protocol error is never answered", b"*x\r\n" + cmd("PING")),
    ("answer first, then protocol error", cmd("PING") + b"*x\r\n"),
]

# Where the cache is deliberately not Redis, and why. Each is run and must still differ.
known = [
    ("inline PING", b"PING\r\n", "inline commands (typed into telnet) are not supported: design.md section 4"),
    ("bulk not followed by CRLF", b"*1\r\n$3\r\nabcde\r\n", "Redis skips two bytes after a bulk without checking them; the cache refuses (a request-smuggling-shaped ambiguity)"),
    ("seventeen arguments", cmd("FOO", *["a"] * 16), "the cache takes at most 16 arguments per command; Redis takes a million"),
]


def main():
    redis = start(["redis-server", "--port", str(REDIS_PORT), "--save", "", "--appendonly", "no", "--protected-mode", "no"], REDIS_PORT)
    cache = start([CACHE, str(CACHE_PORT)], CACHE_PORT)
    bad = 0
    try:
        rnd = random.Random(20261002)
        for name, data in cases:
            for fname, chunks in framings(data, rnd):
                want, got = talk(REDIS_PORT, chunks), talk(CACHE_PORT, chunks)
                if want != got:
                    bad += 1
                    print("DIFFERS  %s [%s]\n   redis: %r\n   cache: %r" % (name, fname, want[:200], got[:200]))
        print("%d cases x framings compared, %d differ" % (len(cases), bad))
        for name, data, why in known:
            want, got = talk(REDIS_PORT, [data]), talk(CACHE_PORT, [data])
            if want == got:
                bad += 1
                print("STALE    known divergence no longer diverges: %s" % name)
            else:
                print("known    %s: %s\n   redis: %r\n   cache: %r" % (name, why, want[:100], got[:100]))
    finally:
        redis.terminate()
        cache.terminate()
    sys.exit(1 if bad else 0)


main()
