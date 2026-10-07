#!/usr/bin/env python3
"""Transactions that need two connections (`docs/design.md` section 14).

    python3 tests/transactions.py [path/to/cache]        (needs redis-server; redis-py and ioredis if installed)

`tests/differential.py` shows what one connection sees. What `WATCH` is for needs a second: a write to a watched key from another
connection must abort `EXEC`, and a write to any other key must not. Each scenario runs against Redis and against the cache, step by step
on two connections, and the replies are compared byte for byte. Then real client libraries run a `WATCH`-based check-and-set against
a competing writer. A library that is not installed is reported as skipped.
"""
import os, shutil, socket, subprocess, sys, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
HERE = os.path.dirname(os.path.abspath(__file__))
REDIS_PORT, CACHE_PORT = 6492, 6493
failures, skipped = [], []


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
    raise SystemExit("server on %d did not start" % port)


class Conn:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port))
        self.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def ask(self, *words, quiet=0.12):
        """Send one command and answer everything the server says until it is quiet."""
        self.s.sendall(cmd(*words))
        got = b""
        self.s.settimeout(quiet)
        while True:
            try:
                d = self.s.recv(65536)
            except (socket.timeout, OSError):
                break
            if not d:
                break
            got += d
        return got


def scenario(steps):
    """`steps` is a list of (connection index, words). Answers the list of replies."""
    out = []
    for port in (REDIS_PORT, CACHE_PORT):
        conns = [Conn(port), Conn(port)]
        conns[0].ask("FLUSHALL")
        out.append([conns[i].ask(*w) for i, w in steps])
        for c in conns:
            c.s.close()
    return out


def check(what, cond, detail=""):
    print(("ok    " if cond else "FAIL  ") + what)
    if not cond:
        failures.append(what)
        if detail:
            print("      " + detail)


def same(name, steps, expect_abort=None):
    want, got = scenario(steps)
    check(name, want == got, "redis %r\n      cache %r" % (want, got))
    if expect_abort is not None:
        # The scenario's last reply is EXEC's: a null array if it aborted.
        aborted = got[-1] == b"*-1\r\n"
        check(name + (": aborts" if expect_abort else ": does not abort"), aborted == expect_abort, "last reply %r" % got[-1])


A, B = 0, 1
watch_then = lambda *middle: [(A, ("SET", "w:k", "1")), (A, ("WATCH", "w:k")), *middle, (A, ("MULTI",)), (A, ("GET", "w:k")), (A, ("EXEC",))]

redis_proc = start(["redis-server", "--port", str(REDIS_PORT), "--save", "", "--appendonly", "no", "--protected-mode", "no"], REDIS_PORT)
cache_proc = start([CACHE, str(CACHE_PORT)], CACHE_PORT)
try:
    same("another connection's SET of the watched key aborts EXEC", watch_then((B, ("SET", "w:k", "2"))), True)
    same("another connection's SET of the same value aborts EXEC", watch_then((B, ("SET", "w:k", "1"))), True)
    same("another connection's DEL aborts EXEC", watch_then((B, ("DEL", "w:k"))), True)
    same("another connection's INCR aborts EXEC", watch_then((B, ("SET", "w:k", "7")), (B, ("INCR", "w:k"))), True)
    same("another connection's EXPIRE aborts EXEC", watch_then((B, ("EXPIRE", "w:k", "100"))), True)
    same("another connection's FLUSHALL aborts EXEC", watch_then((B, ("FLUSHALL",))), True)
    same("a write to another key does not abort EXEC", watch_then((B, ("SET", "w:other", "2")), (B, ("DEL", "w:another"))), False)
    same("another connection's read does not abort EXEC", watch_then((B, ("GET", "w:k")), (B, ("TTL", "w:k"))), False)
    same("the second connection's EXEC is unaffected by the first's watch",
         [(A, ("WATCH", "w:k")), (B, ("MULTI",)), (B, ("SET", "w:k", "5")), (B, ("EXEC",)), (B, ("GET", "w:k"))], None)
    same("a watch that aborted is gone: the next transaction runs",
         [(A, ("SET", "w:k", "1")), (A, ("WATCH", "w:k")), (B, ("SET", "w:k", "2")), (A, ("MULTI",)), (A, ("EXEC",)), (A, ("MULTI",)), (A, ("GET", "w:k")), (A, ("EXEC",))], None)
    same("a command between MULTI and EXEC from another connection runs at once, not in the queue",
         [(A, ("SET", "w:c", "0")), (A, ("MULTI",)), (A, ("INCR", "w:c")), (B, ("INCR", "w:c")), (A, ("INCR", "w:c")), (A, ("EXEC",)), (A, ("GET", "w:c"))], None)
    same("a watched key that expires (set by another connection) aborts EXEC",
         [(A, ("SET", "w:k", "1")), (A, ("WATCH", "w:k")), (B, ("PEXPIRE", "w:k", "30")), (A, ("MULTI",)), (A, ("PING",)), (A, ("EXEC",))], True)

    # Answers larger than the cache's scratch buffer (2.2 MB): a transaction of 150 `GET`s of a 16,000-byte value answers 2.4 MB, which `EXEC` has to
    # send in pieces as it goes. (Not a case of `differential.py`, which sends every case twenty times in one write.)
    big = []
    for port in (REDIS_PORT, CACHE_PORT):
        c = Conn(port)
        c.ask("SET", "t:big", b"v" * 16000)
        c.s.sendall(cmd("MULTI") + b"".join(cmd("GET", "t:big") for _ in range(150)) + cmd("EXEC") + cmd("PING"))
        got = b""
        c.s.settimeout(1.0)
        while not got.endswith(b"+PONG\r\n"):
            d = c.s.recv(1 << 20)
            if not d:
                break
            got += d
        big.append(got)
    check("EXEC sends answers larger than the scratch buffer in pieces, in order", big[0] == big[1] and len(big[0]) > 2400000, "lengths %d and %d" % (len(big[0]), len(big[1])))

    # Real client libraries: a check-and-set loop against a competing writer.
    try:
        import redis
    except ImportError:
        skipped.append("redis-py (pip install redis)")
        redis = None
    if redis:
        r = redis.Redis(port=CACHE_PORT, decode_responses=True, socket_timeout=5)
        w = redis.Redis(port=CACHE_PORT, decode_responses=True, socket_timeout=5)
        r.flushall()
        pipe = r.pipeline()
        pipe.set("py:a", "1")
        pipe.incr("py:a")
        pipe.get("py:a")
        check("redis-py: default pipeline() (a MULTI/EXEC)", pipe.execute() == [True, 2, "2"])
        r.set("py:n", "10")
        attempts = []

        # redis-py's own `transaction()` retries without end, so a server that always aborts would hang the test: the loop is written out and bounded.
        committed = False
        for _ in range(5):
            attempts.append(1)
            with r.pipeline() as p:
                try:
                    p.watch("py:n")
                    value = int(p.get("py:n"))
                    if len(attempts) == 1:
                        w.set("py:n", "100")      # a competing write between the read and the EXEC
                    p.multi()
                    p.set("py:n", value + 1)
                    p.execute()
                    committed = True
                    break
                except redis.WatchError:
                    continue
        check("redis-py: a WATCH loop retries after a competing write and then commits", committed and len(attempts) == 2 and r.get("py:n") == "101", "attempts %d, value %r" % (len(attempts), r.get("py:n")))
        pipe = r.pipeline()
        pipe.watch("py:n")
        w.set("py:n", "5")
        pipe.multi()
        pipe.get("py:n")
        try:
            pipe.execute()
            check("redis-py: a competing write raises WatchError", False)
        except redis.WatchError:
            check("redis-py: a competing write raises WatchError", True)
        pipe = r.pipeline()
        pipe.multi()
        pipe.set("py:b", "1")
        pipe.execute_command("NOSUCHCOMMAND")
        try:
            pipe.execute()
            check("redis-py: an unknown command in the queue fails the transaction", False)
        except redis.ResponseError:
            check("redis-py: an unknown command in the queue fails the transaction", r.get("py:b") is None)
        r3 = redis.Redis(port=CACHE_PORT, decode_responses=True, protocol=3, socket_timeout=5)
        pipe = r3.pipeline()
        pipe.set("py:c", "1")
        pipe.get("py:c")
        check("redis-py: a transaction over RESP3", pipe.execute() == [True, "1"])

    node = shutil.which("node")
    modules = os.path.join(HERE, "clients", "node_modules", "ioredis")
    if node and os.path.isdir(modules):
        run = subprocess.run([node, os.path.join(HERE, "clients", "ioredis_txn.js"), str(CACHE_PORT)], capture_output=True, text=True, timeout=30)
        check("ioredis: multi().exec(), and a watch aborted by a competing write", run.returncode == 0 and "ioredis txn ok" in run.stdout, run.stdout + run.stderr)
    else:
        skipped.append("ioredis (npm install ioredis in tests/clients)")
finally:
    redis_proc.terminate()
    cache_proc.terminate()

if skipped:
    print("skipped: " + "; ".join(skipped))
if failures:
    print("%d failed" % len(failures))
    sys.exit(1)
print("transactions behave as Redis's do")
