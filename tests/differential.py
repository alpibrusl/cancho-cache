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
    # SET and its options
    ("SET NX on a new key, then on it", cmd("SET", "o:nx", "1", "NX") + cmd("SET", "o:nx", "2", "NX") + cmd("GET", "o:nx")),
    ("SET XX on a missing key, then on it", cmd("SET", "o:xx", "1", "XX") + cmd("GET", "o:xx") + cmd("SET", "o:xx", "1") + cmd("SET", "o:xx", "2", "XX") + cmd("GET", "o:xx")),
    ("SET GET", cmd("SET", "o:g", "old") + cmd("SET", "o:g", "new", "GET") + cmd("GET", "o:g") + cmd("SET", "o:g2", "x", "GET")),
    ("SET GET with NX and XX", cmd("SET", "o:gn", "a") + cmd("SET", "o:gn", "b", "NX", "GET") + cmd("SET", "o:gn", "c", "XX", "GET") + cmd("GET", "o:gn")),
    ("SET EX then TTL", cmd("SET", "o:ex", "v", "EX", "1000") + cmd("TTL", "o:ex") + cmd("GET", "o:ex")),
    ("SET PX then TTL", cmd("SET", "o:px", "v", "PX", "1000000") + cmd("TTL", "o:px")),
    ("SET clears a TTL; KEEPTTL keeps it", cmd("SET", "o:k", "v", "EX", "1000") + cmd("SET", "o:k", "w", "KEEPTTL") + cmd("TTL", "o:k") + cmd("SET", "o:k", "x") + cmd("TTL", "o:k")),
    ("SET options in any case", cmd("set", "o:c", "v", "ex", "1000", "nx") + cmd("ttl", "o:c") + cmd("SeT", "o:c", "w", "xX", "gEt")),
    ("SET EX twice", cmd("SET", "o:2", "v", "EX", "100", "EX", "2000") + cmd("TTL", "o:2")),
    ("SET EX and PX", cmd("SET", "o:3", "v", "EX", "100", "PX", "100")),
    ("SET KEEPTTL and EX", cmd("SET", "o:4", "v", "KEEPTTL", "EX", "100")),
    ("SET EX and KEEPTTL", cmd("SET", "o:5", "v", "EX", "100", "KEEPTTL")),
    ("SET NX and XX", cmd("SET", "o:6", "v", "NX", "XX")),
    ("SET EX with no number", cmd("SET", "o:7", "v", "EX")),
    ("SET EX not a number", cmd("SET", "o:8", "v", "EX", "abc")),
    ("SET EX zero", cmd("SET", "o:9", "v", "EX", "0")),
    ("SET EX negative", cmd("SET", "o:10", "v", "EX", "-5")),
    ("SET EX that overflows", cmd("SET", "o:11", "v", "EX", "9223372036854775807")),
    ("SET PX that overflows with the clock", cmd("SET", "o:12", "v", "PX", "9223372036854775807")),
    ("SET unknown option", cmd("SET", "o:13", "v", "FOO")),
    ("SET syntax error beats a bad number", cmd("SET", "o:14", "v", "EX", "abc", "FOO")),
    # INCR and friends
    ("INCR a new key, twice", cmd("INCR", "n:a") + cmd("INCR", "n:a") + cmd("GET", "n:a")),
    ("DECR, INCRBY, DECRBY", cmd("DECR", "n:b") + cmd("INCRBY", "n:b", "10") + cmd("DECRBY", "n:b", "3") + cmd("INCRBY", "n:b", "-100") + cmd("GET", "n:b")),
    ("INCR on a string", cmd("SET", "n:c", "hello") + cmd("INCR", "n:c") + cmd("GET", "n:c")),
    ("INCRBY with a non-integer", cmd("INCRBY", "n:d", "1.5") + cmd("INCRBY", "n:d", "abc") + cmd("INCRBY", "n:d", "") + cmd("INCRBY", "n:d", " 1")),
    ("INCR at the top of the range", cmd("SET", "n:e", "9223372036854775806") + cmd("INCR", "n:e") + cmd("INCR", "n:e") + cmd("GET", "n:e")),
    ("DECR at the bottom of the range", cmd("SET", "n:f", "-9223372036854775807") + cmd("DECR", "n:f") + cmd("DECR", "n:f") + cmd("GET", "n:f")),
    ("INCRBY across the range", cmd("SET", "n:g", "1") + cmd("INCRBY", "n:g", "9223372036854775807") + cmd("INCRBY", "n:g", "-9223372036854775807") + cmd("INCRBY", "n:g", "-9223372036854775808")),
    ("DECRBY the smallest integer", cmd("DECRBY", "n:h", "-9223372036854775808") + cmd("DECRBY", "n:h", "9223372036854775807")),
    ("values that are not integers", cmd("SET", "n:i", "007") + cmd("INCR", "n:i") + cmd("SET", "n:i", " 7") + cmd("INCR", "n:i") + cmd("SET", "n:i", "+7") + cmd("INCR", "n:i") + cmd("SET", "n:i", "-0") + cmd("INCR", "n:i") + cmd("SET", "n:i", "7 ") + cmd("INCR", "n:i") + cmd("SET", "n:i", "") + cmd("INCR", "n:i")),
    ("values at the edge of 64 bits", cmd("SET", "n:j", "9223372036854775808") + cmd("INCR", "n:j") + cmd("SET", "n:j", "-9223372036854775808") + cmd("INCR", "n:j") + cmd("SET", "n:j", "-9223372036854775809") + cmd("INCR", "n:j") + cmd("SET", "n:j", "12345678901234567890123") + cmd("INCR", "n:j")),
    ("INCR keeps the TTL", cmd("SET", "n:k", "5", "EX", "1000") + cmd("INCR", "n:k") + cmd("TTL", "n:k")),
    ("INCR arity", cmd("INCR") + cmd("INCR", "a", "b") + cmd("INCRBY", "a") + cmd("INCRBY", "a", "1", "2") + cmd("DECR") + cmd("DECRBY", "a")),
    # EXPIRE, TTL, PERSIST
    ("EXPIRE, TTL, PERSIST", cmd("SET", "e:a", "v") + cmd("TTL", "e:a") + cmd("EXPIRE", "e:a", "1000") + cmd("TTL", "e:a") + cmd("PERSIST", "e:a") + cmd("TTL", "e:a") + cmd("PERSIST", "e:a")),
    ("EXPIRE and TTL on a missing key", cmd("EXPIRE", "e:none", "10") + cmd("TTL", "e:none") + cmd("PTTL", "e:none") + cmd("PERSIST", "e:none") + cmd("PEXPIRE", "e:none", "10")),
    ("EXPIRE with a non-positive time deletes", cmd("SET", "e:b", "v") + cmd("EXPIRE", "e:b", "0") + cmd("GET", "e:b") + cmd("SET", "e:b", "v") + cmd("EXPIRE", "e:b", "-1") + cmd("EXISTS", "e:b") + cmd("SET", "e:b", "v") + cmd("PEXPIRE", "e:b", "-1") + cmd("EXISTS", "e:b")),
    ("EXPIRE NX XX GT LT", cmd("SET", "e:c", "v") + cmd("EXPIRE", "e:c", "100", "XX") + cmd("EXPIRE", "e:c", "100", "NX") + cmd("EXPIRE", "e:c", "100", "NX") + cmd("EXPIRE", "e:c", "200", "XX") + cmd("TTL", "e:c") + cmd("EXPIRE", "e:c", "100", "GT") + cmd("EXPIRE", "e:c", "300", "GT") + cmd("TTL", "e:c") + cmd("EXPIRE", "e:c", "400", "LT") + cmd("EXPIRE", "e:c", "50", "LT") + cmd("TTL", "e:c")),
    ("EXPIRE GT and LT on a key with no expiry", cmd("SET", "e:d", "v") + cmd("EXPIRE", "e:d", "100", "GT") + cmd("TTL", "e:d") + cmd("EXPIRE", "e:d", "100", "LT") + cmd("TTL", "e:d")),
    ("EXPIRE option errors", cmd("SET", "e:e", "v") + cmd("EXPIRE", "e:e", "10", "NX", "XX") + cmd("EXPIRE", "e:e", "10", "NX", "GT") + cmd("EXPIRE", "e:e", "10", "GT", "LT") + cmd("EXPIRE", "e:e", "10", "FOO") + cmd("EXPIRE", "e:e", "10", "nx", "bar")),
    ("EXPIRE time errors", cmd("SET", "e:f", "v") + cmd("EXPIRE", "e:f", "abc") + cmd("EXPIRE", "e:f", "9223372036854775807") + cmd("EXPIRE", "e:f", "-9223372036854775808") + cmd("PEXPIRE", "e:f", "9223372036854775807") + cmd("PEXPIRE", "e:f", "1.5")),
    ("EXPIRE in any case", cmd("SET", "e:g", "v") + cmd("expire", "e:g", "100", "nx") + cmd("ttl", "e:g")),
    ("EXPIRE arity", cmd("EXPIRE") + cmd("EXPIRE", "a") + cmd("TTL") + cmd("TTL", "a", "b") + cmd("PERSIST") + cmd("PTTL")),
    # the rest
    ("SETNX", cmd("SETNX", "r:a", "1") + cmd("SETNX", "r:a", "2") + cmd("GET", "r:a")),
    ("SETEX and PSETEX", cmd("SETEX", "r:b", "1000", "v") + cmd("TTL", "r:b") + cmd("PSETEX", "r:c", "1000000", "v") + cmd("TTL", "r:c") + cmd("GET", "r:c")),
    ("SETEX errors", cmd("SETEX", "r:d", "0", "v") + cmd("SETEX", "r:d", "-1", "v") + cmd("SETEX", "r:d", "abc", "v") + cmd("SETEX", "r:d", "9223372036854775807", "v") + cmd("PSETEX", "r:d", "0", "v") + cmd("PSETEX", "r:d", "9223372036854775807", "v") + cmd("EXISTS", "r:d")),
    ("GETSET", cmd("GETSET", "r:e", "1") + cmd("GETSET", "r:e", "2") + cmd("GET", "r:e") + cmd("SET", "r:f", "v", "EX", "1000") + cmd("GETSET", "r:f", "w") + cmd("TTL", "r:f")),
    ("GETDEL", cmd("SET", "r:g", "v") + cmd("GETDEL", "r:g") + cmd("GETDEL", "r:g") + cmd("EXISTS", "r:g")),
    ("MSET and MGET", cmd("MSET", "r:h", "1", "r:i", "2", "r:j", "3") + cmd("MGET", "r:h", "r:none", "r:j", "r:i", "r:h") + cmd("MGET", "r:none")),
    ("MSET with a repeated key", cmd("MSET", "r:k", "1", "r:k", "2") + cmd("GET", "r:k")),
    ("MSET and MGET arity", cmd("MSET") + cmd("MSET", "a") + cmd("MSET", "a", "1", "b") + cmd("MGET")),
    ("MSET and MGET of 31 keys", cmd("MSET", *sum([["r:m%d" % i, "v%d" % i] for i in range(31)], [])) + cmd("MGET", *["r:m%d" % i for i in range(31)])),
    ("STRLEN and TYPE", cmd("SET", "r:l", "hello") + cmd("STRLEN", "r:l") + cmd("STRLEN", "r:none") + cmd("TYPE", "r:l") + cmd("TYPE", "r:none") + cmd("SET", "r:e2", "") + cmd("STRLEN", "r:e2")),
    ("UNLINK and TOUCH", cmd("SET", "r:n", "1") + cmd("SET", "r:o", "2") + cmd("TOUCH", "r:n", "r:o", "r:none") + cmd("UNLINK", "r:n", "r:o", "r:none") + cmd("EXISTS", "r:n")),
    ("SELECT", cmd("SELECT", "0") + cmd("SELECT", "16") + cmd("SELECT", "abc") + cmd("SELECT", "-1") + cmd("SELECT")),
    ("arity of the rest", cmd("SETNX", "a") + cmd("SETEX", "a", "1") + cmd("GETSET", "a") + cmd("GETDEL") + cmd("STRLEN") + cmd("TYPE") + cmd("DBSIZE", "x") + cmd("TOUCH") + cmd("UNLINK")),
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
    ("sixty-five arguments", cmd("FOO", *["a"] * 64), "the cache takes at most 64 arguments per command; Redis takes a million"),
    ("SET with EXAT", cmd("SET", "k:exat", "1", "EXAT", "99999999999"), "EXAT, PXAT, EXPIREAT and PEXPIREAT name a time of day, and the cache has a monotonic clock and no calendar"),
    ("EXPIREAT", cmd("EXPIREAT", "k:exat", "99999999999"), "the same"),
    ("SELECT 1", cmd("SELECT", "1"), "one database, not sixteen: SELECT 0 is OK and every other index is out of range"),
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
    # DIFF_ONLY=substring runs only the cases whose name contains it (to check one thing quickly).
    only = os.environ.get("DIFF_ONLY")
    global cases, small_cases
    if only:
        cases = [c for c in cases if only in c[0]]
        small_cases = [c for c in small_cases if only in c[0]]
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
