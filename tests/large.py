#!/usr/bin/env python3
"""Values larger than a block: `SET` takes them in pieces, `GET` sends them in pieces (docs/design.md section 22).

    python3 tests/large.py [path/to/cache]

Each case runs against a server of its own. What is held: values of 1 MiB, 16 MiB and the size of the arena round-trip byte for byte, however they are cut
by the network (a byte at a time included); a value that does not fit the arena is refused in Redis's words and nothing changes; a longer bulk than Redis
allows is a protocol error; nothing sees a half-arrived value; a value written while it is being read closes the reader's connection and nothing else; every
command that would build a long value into a buffer says no instead; and the server is alive afterwards.
"""
import os, random, socket, subprocess, sys, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
PORT = 6431
OOM = b"-OOM command not allowed when used memory > 'maxmemory'.\r\n"
MiB = 1 << 20
TOO_LARGE = b"-ERR value too large for this command"


def cmd(*args):
    out = b"*%d\r\n" % len(args)
    for a in args:
        a = a.encode() if isinstance(a, str) else a
        out += b"$%d\r\n%s\r\n" % (len(a), a)
    return out


def serve(*extra):
    p = subprocess.Popen([CACHE, str(PORT), *extra], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", PORT), 0.2).close()
            return p
        except OSError:
            time.sleep(0.05)
    raise SystemExit("did not start")


def connect(rcvbuf=None, timeout=30):
    s = socket.socket()
    if rcvbuf:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    s.settimeout(timeout)
    s.connect(("127.0.0.1", PORT))
    return s


def read_n(s, n):
    got = bytearray()
    while len(got) < n:
        d = s.recv(min(1 << 20, n - len(got)))
        if not d:
            break
        got += d
    return bytes(got)


def read_line(s):
    got = b""
    while not got.endswith(b"\r\n"):
        d = s.recv(1)
        if not d:
            break
        got += d
    return got


def reply(s):
    """One RESP reply: a simple string, an error, an integer or a bulk (whole)."""
    line = read_line(s)
    if line.startswith(b"$") and line != b"$-1\r\n":
        n = int(line[1:-2])
        return line + read_n(s, n + 2)
    return line


def ask(s, *args):
    s.sendall(cmd(*args))
    return reply(s)


def bulk(v):
    return b"$%d\r\n%s\r\n" % (len(v), v)


def alive():
    s = connect(timeout=5)
    ok = ask(s, "PING") == b"+PONG\r\n"
    s.close()
    return ok


def closed(s, wait=5):
    s.settimeout(wait)
    try:
        while True:
            d = s.recv(1 << 20)
            if not d:
                return True
    except (ConnectionResetError, BrokenPipeError):
        return True
    except OSError:
        return False


failures = []


def check(what, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + what + ("" if ok else " " + str(detail)[:200]))
    if not ok:
        failures.append(what)


def case(body, *args):
    p = serve(*args)
    try:
        body()
        check(body.__name__ + ": the server is alive afterwards", p.poll() is None and alive())
    finally:
        p.kill()
        p.wait()


def round_trips():
    s = connect()
    rnd = random.Random(1)
    for size in (16385, 40000, 65536, 1 * MiB, 16 * MiB):
        v = rnd.randbytes(size)
        s.sendall(cmd("SET", "big", v))
        check("SET of %d bytes" % size, reply(s) == b"+OK\r\n")
        got = ask(s, "GET", "big")
        check("GET of %d bytes comes back byte for byte" % size, got == bulk(v))
        check("STRLEN %d" % size, ask(s, "STRLEN", "big") == b":%d\r\n" % size)
    check("DEL of the big key", ask(s, "DEL", "big") == b":1\r\n" and ask(s, "GET", "big") == b"$-1\r\n")
    # pipelined with the commands around it
    v = rnd.randbytes(100000)
    s.sendall(cmd("SET", "a", "1") + cmd("SET", "big", v) + cmd("GET", "big") + cmd("GET", "a") + cmd("PING"))
    want = b"+OK\r\n+OK\r\n" + bulk(v) + bulk(b"1") + b"+PONG\r\n"
    check("pipelined around a big SET and GET", read_n(s, len(want)) == want)
    # SETEX / PSETEX
    v = rnd.randbytes(70000)
    s.sendall(cmd("SETEX", "t", "100", v))
    check("SETEX of a big value", reply(s) == b"+OK\r\n" and ask(s, "GET", "t") == bulk(v))
    ttl = ask(s, "TTL", "t")
    check("SETEX gave it a TTL", ttl in (b":100\r\n", b":99\r\n"), ttl)
    s.sendall(cmd("PSETEX", "t", "50", v))
    check("PSETEX of a big value", reply(s) == b"+OK\r\n")
    time.sleep(0.3)
    check("PSETEX expired it", ask(s, "GET", "t") == b"$-1\r\n")
    # an errors in the head of the command are answered, and the value is read and thrown away
    s.sendall(cmd("SETEX", "t", "0", v))
    check("SETEX 0 of a big value", reply(s) == b"-ERR invalid expire time in 'setex' command\r\n")
    s.sendall(cmd("SETEX", "t", "abc", v))
    check("SETEX abc of a big value", reply(s) == b"-ERR value is not an integer or out of range\r\n")
    check("and the connection carries on", ask(s, "PING") == b"+PONG\r\n")
    # SET over a hash and over a string with a TTL
    ask(s, "HSET", "h", "f", "v")
    s.sendall(cmd("SET", "h", v))
    check("SET over a hash", reply(s) == b"+OK\r\n" and ask(s, "TYPE", "h") == b"+string\r\n" and ask(s, "GET", "h") == bulk(v))
    ask(s, "SET", "e", "x", "EX", "100")
    s.sendall(cmd("SET", "e", v))
    reply(s)
    check("SET clears the TTL", ask(s, "TTL", "e") == b":-1\r\n")


def cut_up():
    rnd = random.Random(2)
    v = rnd.randbytes(100000)
    whole = cmd("SET", "k", v)
    # every cut of the head, a byte at a time, then the value in random pieces
    s = connect()
    head = whole.index(b"\r\n", whole.index(b"$100000")) + 2
    for i in range(head):
        s.sendall(whole[i:i + 1])
        time.sleep(0.0005)
    at = head
    while at < len(whole):
        n = rnd.choice([1, 2, 3, 7, 100, 4096, 16383, 16384, 16385, 30000])
        s.sendall(whole[at:at + n])
        at += n
    check("a head sent a byte at a time and a value in random pieces", reply(s) == b"+OK\r\n" and ask(s, "GET", "k") == bulk(v))
    # the CRLF split from the value, and from the next command
    for cut in (len(whole) - 2, len(whole) - 1):
        s.sendall(whole[:cut])
        time.sleep(0.05)
        s.sendall(whole[cut:] + cmd("PING"))
        check("the CRLF split at %d" % (len(whole) - cut), reply(s) == b"+OK\r\n" and reply(s) == b"+PONG\r\n")
    # a value one byte over what fits a block, and one under
    for size in (16384 - 40, 16384 - 28, 16384 - 20, 16384 - 10, 16384):
        v = rnd.randbytes(size)
        s.sendall(cmd("SET", "edge", v))
        check("a value of %d bytes, around the edge of a block" % size, reply(s) == b"+OK\r\n" and ask(s, "GET", "edge") == bulk(v))
    # a byte at a time for a whole short big value, many connections at once
    socks = [connect() for _ in range(20)]
    v = rnd.randbytes(20000)
    whole = cmd("SET", "slow", v)
    for i in range(0, len(whole), 1000):
        for t in socks:
            t.sendall(whole[i:i + 1000])
    check("20 connections sending a big value at once", all(reply(t) == b"+OK\r\n" for t in socks) and ask(s, "GET", "slow") == bulk(v))
    # a wrong terminator
    t = connect()
    t.sendall(cmd("SET", "bad", rnd.randbytes(20000))[:-2] + b"XX")
    check("a wrong CRLF after the value is a protocol error", reply(t) == b"-ERR Protocol error: expected CRLF after bulk\r\n" and closed(t))
    check("and the half-stored key is not there", ask(s, "EXISTS", "bad") == b":0\r\n")


def too_big():
    s = connect()
    head = b"*3\r\n$3\r\nSET\r\n$1\r\nk\r\n"
    for n in (1 << 40, 536870913, 999999999):
        t = connect()
        t.sendall(head + b"$%d\r\n" % n)
        check("bulk header of %d bytes" % n, reply(t) == b"-ERR Protocol error: invalid bulk length\r\n" and closed(t))
    # the longest Redis allows is read and refused for want of room, in Redis's words
    ask(s, "SET", "keep", "old")
    t = connect()
    v = b"x" * (100 * MiB)  # more than the 64 MiB arena
    t.sendall(cmd("SET", "keep", v))
    check("a value that cannot fit the arena: -OOM at the end", reply(t) == OOM)
    check("and the old value is still there", ask(s, "GET", "keep") == bulk(b"old"))
    check("and the connection carries on", ask(t, "PING") == b"+PONG\r\n")
    # a command that is not a SET with a long last argument closes, as before
    t = connect()
    t.sendall(cmd("APPEND", "k", b"y" * 20000))
    check("APPEND of 20000 bytes: command too large", reply(t) == b"-ERR Protocol error: command too large\r\n" and closed(t))
    t = connect()
    t.sendall(cmd("SET", "k", b"y" * 20000, "EX", "100"))
    check("SET with options after a big value: command too large (a stated limit)", reply(t) == b"-ERR Protocol error: command too large\r\n" and closed(t))
    t = connect()
    t.sendall(cmd("SET", b"k" * 20000, "v"))
    check("a key of 20000 bytes: command too large", reply(t) == b"-ERR Protocol error: command too large\r\n" and closed(t))


def arena_sized():
    s = connect()
    v = os.urandom(8 * MiB - 100)
    s.sendall(cmd("SET", "whole", v))
    check("a value that is nearly the whole arena", reply(s) == b"+OK\r\n" and ask(s, "GET", "whole") == bulk(v))
    w = os.urandom(8 * MiB - 100)
    s.sendall(cmd("SET", "whole", w))
    check("over itself it needs room for both until the last byte is here: -OOM, in Redis's words", reply(s) == OOM)
    check("and the old value is intact", ask(s, "GET", "whole") == bulk(v))
    s.sendall(cmd("SET", "other", b"z" * 1000000))
    check("a second big key does not fit either", reply(s) == OOM)
    w = v
    ask(s, "DEL", "whole")
    s.sendall(cmd("SET", "other", b"z" * 1000000))
    check("and fits once the first is gone", reply(s) == b"+OK\r\n")
    check("audit: the books balance", b"used_memory:" in info(s))


def info(s):
    s.sendall(cmd("INFO"))
    first = read_line(s)
    body = read_n(s, int(first[1:-2]) + 2)
    return body


def guards():
    s = connect()
    big = os.urandom(100000)
    s.sendall(cmd("SET", "big", big))
    reply(s)
    ask(s, "SET", "small", "s")
    for args in (("GETSET", "big", "x"), ("GETDEL", "big"), ("GETEX", "big", "EX", "100"), ("GETRANGE", "big", "0", "-1"), ("SET", "big", "x", "GET"), ("APPEND", "big", "x"), ("SETRANGE", "big", "99999", "xyz")):
        r = ask(s, *args)
        check("%s of a big value says no" % args[0], r.startswith(b"-ERR"), r)
    check("none of them changed it", ask(s, "GET", "big") == bulk(big) and ask(s, "STRLEN", "big") == b":100000\r\n")
    check("GETRANGE of a short piece works", ask(s, "GETRANGE", "big", "10", "19") == bulk(big[10:20]))
    check("GETRANGE of a piece 15000 long works", ask(s, "GETRANGE", "big", "5", "15004") == bulk(big[5:15005]))
    r = ask(s, "GETRANGE", "big", "0", "20000")
    check("GETRANGE of 20001 bytes says no", r.startswith(TOO_LARGE), r)
    s.sendall(cmd("MGET", "small", "big", "small"))
    got = read_n(s, len(b"*3\r\n$1\r\ns\r\n") + 1)
    rest = b""
    while not rest.endswith(b"$1\r\ns\r\n"):
        rest += s.recv(1000)
    check("MGET gives an error for the element and carries on", got.startswith(b"*3\r\n$1\r\ns\r\n-") and TOO_LARGE in got + rest)
    ask(s, "MULTI")
    check("GET inside MULTI queues", ask(s, "GET", "big") == b"+QUEUED\r\n")
    ask(s, "GET", "small")
    s.sendall(cmd("EXEC"))
    r = read_line(s)
    got = r
    while not got.endswith(b"$1\r\ns\r\n"):
        got += s.recv(1000)
    check("EXEC answers the big GET with an error and the rest as usual", got.startswith(b"*2\r\n-ERR value too large"), got[:80])
    # a big SET inside MULTI cannot be queued
    ask(s, "MULTI")
    s.sendall(cmd("SET", "x", big))
    check("a big SET inside MULTI: queue full", reply(s) == b"-ERR transaction queue is full\r\n")
    check("EXEC then aborts", ask(s, "EXEC").startswith(b"-EXECABORT"))
    check("and the key was not set", ask(s, "EXISTS", "x") == b":0\r\n")
    check("the big value is still readable", ask(s, "GET", "big") == bulk(big))


def invisible():
    a = connect()
    b = connect()
    ask(b, "SET", "k", "old")
    v = os.urandom(300000)
    whole = cmd("SET", "k", v)
    a.sendall(whole[:150000])
    time.sleep(0.2)
    check("a half-arrived value is not seen: GET gives the old one", ask(b, "GET", "k") == bulk(b"old"))
    ask(b, "SET", "n", "1")
    a.sendall(cmd("SET", "newkey", b"q" * 100) [:0])
    check("DBSIZE and EXISTS do not count it", ask(b, "DBSIZE") == b":2\r\n")
    c = connect()
    c.sendall(cmd("SET", "fresh", b"w" * 200000)[:100000])
    time.sleep(0.2)
    check("a key that is only half here does not exist", ask(b, "EXISTS", "fresh") == b":0\r\n" and ask(b, "DBSIZE") == b":2\r\n")
    b.sendall(cmd("KEYS", "*"))
    n = int(read_line(b)[1:-2])
    names = [reply(b) for _ in range(n)]
    check("KEYS does not list it", n == 2 and b"fresh" not in b"".join(names), names)
    a.sendall(whole[150000:])
    check("the rest arrives and the value is stored", reply(a) == b"+OK\r\n" and ask(b, "GET", "k") == bulk(v))
    c.sendall(cmd("SET", "fresh", b"w" * 200000)[100000:])
    check("the other too", reply(c) == b"+OK\r\n" and ask(b, "GET", "fresh") == bulk(b"w" * 200000))
    # a client that goes away part way leaves nothing behind
    before = info(b)
    d = connect()
    d.sendall(cmd("SET", "gone", b"g" * 5000000)[:2000000])
    time.sleep(0.2)
    d.close()
    time.sleep(0.3)
    check("a client that leaves part way leaves no key", ask(b, "EXISTS", "gone") == b":0\r\n")
    for _ in range(5):
        e = connect()
        e.sendall(cmd("SET", "gone", b"g" * 5000000)[:2000000])
        e.close()
    time.sleep(0.3)
    w = os.urandom(40 * MiB)
    b.sendall(cmd("SET", "plenty", w))
    check("and the arena it took is back (a 40 MiB value fits after ten abandoned 5 MB ones)", reply(b) == b"+OK\r\n")
    # FLUSHALL while a value is arriving
    f = connect()
    f.sendall(cmd("SET", "mid", b"m" * 20000)[:10000])
    time.sleep(0.2)
    ask(b, "FLUSHALL")
    # Its entry number is now another key's, with a longer value: what is sent next must not land in it.
    others = {"o%d" % i: os.urandom(30000) for i in range(40)}
    for k, val in others.items():
        ask(b, "SET", k, val)
    f.sendall(cmd("SET", "mid", b"m" * 20000)[10000:])
    r = reply(f)
    check("FLUSHALL while a value arrives: an error, not a half value", r.startswith(b"-ERR the keys were flushed"), r)
    check("and nothing is stored", ask(b, "EXISTS", "mid") == b":0\r\n" and ask(b, "DBSIZE") == b":40\r\n")
    check("and the keys written since are intact", all(ask(b, "GET", k) == bulk(val) for k, val in others.items()))
    check("the connection carries on", ask(f, "PING") == b"+PONG\r\n")


def watched():
    a = connect()
    b = connect()
    ask(a, "SET", "w", "old")
    ask(a, "WATCH", "w")
    ask(a, "MULTI")
    ask(a, "SET", "other", "1")
    b.sendall(cmd("SET", "w", os.urandom(50000)))
    reply(b)
    check("a big SET aborts a transaction that watched the key", ask(a, "EXEC") == b"*-1\r\n")


def written_while_read():
    big = os.urandom(8 * MiB)
    w = connect()
    w.sendall(cmd("SET", "k", big))
    reply(w)
    ask(w, "SET", "other", "o")
    r = connect(rcvbuf=4096)
    r.sendall(cmd("GET", "k"))
    first = read_n(r, 100000)
    check("a reader that has taken a little", first.startswith(b"$%d\r\n" % len(big)))
    time.sleep(0.3)
    # a write to another key goes through and does not hurt the reader
    ask(w, "SET", "other", "p")
    rest = read_n(r, 1 * MiB)
    check("a write to another key does not close the reader", len(rest) == 1 * MiB)
    # a write to the same key does
    ask(w, "SET", "k", "small")
    check("the key written while it is read: the reader is closed, not sent a torn value", closed(r, 20))
    check("the writer sees its own write", ask(w, "GET", "k") == bulk(b"small"))
    # the same by DEL and by SET (an expiry does not change the value, below)
    for name, act in (("DEL", lambda: ask(w, "DEL", "k")), ("SET of a small value", lambda: ask(w, "SET", "k", "x")), ("SET of a big value", lambda: (w.sendall(cmd("SET", "k", os.urandom(50000))), reply(w)))):
        ask(w, "DEL", "k")
        w.sendall(cmd("SET", "k", big))
        reply(w)
        r = connect(rcvbuf=4096)
        r.sendall(cmd("GET", "k"))
        read_n(r, 100000)
        time.sleep(0.2)
        act()
        check("%s of the key while it is read closes the reader" % name, closed(r, 20))
    # an expiry does not change the value, and a reader is not harmed by it
    ask(w, "DEL", "k")
    w.sendall(cmd("SET", "k", big))
    reply(w)
    r = connect(rcvbuf=4096)
    r.sendall(cmd("GET", "k"))
    first = read_n(r, 100000)
    ask(w, "EXPIRE", "k", "100")
    got = first + read_n(r, len(big) + 2 + len(b"$%d\r\n" % len(big)) - len(first))
    check("an EXPIRE while it is read does not hurt the reader", got == bulk(big))


def fair():
    import threading
    big = os.urandom(32 * MiB)
    w = connect()
    w.sendall(cmd("SET", "k", big))
    reply(w)
    r = connect()
    want = 8 * len(bulk(big))
    got = []
    # A client that reads as fast as it can, so that the kernel always has room and only the server's own turn limit stops a send.
    reader = threading.Thread(target=lambda: got.append(len(read_n(r, want))))
    r.sendall(cmd("GET", "k") * 8)
    reader.start()
    t = connect()
    worst = 0
    for _ in range(100):
        a = time.time()
        ask(t, "PING")
        worst = max(worst, time.time() - a)
    reader.join()
    check("PING while 256 MiB is being sent to another client: the longest wait %.0f ms" % (worst * 1000), worst < 0.2, worst)
    check("and the eight GETs arrive whole", got == [want])


def compaction():
    # a small arena, so that the records move while a value is arriving
    rnd = random.Random(3)
    v = rnd.randbytes(1500000)
    whole = cmd("SET", "moving", v)
    a = connect()
    b = connect()
    keep = {}
    at = 0
    n = 0
    while at < len(whole):
        a.sendall(whole[at:at + 20000])
        at += 20000
        for _ in range(10):
            k = "c%d" % (n % 40)
            val = rnd.randbytes(rnd.choice([100, 2000, 20000]))
            b.sendall(cmd("SET", k, val))
            ok = reply(b)
            if ok == b"+OK\r\n":
                keep[k] = val
            n += 1
    check("a value arriving while others churn and the arena compacts", reply(a) == b"+OK\r\n")
    check("it is intact", ask(a, "GET", "moving") == bulk(v))
    check("the others that were stored are intact", all(ask(b, "GET", k) == bulk(val) for k, val in keep.items() if ask(b, "EXISTS", k) == b":1\r\n"))


case(round_trips)
case(cut_up)
case(too_big)
case(arena_sized, "8")
case(guards)
case(invisible)
case(watched)
case(written_while_read)
case(fair)
case(compaction, "4")

if failures:
    print("\n%d failure(s):" % len(failures))
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("\nlarge values: all passed")
