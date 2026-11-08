#!/usr/bin/env python3
"""MONITOR against Redis (docs/design.md section 34).

    python3 tests/monitor.py [path/to/cache]        (needs redis-server, Redis 7.0)

The same commands run on Redis and on the cache while connections are in `MONITOR` mode, and the lines they were shown are compared with the time and the client's port taken out: which commands
are shown and which are not (admin ones, refused ones, the ones queued in MULTI), the order (the commands of an EXEC and then EXEC), the arguments as Redis quotes them (escapes, binary), the
redaction of AUTH, what a watching connection may itself run, and RESET.
"""
import os, re, socket, subprocess, sys, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
RPORT, CPORT = 6911, 6912
failures = []


def cmd(*args):
    out = b"*%d\r\n" % len(args)
    for a in args:
        a = a.encode("latin1") if isinstance(a, str) else a
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
    raise SystemExit("did not start: %s" % argv)


class Conn:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port))
        self.s.settimeout(0.25)

    def send(self, *args):
        self.s.sendall(cmd(*args))

    def drain(self):
        out = b""
        try:
            while True:
                d = self.s.recv(1 << 20)
                if not d:
                    break
                out += d
        except (socket.timeout, OSError):
            pass
        return out

    def ask(self, *args):
        self.send(*args)
        time.sleep(0.05)
        return self.drain()


def check(what, got, want):
    if got != want:
        failures.append("%s: got %r, wanted %r" % (what, got, want))


def lines(raw):
    """a monitor stream -> the lines, without the time and the port"""
    out = []
    for l in raw.split(b"\r\n"):
        if l:
            out.append(re.sub(rb"^\+\d+\.\d{6} \[0 [^\]]*\] ", b"+T [0 A] ", l))
    return out


redis = start(["redis-server", "--port", str(RPORT), "--save", "", "--appendonly", "no", "--protected-mode", "no"], RPORT)
cache = start([CACHE, str(CPORT)], CPORT)
try:
    ports = (RPORT, CPORT)
    mon = [Conn(p) for p in ports]
    mon2 = [Conn(p) for p in ports]
    cl = [Conn(p) for p in ports]
    for i in (0, 1):
        check("MONITOR answers OK", mon[i].ask("MONITOR"), b"+OK\r\n")
        check("a second watcher too", mon2[i].ask("MONITOR"), b"+OK\r\n")
        check("MONITOR again is ignored, with no answer (%d)" % i, mon2[i].ask("MONITOR"), b"")
    check("MONITOR with an argument", [mon2[i].ask("MONITOR", "x") for i in (0, 1)][1], [mon2[i].ask("MONITOR", "x") for i in (0, 1)][0])
    for i in (0, 1):
        mon[i].drain()
        mon2[i].drain()
    time.sleep(0.2)
    # a MONITOR that comes while others watch is not shown to them
    late = [Conn(p) for p in ports]
    for i in (0, 1):
        check("a late watcher", late[i].ask("MONITOR"), b"+OK\r\n")

    script = [("SET", "k", "v"), ("get", "k"), ("AUTH", "pw"), ("AUTH", "u", "pw"), ("HELLO", "3", "AUTH", "u", "pw"), ("HELLO", "2"), ("PING",), ("MULTI",), ("SET", "a", "1"), ("INCR", "a"),
              ("EXEC",), ("CLIENT", "SETNAME", "x"), ("INFO",), ("SLOWLOG", "LEN"), ("NOSUCH",), ("SET", "a"), ("CONFIG", "GET", "port"), ("CLIENT", "LIST"), ("LATENCY", "LATEST"),
              ("SET", "bin", "a\x00b\"c\\d\n\x7f\xc3\xa9\x01\t\r\x07\x08"), ("GET", "bin"), ("SET", "big", "x" * 200), ("COMMAND", "COUNT"), ("FLUSHALL",), ("DBSIZE",), ("WATCH", "k"),
              ("UNWATCH",), ("MULTI",), ("DISCARD",), ("ECHO", "hi there"), ("KEYS", "*"), ("SELECT", "0"), ("GET", "k", "extra"), ("MEMORY", "USAGE", "k"), ("OBJECT", "ENCODING", "k"),
              ("MULTI",), ("GET", "k"), ("NOSUCH",), ("EXEC",), ("DEL", "a", "b", "c")]
    for i in (0, 1):
        for step in script:
            cl[i].ask(*step)
    time.sleep(0.2)
    got = [lines(mon[i].drain()) for i in (0, 1)]
    got2 = [lines(mon2[i].drain()) for i in (0, 1)]
    check("what the first watcher was shown", got[1], got[0])
    check("what the second watcher was shown", got2[1], got2[0])
    check("both watchers were shown the same", got2[1], got[1])
    check("something was shown", len(got[1]) > 20, True)
    check("no MONITOR in what was shown", [l for l in got[1] if b'"MONITOR"' in l], [])
    # the time and the address, which the comparison above leaves out: the address is the one the commands came from, and the microseconds are not always zero
    for i in (0, 1):
        cl[i].ask("SET", "t", "1")
    time.sleep(0.2)
    samples = []
    for n in range(40):
        for i in (1,):
            cl[i].ask("GET", "t")
    time.sleep(0.2)
    stream = mon[1].drain()
    me = "127.0.0.1:%d" % cl[1].s.getsockname()[1]
    heads = re.findall(rb"^\+(\d+)\.(\d{6}) \[0 ([^\]]*)\]", stream, re.M)
    check("lines have a time near now, in seconds", all(abs(int(h[0]) - time.time()) < 60 for h in heads) and len(heads) >= 40, True)
    check("every line names the client that ran the command", set(h[2].decode() for h in heads), {me})
    check("the microseconds are not all zero", any(h[1] != b"000000" for h in heads), True)

    # what a watching connection may run itself
    for i in (0, 1):
        mon[i].drain()
    r = [(mon[i].ask("PING"), mon[i].ask("SET", "q", "1"), mon[i].ask("GET", "q"), mon[i].ask("INFO", "server")[:1], mon[i].ask("DBSIZE"), mon[i].ask("CLIENT", "ID")) for i in (0, 1)]
    check("a watcher runs PING, is refused the keyspace", [lines(x) if x.startswith(b"+PONG") else x for x in r[1][:3]], [lines(x) if x.startswith(b"+PONG") else x for x in r[0][:3]])
    check("a watcher runs INFO", r[1][3], r[0][3])
    check("a watcher is refused DBSIZE", r[1][4], r[0][4])
    check("a watcher may ask its id", r[1][5][:1], r[0][5][:1])
    # RESET ends watching
    for i in (0, 1):
        mon[i].ask("RESET")
        mon[i].drain()
        cl[i].ask("SET", "after", "reset")
    time.sleep(0.2)
    shown = [lines(mon[i].drain()) for i in (0, 1)]
    check("after RESET the connection is no longer shown commands", shown[1], shown[0])
    for i in (0, 1):
        check("and runs the keyspace again (%d)" % i, mon[i].ask("SET", "q", "1"), b"+OK\r\n")
    # QUIT of a watcher
    for i in (0, 1):
        mon2[i].drain()
        check("QUIT of a watcher is OK (%d)" % i, mon2[i].ask("QUIT")[:5], b"+OK\r\n")
    for i in (0, 1):
        check("the cache is still serving (%d)" % i, cl[i].ask("PING"), b"+PONG\r\n")
    # a watcher that does not read: the server goes on serving others
    slow = Conn(CPORT)
    slow.ask("MONITOR")
    for n in range(2000):
        cl[1].send("SET", "k%d" % n, "v" * 100)
        if n % 100 == 0:
            cl[1].drain()
    cl[1].drain()
    time.sleep(0.3)
    check("a watcher that never reads does not stop the cache", cl[1].ask("PING"), b"+PONG\r\n")
finally:
    for p in (redis, cache):
        p.terminate()

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("MONITOR agrees with Redis")
