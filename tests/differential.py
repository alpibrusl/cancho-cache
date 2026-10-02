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
REDIS_PORT, CACHE_PORT, SMALL_PORT = 6392, 6393, 6396


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
    ("SET then GET", cmd("SET", "d:a", "hello") + cmd("GET", "d:a")),
    ("GET missing", cmd("GET", "d:missing")),
    ("SET overwrites", cmd("SET", "d:b", "one") + cmd("SET", "d:b", "two") + cmd("GET", "d:b")),
    ("SET empty value", cmd("SET", "d:e", "") + cmd("GET", "d:e") + cmd("EXISTS", "d:e")),
    ("SET empty key", cmd("SET", "", "v") + cmd("GET", "") + cmd("DEL", "")),
    ("SET binary key and value", cmd("SET", binary, binary[::-1]) + cmd("GET", binary) + cmd("DEL", binary)),
    ("SET value of protocol bytes", cmd("SET", "d:p", b"\r\n$3\r\nGET\r\n") + cmd("GET", "d:p")),
    ("SET 10000 bytes", cmd("SET", "d:big", b"v" * 10000) + cmd("GET", "d:big")),
    ("SET grows then shrinks", cmd("SET", "d:g", "a") + cmd("SET", "d:g", "b" * 300) + cmd("SET", "d:g", "c") + cmd("GET", "d:g")),
    ("DEL counts what existed", cmd("SET", "d:x", "1") + cmd("SET", "d:y", "2") + cmd("DEL", "d:x", "d:y", "d:z") + cmd("GET", "d:x") + cmd("DEL", "d:x")),
    ("EXISTS counts duplicates", cmd("SET", "d:q", "1") + cmd("EXISTS", "d:q", "d:q", "d:none", "d:q")),
    ("commands in any case", cmd("set", "d:c", "1") + cmd("GeT", "d:c") + cmd("dEl", "d:c") + cmd("exists", "d:c")),
    ("GET no key", cmd("GET")),
    ("GET two keys", cmd("GET", "a", "b")),
    ("SET one argument", cmd("SET", "a")),
    ("SET no argument", cmd("SET")),
    ("DEL no key", cmd("DEL")),
    ("EXISTS no key", cmd("EXISTS")),
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
    ("SET with EX", cmd("SET", "k:ex", "1", "EX", "100"), "options of SET (EX, PX, NX, XX, GET, KEEPTTL) come with expiry, step C2"),
    ("SET with NX", cmd("SET", "k:nx", "1", "NX"), "options of SET come with expiry, step C2"),
]


def random_mix(seed, ops, keys=20, longest=300, prefix="m"):
    """A seeded stream of SET/GET/DEL/EXISTS over a small keyspace: values that fit where they are, grow past it,
    shrink again, and keys that come and go -- the paths of the store that a hand-written case does not reach."""
    r = random.Random(seed)
    out = b""
    for _ in range(ops):
        k = "%s:%d" % (prefix, r.randrange(keys))
        x = r.random()
        if x < 0.45:
            out += cmd("SET", k, bytes([97 + r.randrange(26)]) * r.choice([0, 1, 3, 8, 9, 40, r.randrange(longest)]))
        elif x < 0.75:
            out += cmd("GET", k)
        elif x < 0.9:
            out += cmd("DEL", k, *["%s:%d" % (prefix, r.randrange(keys)) for _ in range(r.randrange(3))])
        else:
            out += cmd("EXISTS", k, "%s:%d" % (prefix, r.randrange(keys)))
    return out


cases += [("random mix %d" % seed, random_mix(seed, 400)) for seed in (1, 2, 3, 4, 5)]
# Against a cache whose index has 32 slots (`cache <port> 64 16`) and a keyspace that fills half of it, so that keys do collide, probe runs
# form, and a deletion has to shift entries back: with the default million-key index twenty keys never touch each other.
small_cases = [("small index, random mix %d" % seed, random_mix(seed, 500, keys=14, longest=60, prefix="s")) for seed in range(11, 19)]
cases += [("random mix, two keys, many collisions", random_mix(99, 600, keys=2)), ("random mix, long values", random_mix(7, 150, keys=5, longest=3000))]


def main():
    redis = start(["redis-server", "--port", str(REDIS_PORT), "--save", "", "--appendonly", "no", "--protected-mode", "no"], REDIS_PORT)
    cache = start([CACHE, str(CACHE_PORT)], CACHE_PORT)
    small = start([CACHE, str(SMALL_PORT), "64", "16"], SMALL_PORT)
    bad = 0
    try:
        rnd = random.Random(20261002)
        for name, data in cases:
            for fname, chunks in framings(data, rnd):
                want, got = talk(REDIS_PORT, chunks), talk(CACHE_PORT, chunks)
                if want != got:
                    bad += 1
                    print("DIFFERS  %s [%s]\n   redis: %r\n   cache: %r" % (name, fname, want[:200], got[:200]))
        for name, data in small_cases:
            for fname, chunks in framings(data, rnd):
                want, got = talk(REDIS_PORT, chunks), talk(SMALL_PORT, chunks)
                if want != got:
                    bad += 1
                    print("DIFFERS  %s [%s]\n   redis: %r\n   cache: %r" % (name, fname, want[:200], got[:200]))
        print("%d cases + %d small-index cases x framings compared, %d differ" % (len(cases), len(small_cases), bad))
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
        small.terminate()
    sys.exit(1 if bad else 0)


main()
