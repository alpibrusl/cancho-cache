#!/usr/bin/env python3
"""Many connections: 10,000 of them, and what happens when a pool of lent buffers runs out.

    python3 tests/connections.py [path/to/cache]

Buffers are lent, not owned (docs/design.md section 21): an idle connection holds none. Five cases, each on its own
server: 10,000 idle connections that all answer PING (and the 10,001st is refused), RSS per idle connection,
a thousand half-commands finished later (and byte by byte), more half-commands than input blocks (extras closed, the
rest finish, blocks come back), more MULTI queues than queue blocks (the extras are refused, EXEC works), and
non-reading clients that exhaust the output pool (then a second wave proves the blocks were returned).
"""
import os, socket, subprocess, sys, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
PORT = 6411


def cmd(*args):
    out = b"*%d\r\n" % len(args)
    for a in args:
        a = a.encode() if isinstance(a, str) else a
        out += b"$%d\r\n%s\r\n" % (len(a), a)
    return out


def serve():
    p = subprocess.Popen([CACHE, str(PORT)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", PORT), 0.2).close()
            return p
        except OSError:
            time.sleep(0.05)
    raise SystemExit("did not start")


def rss_kb(pid):
    for line in open("/proc/%d/status" % pid):
        if line.startswith("VmRSS:"):
            return int(line.split()[1])


def connect(timeout=5, rcvbuf=None):
    s = socket.socket()
    if rcvbuf:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    s.settimeout(timeout)
    s.connect(("127.0.0.1", PORT))
    return s


def read_until(s, n):
    got = b""
    while len(got) < n:
        d = s.recv(65536)
        if not d:
            return got
        got += d
    return got


def ping(s):
    s.sendall(cmd("PING"))
    try:
        return read_until(s, 7) == b"+PONG\r\n"
    except OSError:
        return False


def info(field):
    s = connect()
    s.sendall(cmd("INFO"))
    got = b""
    while b"\r\n\r\n" not in got and not got.endswith(b"\r\n") or field.encode() not in got:
        d = s.recv(65536)
        if not d:
            break
        got += d
    s.close()
    for line in got.decode().split("\r\n"):
        if line.startswith(field + ":"):
            return int(line.split(":")[1])
    raise SystemExit("INFO has no " + field)


def wait_clients(want, limit=10):
    end = time.time() + limit
    while time.time() < end:
        if info("connected_clients") <= want:
            return True
        time.sleep(0.1)
    return False


failures = []


def check(what, ok, detail=""):
    if not ok:
        failures.append("%s %s" % (what, detail))
    print(("ok   " if ok else "FAIL ") + what, detail if not ok else "")


def case(body):
    p = serve()
    try:
        body(p)
        check(body.__name__ + ": server alive afterwards", p.poll() is None)
    finally:
        p.kill()
        p.wait()


def idle_connections(p):
    base = rss_kb(p.pid)
    socks = []
    for i in range(10000):
        try:
            socks.append(connect())
        except OSError:
            break
    # One connection is the INFO probe's; it comes and goes, so ask for 10,000 + at most that one.
    check("10,000 connections accepted", len(socks) == 10000, "got %d" % len(socks))
    for s in socks:
        s.sendall(cmd("PING"))
    ok = all(read_until(s, 7) == b"+PONG\r\n" for s in socks)
    check("all 10,000 idle connections answer PING", ok)
    grown = rss_kb(p.pid) - base
    print("     RSS grew %d KiB for %d connections: %.0f bytes each" % (grown, len(socks), grown * 1024 / len(socks)))
    extra = connect()
    extra.sendall(cmd("PING"))
    try:
        refused = read_until(extra, 7) != b"+PONG\r\n"
    except OSError:
        refused = True
    check("the 10,001st connection is refused", refused)
    for s in socks[:5000]:
        s.close()
    check("closing 5,000 makes room", wait_clients(5002))
    again = connect()
    check("a new connection is served after closing", ping(again))
    for s in socks[5000:]:
        s.close()


def half_commands(p):
    n = 800
    socks = [connect() for _ in range(n)]
    for i, s in enumerate(socks):
        whole = cmd("SET", "k%d" % i, "v" * 100)
        s.sendall(whole[: 20 + i % 40])
    time.sleep(0.3)
    for i, s in enumerate(socks):
        whole = cmd("SET", "k%d" % i, "v" * 100)
        s.sendall(whole[20 + i % 40 :])
    check("800 half-commands all finish", all(read_until(s, 5) == b"+OK\r\n" for s in socks))
    c = connect()
    c.sendall(cmd("GET", "k799"))
    check("the values are there", read_until(c, 4 + 2 + 100 + 2) == b"$100\r\n" + b"v" * 100 + b"\r\n")
    # byte by byte, interleaved across 200 connections
    socks2 = socks[:200]
    for s in socks2:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    whole = cmd("SET", "slow", "x" * 300)
    for off in range(len(whole)):
        for s in socks2:
            s.sendall(whole[off : off + 1])
    check("byte-by-byte on 200 connections", all(read_until(s, 5) == b"+OK\r\n" for s in socks2))
    for s in socks + [c]:
        s.close()


def input_exhaustion(p):
    held = []
    for i in range(1400):
        try:
            s = connect()
            s.sendall(b"*1\r\n$4\r\nPI")
            held.append(s)
        except OSError:
            break
    time.sleep(0.5)
    live = 0
    for s in held:
        s.settimeout(0.05)
        try:
            if s.recv(1) == b"":
                continue
        except socket.timeout:
            live += 1
        except OSError:
            continue
    print("     %d of %d half-commands survived (pool is 1,024 blocks)" % (live, len(held)))
    check("some half-commands were refused", live < len(held))
    check("at least the pool's worth survived", live >= 1000)
    for s in held:
        s.close()
    check("blocks come back after closing", wait_clients(1))
    c = connect()
    s2 = [connect() for _ in range(900)]
    for s in s2:
        s.sendall(b"*1\r\n$4\r\nPI")
    for s in s2:
        s.sendall(b"NG\r\n")
    check("a second wave of 900 half-commands finishes", all(read_until(s, 7) == b"+PONG\r\n" for s in s2))
    check("the cache still answers", ping(c))


def queue_exhaustion(p):
    socks = [connect() for _ in range(160)]
    refused = 0
    for i, s in enumerate(socks):
        s.sendall(cmd("MULTI") + cmd("SET", "q%d" % i, "1"))
    for s in socks:
        got = read_until(s, 5)  # +OK\r\n for MULTI
        rest = b""
        try:
            rest = s.recv(100) if len(got) == 5 else b""
        except OSError:
            pass
        if b"-ERR" in got or b"-ERR" in rest:
            refused += 1
    print("     %d of 160 MULTI queues refused (pool is 128 blocks)" % refused)
    check("queues beyond the pool are refused", refused >= 160 - 128 and refused < 160)
    done = 0
    for s in socks:
        s.sendall(cmd("EXEC"))
        got = s.recv(200)
        if got.startswith(b"*1\r\n+OK") or got.startswith(b"*1\r\n"):
            done += 1
    check("EXEC works for the ones that queued", done >= 128, "done=%d" % done)
    for s in socks:
        s.close()
    check("blocks come back", wait_clients(1))
    again = [connect() for _ in range(100)]
    for s in again:
        s.sendall(cmd("MULTI") + cmd("SET", "z", "1") + cmd("EXEC"))
    ok = all(read_until(s, 5 + 5 + 4 + 5, ) .endswith(b"+OK\r\n") for s in again)
    check("a second wave of 100 transactions", ok)


def output_exhaustion(p):
    c = connect(timeout=60)
    c.sendall(cmd("SET", "big", "v" * 16000))
    read_until(c, 5)
    one = cmd("GET", "big")

    def wave(n):
        stalled = []
        for _ in range(n):
            try:
                s = connect(rcvbuf=2048)
                s.sendall(one * 400)
                stalled.append(s)
            except OSError:
                break
        return stalled

    first = wave(300)
    time.sleep(1)
    check("the cache answers while 300 clients do not read", ping(c))
    alive = info("connected_clients")
    print("     %d clients still connected after the first wave" % alive)
    for s in first:
        s.close()
    check("blocks come back after the stalled clients close", wait_clients(2, 20))
    second = wave(300)
    time.sleep(1)
    check("a second wave behaves the same", ping(c))
    for s in second:
        s.close()


for body in (idle_connections, half_commands, input_exhaustion, queue_exhaustion, output_exhaustion):
    case(body)

if failures:
    print("\n%d failure(s):" % len(failures))
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("\nconnections: all passed")
