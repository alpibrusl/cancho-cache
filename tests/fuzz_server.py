#!/usr/bin/env python3
"""Hostile bytes into the running cache: it must never die, and must still answer PING afterwards.

    python3 tests/fuzz_server.py [path/to/cache] [rounds]

Three sources, all seeded: pure random bytes, valid commands with a few bytes flipped, and valid commands cut at a
random point and followed by more. A trap would show as the process ending (SIGILL); a hang as a PING that does not
come back. The parser itself is also swept over every short string by `tests/resp_test.ls`; this is the loop around it.
"""
import os, random, socket, subprocess, sys, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
PORT = 6394
rnd = random.Random(7)


def cmd(*args):
    out = b"*%d\r\n" % len(args)
    for a in args:
        a = a.encode() if isinstance(a, str) else a
        out += b"$%d\r\n%s\r\n" % (len(a), a)
    return out


valid = [cmd("PING"), cmd("PING", "x"), cmd("ECHO", "hello"), cmd("ECHO", bytes(range(256))), cmd("FOO", "a", "b"), b"*0\r\n", b"*-1\r\n",
         cmd("SET", "k", "v", "EX", "10", "NX", "GET"), cmd("SET", "k", "9223372036854775807"), cmd("INCR", "k"), cmd("INCRBY", "k", "-9223372036854775808"),
         cmd("EXPIRE", "k", "9223372036854775807", "GT"), cmd("PEXPIRE", "k", "-9223372036854775808"), cmd("TTL", "k"), cmd("MSET", "a", "1", "b", "2"),
         cmd("MGET", "a", "b", "c"), cmd("GETSET", "k", "v"), cmd("SETEX", "k", "1", "v"), cmd("DEL", "a", "b"), cmd("FLUSHALL"),
         cmd("HELLO", "3", "AUTH", "default", "x", "SETNAME", "n"), cmd("HELLO", "2"), cmd("HELLO", "9999999999999999999"), cmd("AUTH", "a", "b"), cmd("CLIENT", "SETNAME", "x" * 200),
         cmd("CLIENT", "GETNAME"), cmd("CLIENT", "ID"), cmd("INFO"), cmd("INFO", "memory"), cmd("CONFIG", "GET", "*"), cmd("CONFIG", "GET", "a", "b", "c", "max*", "[", "?"),
         cmd("COMMAND"), cmd("COMMAND", "LIST"), cmd("COMMAND", "COUNT"), cmd("RESET"), cmd("QUIT")]


def sample():
    k = rnd.random()
    if k < 0.34:
        return bytes(rnd.randrange(256) for _ in range(rnd.randint(0, 200)))
    v = rnd.choice(valid) * rnd.randint(1, 4)
    if k < 0.67:
        b = bytearray(v)
        for _ in range(rnd.randint(1, 4)):
            if b:
                b[rnd.randrange(len(b))] = rnd.randrange(256)
        return bytes(b)
    return v[: rnd.randint(0, len(v))] + rnd.choice(valid)


def ping_ok():
    s = socket.create_connection(("127.0.0.1", PORT), 2)
    s.sendall(cmd("PING"))
    s.settimeout(2)
    ok = s.recv(100) == b"+PONG\r\n"
    s.close()
    return ok


p = subprocess.Popen([CACHE, str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", PORT), 0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    for i in range(ROUNDS):
        s = socket.create_connection(("127.0.0.1", PORT), 2)
        try:
            s.sendall(sample())
            s.settimeout(0.01)
            try:
                s.recv(65536)
            except (socket.timeout, OSError):
                pass
        except OSError:
            pass
        s.close()
        if p.poll() is not None:
            print("DIED after %d rounds, status %s" % (i, p.returncode))
            sys.exit(1)
        if i % 500 == 499 and not ping_ok():
            print("no PONG after %d rounds" % (i + 1))
            sys.exit(1)
    if not ping_ok():
        print("no PONG at the end")
        sys.exit(1)
    print("%d hostile connections, still alive, still answering" % ROUNDS)
finally:
    p.terminate()
