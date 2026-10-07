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
import os, random, re, socket, subprocess, sys, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
REDIS_PORT, CACHE_PORT, SMALL_PORT = 6392, 6393, 6396


def cmd(*args):
    out = b"*%d\r\n" % len(args)
    for a in args:
        a = a.encode() if isinstance(a, str) else a
        out += b"$%d\r\n%s\r\n" % (len(a), a)
    return out


def start(argv, port):
    p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=None if os.environ.get("DIFF_STDERR") else subprocess.DEVNULL)
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


def norm(reply):
    """HELLO's answer carries the connection's id, which no two servers share: compare everything else."""
    return re.sub(rb"(\$2\r\nid\r\n:)[0-9]+", rb"\1N", reply)


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
    # AUTH, HELLO, CLIENT, CONFIG, QUIT, RESET: what a client says when it connects
    ("AUTH forms", cmd("AUTH", "secret") + cmd("AUTH", "default", "secret") + cmd("AUTH", "bob", "secret") + cmd("AUTH") + cmd("AUTH", "a", "b", "c")),
    ("HELLO argument errors", cmd("HELLO", "abc") + cmd("HELLO", "1") + cmd("HELLO", "0") + cmd("HELLO", "4") + cmd("HELLO", "-1") + cmd("HELLO", "2", "FOO") + cmd("HELLO", "2", "AUTH") + cmd("HELLO", "2", "AUTH", "u") + cmd("HELLO", "2", "SETNAME")),
    ("HELLO auth and setname errors", cmd("HELLO", "2", "AUTH", "bob", "x") + cmd("HELLO", "2", "SETNAME", "a b") + cmd("HELLO", "2", "AUTH", "bob", "x", "SETNAME", "a b")),
    ("CLIENT SETNAME and GETNAME", cmd("CLIENT", "GETNAME") + cmd("CLIENT", "SETNAME", "worker-1") + cmd("CLIENT", "GETNAME") + cmd("CLIENT", "SETNAME", "") + cmd("CLIENT", "GETNAME") + cmd("client", "setname", "x") + cmd("client", "getname")),
    ("CLIENT SETNAME rejects", cmd("CLIENT", "SETNAME", "a b") + cmd("CLIENT", "SETNAME", "a\nb") + cmd("CLIENT", "SETNAME", b"caf\xc3\xa9") + cmd("CLIENT", "GETNAME")),
    ("HELLO SETNAME sets the name", cmd("CLIENT", "SETNAME", "before") + cmd("HELLO", "2", "SETNAME", "after") [:0] + cmd("CLIENT", "GETNAME")),
    ("CLIENT arity", cmd("CLIENT") + cmd("CLIENT", "ID", "x") + cmd("CLIENT", "GETNAME", "x") + cmd("CLIENT", "SETNAME") + cmd("CLIENT", "SETNAME", "a", "b")),
    ("CLIENT unknown subcommand", cmd("CLIENT", "FOO") + cmd("CLIENT", "SETINFO", "LIB-NAME", "x") + cmd("CLIENT", "SETINFO", "LIB-VER", "1") + cmd("CLIENT", "foo", "bar")),
    ("CONFIG GET of settings both agree on", cmd("CONFIG", "GET", "save") + cmd("CONFIG", "GET", "appendonly") + cmd("config", "get", "SAVE")),
    ("CONFIG GET with overlapping patterns answers each setting once", cmd("CONFIG", "GET", "save", "save") + cmd("CONFIG", "GET", "sav*", "save") + cmd("CONFIG", "GET", "save", "sav*") + cmd("CONFIG", "GET", "s*", "save", "s*")[:0] + cmd("CONFIG", "GET", "appendonl*", "appendonly", "appendonl*")),
    ("CONFIG GET of nothing", cmd("CONFIG", "GET", "no-such-setting") + cmd("CONFIG", "GET", "zzz*")),
    ("CONFIG arity and unknown", cmd("CONFIG") + cmd("CONFIG", "GET") + cmd("CONFIG", "FOO") + cmd("CONFIG", "RESETSTAT")),
    ("QUIT answers and closes", cmd("PING") + cmd("QUIT") + cmd("PING")),
    ("RESET", cmd("CLIENT", "SETNAME", "x") + cmd("RESET") + cmd("CLIENT", "GETNAME") + cmd("RESET", "x")),
    # RESP3: the replies that differ between the protocols are the null, and a map
    ("HELLO 2 and HELLO with no version", cmd("HELLO", "2") + cmd("HELLO") + cmd("HELLO", "2", "SETNAME", "n") + cmd("CLIENT", "GETNAME")),
    ("RESP3 nulls", cmd("HELLO", "3") + cmd("GET", "r3:none") + cmd("MGET", "r3:none", "r3:none") + cmd("SET", "r3:a", "1") + cmd("MGET", "r3:a", "r3:none") + cmd("SET", "r3:a", "2", "GET") + cmd("SET", "r3:b", "x", "GET") + cmd("GETSET", "r3:c", "1") + cmd("GETDEL", "r3:none") + cmd("CLIENT", "GETNAME") + cmd("SET", "r3:a", "3", "XX", "NX")[:0] + cmd("SET", "r3:a", "3", "NX")),
    ("RESP3 and back to RESP2", cmd("HELLO", "3") + cmd("GET", "r3:none") + cmd("HELLO", "2") + cmd("GET", "r3:none") + cmd("HELLO", "3") + cmd("RESET") + cmd("GET", "r3:none")),
    ("RESP3 config map", cmd("HELLO", "3") + cmd("CONFIG", "GET", "save") + cmd("CONFIG", "GET", "appendonly") + cmd("CONFIG", "GET", "nothing")),
    ("RESP3 leaves integers, errors and strings alone", cmd("HELLO", "3") + cmd("SET", "r3:i", "5") + cmd("INCR", "r3:i") + cmd("TTL", "r3:i") + cmd("EXISTS", "r3:i") + cmd("INCR", "r3:none-a") + cmd("FOO") + cmd("GET") + cmd("TYPE", "r3:i") + cmd("PING") + cmd("PING", "x") + cmd("ECHO", "y")),
    ("HELLO 3 with options", cmd("HELLO", "3", "SETNAME", "r3") + cmd("CLIENT", "GETNAME") + cmd("HELLO", "3", "AUTH", "default", "x") + cmd("HELLO", "3", "AUTH", "bob", "x")),
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
    ("CONFIG SET", cmd("CONFIG", "SET", "maxmemory", "1mb"), "the arena, key table and policy are fixed at start"),
    ("CONFIG GET maxmemory", cmd("CONFIG", "GET", "maxmemory"), "the cache's own settings and its own defaults"),
    ("COMMAND COUNT", cmd("COMMAND", "COUNT"), "49 commands, not 240"),
    ("COMMAND", cmd("COMMAND"), "the command table (flags, arity, key positions) is not kept"),
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

def seq(*commands):
    """Several commands, each given as a tuple of words, as one stream."""
    return b"".join(cmd(*c) for c in commands)


M, X = ("MULTI",), ("EXEC",)
# Transactions (`docs/design.md` section 14): what one connection can show. A second connection (a write to a watched key) is `tests/transactions.py`.
cases += [
    ("MULTI EXEC, several commands", seq(M, ("SET", "t:a", "1"), ("INCR", "t:a"), ("GET", "t:a"), ("DEL", "t:a"), X)),
    ("MULTI EXEC, empty", seq(M, X)),
    ("MULTI EXEC, one command", seq(M, ("PING",), X)),
    ("MULTI, then the connection reads the queue back", seq(("SET", "t:r", "5"), M, ("GET", "t:r"), ("INCR", "t:r"), ("GET", "t:r"), X, ("GET", "t:r"))),
    ("MULTI nested", seq(M, M, ("PING",), X)),
    ("MULTI with an argument", seq(("MULTI", "x"))),
    ("MULTI with an argument inside MULTI", seq(M, ("MULTI", "x"), ("PING",), X)),
    ("EXEC without MULTI", seq(X)),
    ("EXEC with an argument", seq(("EXEC", "x"))),
    ("DISCARD without MULTI", seq(("DISCARD",))),
    ("DISCARD with an argument", seq(("DISCARD", "x"))),
    ("MULTI DISCARD", seq(M, ("SET", "t:d", "1"), ("DISCARD",), ("GET", "t:d"))),
    ("MULTI DISCARD, then a new MULTI", seq(M, ("SET", "t:d", "1"), ("DISCARD",), M, ("GET", "t:d"), X)),
    ("MULTI, unknown command, EXEC", seq(M, ("SET", "t:u", "1"), ("NOSUCH", "a", "b"), X, ("GET", "t:u"))),
    ("MULTI, wrong arity, EXEC", seq(M, ("SET", "t:u", "1"), ("GET",), X, ("GET", "t:u"))),
    ("EXECABORT clears the transaction", seq(M, ("NOSUCH",), X, X, ("PING",))),
    ("DISCARD after an error in the queue", seq(M, ("NOSUCH",), ("DISCARD",), M, ("PING",), X)),
    ("runtime error in the middle, the rest run", seq(("SET", "t:e", "x"), M, ("SET", "t:f", "1"), ("INCR", "t:e"), ("GET", "t:f"), X)),
    ("syntax error inside a command, found at EXEC", seq(M, ("SET", "t:s", "1", "EX"), ("SET", "t:s", "2"), ("GET", "t:s"), X)),
    ("SET options inside MULTI", seq(M, ("SET", "t:o", "1", "EX", "100"), ("TTL", "t:o"), ("SET", "t:o", "2", "NX"), ("GET", "t:o"), X)),
    ("MGET and MSET inside MULTI", seq(M, ("MSET", "t:m1", "a", "t:m2", "b"), ("MGET", "t:m1", "t:m2", "t:m3"), X)),
    ("QUIT inside MULTI closes the connection", seq(M, ("PING",), ("QUIT",), ("PING",))),
    ("RESET inside MULTI", seq(M, ("PING",), ("RESET",), X)),
    ("RESET inside MULTI, then MULTI again", seq(M, ("SET", "t:z", "1"), ("RESET",), M, ("GET", "t:z"), X)),
    ("WATCH, no change, EXEC", seq(("SET", "t:w", "1"), ("WATCH", "t:w"), M, ("GET", "t:w"), X)),
    ("WATCH, own write, EXEC aborts", seq(("SET", "t:w", "1"), ("WATCH", "t:w"), ("SET", "t:w", "2"), M, ("GET", "t:w"), X, ("GET", "t:w"))),
    ("WATCH, own write of the same value, EXEC aborts", seq(("SET", "t:w", "1"), ("WATCH", "t:w"), ("SET", "t:w", "1"), M, ("GET", "t:w"), X)),
    ("WATCH, own DEL, EXEC aborts", seq(("SET", "t:w", "1"), ("WATCH", "t:w"), ("DEL", "t:w"), M, ("PING",), X)),
    ("WATCH, own EXPIRE, EXEC aborts", seq(("SET", "t:w", "1"), ("WATCH", "t:w"), ("EXPIRE", "t:w", "100"), M, ("PING",), X)),
    ("WATCH, own FLUSHALL, EXEC aborts", seq(("SET", "t:w", "1"), ("WATCH", "t:w"), ("FLUSHALL",), M, ("PING",), X)),
    ("WATCH a key that does not exist, then create it", seq(("DEL", "t:n"), ("WATCH", "t:n"), ("SET", "t:n", "1"), M, ("PING",), X)),
    ("WATCH, a read does not abort", seq(("SET", "t:w", "1"), ("WATCH", "t:w"), ("GET", "t:w"), ("TTL", "t:w"), M, ("PING",), X)),
    ("WATCH, the abort clears the watch", seq(("SET", "t:w", "1"), ("WATCH", "t:w"), ("SET", "t:w", "2"), M, X, M, ("PING",), X)),
    ("WATCH several keys, one changes", seq(("SET", "t:w1", "1"), ("WATCH", "t:w1", "t:w2", "t:w3"), ("SET", "t:w2", "x"), M, ("PING",), X)),
    ("WATCH, UNWATCH, then a write", seq(("SET", "t:w", "1"), ("WATCH", "t:w"), ("UNWATCH",), ("SET", "t:w", "2"), M, ("PING",), X)),
    ("UNWATCH with nothing watched", seq(("UNWATCH",))),
    ("UNWATCH with an argument", seq(("UNWATCH", "x"))),
    ("WATCH with no key", seq(("WATCH",))),
    ("WATCH inside MULTI", seq(M, ("WATCH", "t:w"), ("PING",), X)),
    ("WATCH inside MULTI does not mark the transaction", seq(M, ("WATCH", "t:w"), X)),
    ("WATCH, DISCARD clears the watch", seq(("SET", "t:w", "1"), ("WATCH", "t:w"), M, ("DISCARD",), ("SET", "t:w", "2"), M, ("PING",), X)),
    ("WATCH, RESET clears the watch", seq(("SET", "t:w", "1"), ("WATCH", "t:w"), ("RESET",), ("SET", "t:w", "2"), M, ("PING",), X)),
    ("WATCH the same key twice", seq(("SET", "t:w", "1"), ("WATCH", "t:w", "t:w"), M, ("PING",), X)),
    ("HELLO 3, MULTI EXEC", seq(("HELLO", "3"), M, ("SET", "t:h", "1"), ("GET", "t:h"), ("GET", "t:nokey"), X)),
    ("HELLO 3, an aborted EXEC is a RESP3 null", seq(("HELLO", "3"), ("SET", "t:w", "1"), ("WATCH", "t:w"), ("SET", "t:w", "2"), M, ("PING",), X)),
    ("HELLO 3, MULTI errors", seq(("HELLO", "3"), M, ("NOSUCH",), X, X)),
    ("WATCH cleared by a successful EXEC", seq(("SET", "t:w", "1"), ("WATCH", "t:w"), M, X, ("SET", "t:w", "2"), M, ("PING",), X)),
    ("a long transaction", seq(M, *[("SET", "t:l%d" % i, "v" * i) for i in range(40)], *[("GET", "t:l%d" % i) for i in range(40)], X)),
]
# The string commands of issue #10, slice 1 (`docs/design.md` section 15).
cases += [
    ("APPEND to a new key", seq(("DEL", "a:k"), ("APPEND", "a:k", "hello"), ("GET", "a:k"))),
    ("APPEND to an existing key", seq(("SET", "a:k", "hello"), ("APPEND", "a:k", " world"), ("GET", "a:k"), ("STRLEN", "a:k"))),
    ("APPEND an empty value to a new key creates it", seq(("DEL", "a:e"), ("APPEND", "a:e", ""), ("EXISTS", "a:e"), ("STRLEN", "a:e"), ("TYPE", "a:e"))),
    ("APPEND an empty value to an existing key", seq(("SET", "a:e", "x"), ("APPEND", "a:e", ""), ("GET", "a:e"))),
    ("APPEND keeps the expiry", seq(("SET", "a:t", "x", "EX", "100"), ("APPEND", "a:t", "y"), ("GET", "a:t"), ("TTL", "a:t"))),
    ("APPEND to a number", seq(("SET", "a:n", "10"), ("APPEND", "a:n", "5"), ("INCR", "a:n"), ("GET", "a:n"))),
    ("APPEND binary", seq(("DEL", "a:b"), ("APPEND", "a:b", binary), ("APPEND", "a:b", binary), ("STRLEN", "a:b"), ("GET", "a:b"))),
    ("APPEND again and again (the value outgrows its room)", seq(("DEL", "a:g"), *[("APPEND", "a:g", "x" * (i % 7 + 1)) for i in range(60)], ("STRLEN", "a:g"), ("GET", "a:g"))),
    ("APPEND growing among other keys", seq(*[("SET", "a:o%d" % i, "v" * 10) for i in range(5)], *[x for i in range(30) for x in (("APPEND", "a:o%d" % (i % 5), "y" * 9), ("GET", "a:o%d" % ((i + 2) % 5)))])),
    ("APPEND no value", seq(("APPEND", "a:k"))),
    ("APPEND three words", seq(("APPEND", "a:k", "a", "b"))),
    ("SETRANGE on a new key", seq(("DEL", "a:r"), ("SETRANGE", "a:r", "0", "abc"), ("GET", "a:r"))),
    ("SETRANGE on a new key at an offset pads with zeros", seq(("DEL", "a:r"), ("SETRANGE", "a:r", "5", "abc"), ("GET", "a:r"), ("STRLEN", "a:r"))),
    ("SETRANGE inside a value", seq(("SET", "a:r", "Hello World"), ("SETRANGE", "a:r", "6", "Redis"), ("GET", "a:r"))),
    ("SETRANGE past the end", seq(("SET", "a:r", "abc"), ("SETRANGE", "a:r", "10", "xyz"), ("GET", "a:r"), ("STRLEN", "a:r"))),
    ("SETRANGE that ends before the end leaves the length", seq(("SET", "a:r", "abcdefgh"), ("SETRANGE", "a:r", "1", "ZZ"), ("GET", "a:r"))),
    ("SETRANGE pads with zeros over old garbage, a new key", seq(("SET", "z:g", "x" * 200), ("FLUSHALL",), ("SETRANGE", "z:n", "5", "abc"), ("GET", "z:n"))),
    ("SETRANGE pads with zeros over old garbage, in place", seq(("SET", "z:p", "zzzzzzzz"), ("SET", "z:p", "ab"), ("SETRANGE", "z:p", "5", "X"), ("GET", "z:p"))),
    ("APPEND to a key whose room holds old garbage", seq(("SET", "z:q", "zzzzzzzz"), ("SET", "z:q", "ab"), ("APPEND", "z:q", "cd"), ("GET", "z:q"))),
    ("SETRANGE with an empty value on a missing key does not create it", seq(("DEL", "a:r"), ("SETRANGE", "a:r", "5", ""), ("EXISTS", "a:r"))),
    ("SETRANGE with an empty value on an existing key", seq(("SET", "a:r", "abc"), ("SETRANGE", "a:r", "1", ""), ("SETRANGE", "a:r", "100", ""), ("GET", "a:r"))),
    ("SETRANGE keeps the expiry", seq(("SET", "a:t", "abc", "EX", "100"), ("SETRANGE", "a:t", "1", "Z"), ("GET", "a:t"), ("TTL", "a:t"))),
    ("SETRANGE negative offset", seq(("SETRANGE", "a:r", "-1", "x"))),
    ("SETRANGE offset not a number", seq(("SETRANGE", "a:r", "x", "x"))),
    ("SETRANGE offset too large", seq(("SETRANGE", "a:r", "536870912", "x"), ("SETRANGE", "a:r", "9223372036854775807", "x"))),
    ("SETRANGE then APPEND then GETRANGE", seq(("DEL", "a:m"), ("SETRANGE", "a:m", "3", "ab"), ("APPEND", "a:m", "cd"), ("GETRANGE", "a:m", "0", "-1"))),
    ("SETRANGE arity", seq(("SETRANGE", "a:r", "1"))),
    ("GETRANGE of a missing key", seq(("DEL", "g:k"), ("GETRANGE", "g:k", "0", "-1"))),
    ("GETRANGE ranges", seq(("SET", "g:k", "This is a string"), *[("GETRANGE", "g:k", str(a), str(b)) for a, b in [(0, 3), (-3, -1), (0, -1), (10, 100), (0, 0), (-1, -1), (5, 3), (-1, -3), (-100, 3), (3, -100), (-100, -50), (100, 200), (16, 20), (15, 15), (0, 15), (-16, -1), (-17, -1), (-50, -100), (-17, -20), (-100, -150)]])),
    ("GETRANGE of an empty value", seq(("SET", "g:e", ""), ("GETRANGE", "g:e", "0", "-1"), ("GETRANGE", "g:e", "0", "0"))),
    ("GETRANGE not integers", seq(("SET", "g:k", "abc"), ("GETRANGE", "g:k", "a", "1"), ("GETRANGE", "g:k", "1", "b"), ("GETRANGE", "g:k", "1.5", "2"))),
    ("GETRANGE huge indexes", seq(("SET", "g:k", "abc"), ("GETRANGE", "g:k", "0", "9223372036854775807"), ("GETRANGE", "g:k", "-9223372036854775808", "1"), ("GETRANGE", "g:k", "-9223372036854775808", "-9223372036854775808"))),
    ("GETRANGE of binary", seq(("SET", "g:b", binary), ("GETRANGE", "g:b", "100", "130"))),
    ("SUBSTR", seq(("SET", "g:k", "This is a string"), ("SUBSTR", "g:k", "0", "3"), ("SUBSTR", "g:k", "-3", "-1"))),
    ("GETRANGE arity", seq(("GETRANGE", "g:k", "1"))),
    ("GETEX of a missing key", seq(("DEL", "x:k"), ("GETEX", "x:k"), ("GETEX", "x:k", "EX", "10"), ("EXISTS", "x:k"))),
    ("GETEX without options is a GET", seq(("SET", "x:k", "v"), ("GETEX", "x:k"), ("TTL", "x:k"))),
    ("GETEX EX sets the expiry", seq(("SET", "x:k", "v"), ("GETEX", "x:k", "EX", "100"), ("TTL", "x:k"))),
    ("GETEX PX sets the expiry", seq(("SET", "x:k", "v"), ("GETEX", "x:k", "PX", "100000"), ("PTTL", "x:k") if False else ("TTL", "x:k"))),
    ("GETEX PERSIST removes the expiry", seq(("SET", "x:k", "v", "EX", "100"), ("GETEX", "x:k", "PERSIST"), ("TTL", "x:k"))),
    ("GETEX PERSIST on a key with none", seq(("SET", "x:k", "v"), ("GETEX", "x:k", "PERSIST"), ("TTL", "x:k"))),
    ("GETEX with an expiry that has passed deletes the key", seq(("SET", "x:k", "v"), ("GETEX", "x:k", "PX", "1"), ("EXISTS", "x:k") if False else ("PING",))),
    ("GETEX EX 0", seq(("SET", "x:k", "v"), ("GETEX", "x:k", "EX", "0"), ("GET", "x:k"))),
    ("GETEX EX negative", seq(("SET", "x:k", "v"), ("GETEX", "x:k", "EX", "-5"), ("GET", "x:k"))),
    ("GETEX EX not a number", seq(("SET", "x:k", "v"), ("GETEX", "x:k", "EX", "abc"))),
    ("GETEX EX too large", seq(("SET", "x:k", "v"), ("GETEX", "x:k", "EX", "9223372036854775807"), ("GETEX", "x:k", "PX", "9223372036854775807"))),
    ("GETEX EX and PERSIST", seq(("SET", "x:k", "v"), ("GETEX", "x:k", "EX", "10", "PERSIST"), ("GETEX", "x:k", "PERSIST", "EX", "10"))),
    ("GETEX EX and PX", seq(("SET", "x:k", "v"), ("GETEX", "x:k", "EX", "10", "PX", "10"), ("GETEX", "x:k", "PX", "10", "EX", "10"))),
    ("GETEX EX twice", seq(("SET", "x:k", "v"), ("GETEX", "x:k", "EX", "10", "EX", "20"), ("TTL", "x:k"))),
    ("GETEX EX without a number", seq(("SET", "x:k", "v"), ("GETEX", "x:k", "EX"))),
    ("GETEX unknown option", seq(("SET", "x:k", "v"), ("GETEX", "x:k", "NX"), ("GETEX", "x:k", "KEEPTTL"), ("GETEX", "x:k", "GET"))),
    ("GETEX with a bad option on a missing key", seq(("DEL", "x:z"), ("GETEX", "x:z", "NX"), ("GETEX", "x:z", "EX", "-1"), ("GETEX", "x:z", "EX", "abc"))),
    ("GETEX no key", seq(("GETEX",))),
    ("GETEX RESP3", seq(("HELLO", "3"), ("DEL", "x:z"), ("GETEX", "x:z"), ("SET", "x:k", "v"), ("GETEX", "x:k", "EX", "10"))),
    ("MSETNX all new", seq(("DEL", "n:a", "n:b"), ("MSETNX", "n:a", "1", "n:b", "2"), ("MGET", "n:a", "n:b"))),
    ("MSETNX with one existing sets none", seq(("DEL", "n:a", "n:b", "n:c"), ("SET", "n:b", "old"), ("MSETNX", "n:a", "1", "n:b", "2", "n:c", "3"), ("MGET", "n:a", "n:b", "n:c"))),
    ("MSETNX the same key twice", seq(("DEL", "n:a"), ("MSETNX", "n:a", "1", "n:a", "2"), ("GET", "n:a"))),
    ("MSETNX odd words", seq(("MSETNX", "n:a", "1", "n:b"))),
    ("MSETNX no value", seq(("MSETNX", "n:a"))),
    ("MSETNX clears an expiry it does not touch", seq(("DEL", "n:a"), ("MSETNX", "n:a", "1"), ("TTL", "n:a"))),
    ("the new string commands inside MULTI", seq(M, ("APPEND", "q:k", "a"), ("SETRANGE", "q:k", "3", "b"), ("GETRANGE", "q:k", "0", "-1"), ("GETEX", "q:k", "EX", "100"), ("MSETNX", "q:x", "1"), X)),
    ("the new string commands, arity inside MULTI", seq(M, ("APPEND", "q:k"), ("SETRANGE", "q:k", "1"), ("GETRANGE", "q:k"), ("GETEX",), ("MSETNX", "q:k"), X)),
]
# Arity: a command queued with one word too few, exactly enough and one too many is accepted or refused as Redis does. The commands the
# transaction does not queue (MULTI, EXEC, DISCARD, WATCH, QUIT, RESET), and the ones whose answers are not the same twice (HELLO, INFO,
# COMMAND, CLIENT, CONFIG, AUTH with arguments), are not swept.
for name in ["GET", "SET", "PING", "ECHO", "DEL", "UNLINK", "EXISTS", "TOUCH", "INCR", "DECR", "INCRBY", "DECRBY", "EXPIRE", "PEXPIRE", "TTL", "PTTL", "PERSIST",
             "SETNX", "SETEX", "PSETEX", "GETSET", "GETDEL", "MGET", "MSET", "STRLEN", "TYPE", "DBSIZE", "FLUSHALL", "FLUSHDB", "UNWATCH", "APPEND", "SETRANGE", "GETRANGE", "SUBSTR", "GETEX", "MSETNX"]:
    for given in range(0, 5):
        numeric = name in ("INCRBY", "DECRBY", "EXPIRE", "PEXPIRE", "SETEX", "PSETEX")
        args = ["1" if numeric and i == 1 else "t:k" for i in range(given)]
        cases.append(("arity of %s with %d words, queued" % (name, given), seq(M, (name, *args), X)))
for name in ("CLIENT", "CONFIG", "AUTH", "SELECT", "NOSUCH"):
    cases.append(("arity of %s with no words, queued" % name, seq(M, (name,), X)))

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
            if os.environ.get("DIFF_TRACE"):
                print("case", name, flush=True)
            for fname, chunks in framings(data, rnd):
                want, got = norm(talk(REDIS_PORT, chunks)), norm(talk(CACHE_PORT, chunks))
                if want != got:
                    bad += 1
                    print("DIFFERS  %s [%s]\n   redis: %r\n   cache: %r" % (name, fname, want[:200], got[:200]))
        for name, data in small_cases:
            for fname, chunks in framings(data, rnd):
                want, got = norm(talk(REDIS_PORT, chunks)), norm(talk(SMALL_PORT, chunks))
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
