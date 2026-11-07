#!/usr/bin/env python3
"""Cluster-lite against a real Redis Cluster (docs/design.md section 29).

    python3 tests/cluster.py [path/to/cache]        (needs redis-server and redis-cli, Redis 7.0)

Three Redis nodes (ports 7100-7102) are made into a cluster with `redis-cli --cluster create`, which gives them slots 0-5460, 5461-10922 and
10923-16383; three cache nodes (7200-7202) are started with the same map. Each line is sent to the same node of both and the replies are compared,
after the Redis side's ports are mapped to the cache's (7100 -> 7200, bus ports too) and node ids, which are random in Redis, are replaced by `ID`.
What cannot be the same, and is compared in the shape it can be: the order of the lines of CLUSTER NODES and of the shards of CLUSTER SHARDS (Redis lists
them in the order of a dictionary), the epochs, timestamps and message counters (there is no gossip here).

Also: CLUSTER KEYSLOT of tens of thousands of random and hash-tagged keys, and a map that is not one stops the start.
"""
import os, random, re, shutil, socket, subprocess, sys, tempfile, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
RPORTS, CPORTS = (7100, 7101, 7102), (7200, 7201, 7202)
MAP = "--cluster=0-5460=127.0.0.1:7200,5461-10922=127.0.0.1:7201,10923-16383=127.0.0.1:7202"
failures, checked = [], 0


def cmd(*args):
    out = b"*%d\r\n" % len(args)
    for a in args:
        a = a.encode() if isinstance(a, str) else a
        out += b"$%d\r\n%s\r\n" % (len(a), a)
    return out


class Client:
    def __init__(self, port, resp3=False):
        self.s = socket.create_connection(("127.0.0.1", port))
        self.s.settimeout(10)
        self.buf = b""
        if resp3:
            self.ask("HELLO", "3")

    def _line(self):
        while b"\r\n" not in self.buf:
            d = self.s.recv(1 << 20)
            if not d:
                raise EOFError
            self.buf += d
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def _bytes(self, n):
        while len(self.buf) < n + 2:
            d = self.s.recv(1 << 20)
            if not d:
                raise EOFError
            self.buf += d
        v, self.buf = self.buf[:n], self.buf[n + 2:]
        return v

    def read(self):
        line = self._line()
        t, rest = line[:1], line[1:]
        if t == b"+":
            return ("+", rest)
        if t == b"-":
            return ("-", rest)
        if t == b":":
            return int(rest)
        if t == b"$":
            n = int(rest)
            return None if n < 0 else self._bytes(n)
        if t == b"=":
            return ("txt", self._bytes(int(rest))[4:])
        if t == b"_":
            return None
        if t == b"*":
            n = int(rest)
            return None if n < 0 else [self.read() for _ in range(n)]
        if t == b"~":
            return ("set", [self.read() for _ in range(int(rest))])
        if t == b"%":
            return ("map", [self.read() for _ in range(2 * int(rest))])
        raise ValueError(line)

    def ask(self, *args):
        self.s.sendall(cmd(*args))
        return self.read()

    def pipeline(self, commands):
        self.s.sendall(b"".join(cmd(*c) for c in commands))
        return [self.read() for _ in commands]


ID = re.compile(rb"[0-9a-f]{40}")


def mapped(x, redis_side, keep_ints):
    """A reply with node ids replaced and, on the Redis side, its ports taken to the cache's."""
    if isinstance(x, bytes):
        x = ID.sub(b"ID", x)
        if redis_side:
            for r, c in zip(RPORTS, CPORTS):
                x = x.replace(b"%d" % r, b"%d" % c).replace(b"%d" % (r + 10000), b"%d" % (c + 10000))
        return x
    if isinstance(x, int):
        if redis_side and not keep_ints:
            for r, c in zip(RPORTS, CPORTS):
                if x == r:
                    return c
                if x == r + 10000:
                    return c + 10000
        return x
    if isinstance(x, tuple):
        return (x[0], mapped(x[1], redis_side, keep_ints))
    if isinstance(x, list):
        return [mapped(i, redis_side, keep_ints) for i in x]
    return x


def check(what, got, want):
    global checked
    checked += 1
    if got != want:
        failures.append("%s\n    redis: %r\n    cache: %r" % (what, want, got))


def nodes_lines(reply):
    """CLUSTER NODES as sorted lines with what has no counterpart (the timestamp and epoch) taken out."""
    text = reply[1] if isinstance(reply, tuple) else reply
    lines = []
    for line in text.decode().strip().split("\n"):
        parts = line.split(" ")
        parts[0], parts[4], parts[5], parts[6] = "ID", "0", "T", "E"
        lines.append(" ".join(parts).encode())
    return sorted(lines)


def info_fields(reply):
    text = reply[1] if isinstance(reply, tuple) else reply
    keep = ("cluster_state", "cluster_slots_assigned", "cluster_slots_ok", "cluster_slots_pfail", "cluster_slots_fail", "cluster_known_nodes", "cluster_size")
    return [l for l in text.decode().split("\r\n") if l.split(":")[0] in keep]


def shards_sorted(reply):
    items = reply[1] if isinstance(reply, tuple) else reply
    return sorted(items, key=lambda s: ID.sub(b"ID", repr(s).encode()))


def main():
    tmp = tempfile.mkdtemp(prefix="cluster-")
    procs = []
    try:
        for p in RPORTS:
            d = os.path.join(tmp, str(p))
            os.mkdir(d)
            procs.append(subprocess.Popen(["redis-server", "--port", str(p), "--cluster-enabled", "yes", "--cluster-config-file", "nodes.conf", "--save", "", "--appendonly", "no",
                                           "--protected-mode", "no"], cwd=d, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for i, p in enumerate(CPORTS):
            procs.append(subprocess.Popen([CACHE, str(p), "64", "100000", "noeviction", "1", MAP], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for p in RPORTS + CPORTS:
            for _ in range(100):
                try:
                    socket.create_connection(("127.0.0.1", p), 0.2).close()
                    break
                except OSError:
                    time.sleep(0.05)
        subprocess.run(["redis-cli", "--cluster", "create"] + ["127.0.0.1:%d" % p for p in RPORTS] + ["--cluster-yes"], stdout=subprocess.DEVNULL, check=True)
        for _ in range(100):
            if b"cluster_state:ok" in Client(RPORTS[0]).ask("CLUSTER", "INFO")[1:] if False else b"cluster_state:ok" in Client(RPORTS[0]).ask("CLUSTER", "INFO"):
                break
            time.sleep(0.1)
        time.sleep(1)
        rnd = random.Random(20261107)

        def same(what, node, *line, resp3=False, keep_ints=False, prep=None):
            r, c = Client(RPORTS[node], resp3), Client(CPORTS[node], resp3)
            want, got = r.ask(*line), c.ask(*line)
            if prep:
                want, got = prep(want), prep(got)
            check("%s [node %d%s]" % (what, node, ", RESP3" if resp3 else ""), mapped(got, False, keep_ints), mapped(want, True, keep_ints))

        # --- the slot of a key
        keys = ["foo", "bar", "{user1}.a", "{}x", "a{b", "{{a}}", "{a}{b}", "a}{b", "{", "}", "{}", "x{}", "{a}", "a{b}c{d}", "", "{" + "k" * 300 + "}z"]
        for _ in range(30000):
            kind = rnd.random()
            body = "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz0123456789:_-{}") for _ in range(rnd.randrange(0, 24)))
            keys.append(body if kind < 0.5 else "{%s}%s" % (body, "x" * rnd.randrange(0, 5)))
        r, c = Client(RPORTS[0]), Client(CPORTS[0])
        want = r.pipeline([("CLUSTER", "KEYSLOT", k) for k in keys])
        got = c.pipeline([("CLUSTER", "KEYSLOT", k) for k in keys])
        bad = [(k, w, g) for k, w, g in zip(keys, want, got) if w != g]
        check("CLUSTER KEYSLOT of %d keys, random and hash-tagged: %d differ %r" % (len(keys), len(bad), bad[:3]), len(bad), 0)
        # --- redirects, for every node
        for node in range(3):
            sample = ["k%d" % i for i in range(60)] + ["{t%d}a" % i for i in range(10)]
            for k in sample:
                same("SET %s" % k, node, "SET", k, "v")
                same("GET %s" % k, node, "GET", k)
            for _ in range(80):
                a, b, d = rnd.choice(sample), rnd.choice(sample), rnd.choice(sample)
                for line in (("MGET", a, b), ("DEL", a, b, d), ("MSET", a, "1", b, "2"), ("EXISTS", a, b), ("TOUCH", a, b, d), ("RENAME", a, b), ("COPY", a, b), ("UNLINK", a), ("EXPIRE", a, "100"), ("TTL", a), ("OBJECT", "ENCODING", a)):
                    same(" ".join(line), node, *line)
            for line in (("MGET", "{t1}a", "{t1}b"), ("MSET", "{t2}a", "1", "{t2}b", "2"), ("DEL", "{t3}a", "{t3}z"), ("SET", "{t4}x", "1"), ("HSET", "h", "f", "v"), ("PING",), ("DBSIZE",), ("KEYS", "nosuch*"), ("SELECT", "0"), ("SELECT", "1"), ("ECHO", "x"), ("MGET",), ("GET",), ("SET", "a")):
                same(" ".join(line), node, *line)
        # --- a transaction in which a command is redirected
        for node in range(3):
            for resp3 in (False, True):
                r, c = Client(RPORTS[node], resp3), Client(CPORTS[node], resp3)
                lines = [("MULTI",), ("SET", "foo", "1"), ("SET", "bar", "1"), ("GET", "bar"), ("EXEC",), ("MULTI",), ("SET", "k1", "1"), ("GET", "k1"), ("EXEC",), ("WATCH", "foo"), ("WATCH", "bar", "baz"), ("MULTI",), ("DISCARD",)]
                check("MULTI with a redirected command [node %d%s]" % (node, ", RESP3" if resp3 else ""), mapped(c.pipeline(lines), False, False), mapped(r.pipeline(lines), True, False))
        # --- the CLUSTER commands
        for node in range(3):
            for resp3 in (False, True):
                same("CLUSTER SLOTS", node, "CLUSTER", "SLOTS", resp3=resp3)
                same("CLUSTER NODES", node, "CLUSTER", "NODES", resp3=resp3, prep=nodes_lines)
                same("CLUSTER INFO", node, "CLUSTER", "INFO", resp3=resp3, prep=info_fields)
                same("CLUSTER SHARDS", node, "CLUSTER", "SHARDS", resp3=resp3, prep=shards_sorted)
                same("CLUSTER MYID has the shape of an id", node, "CLUSTER", "MYID", resp3=resp3, prep=lambda x: bool(ID.fullmatch(x)))
            for line in (("CLUSTER",), ("CLUSTER", "KEYSLOT"), ("CLUSTER", "KEYSLOT", "a", "b"), ("CLUSTER", "NOSUCH"), ("CLUSTER", "MYID", "x"), ("CLUSTER", "SLOTS", "x"), ("READONLY",), ("READWRITE",), ("READONLY", "x"), ("CLUSTER", "keyslot", "foo"), ("cluster", "KeySlot", "{a}b")):
                same(" ".join(line), node, *line, keep_ints=True)
        # --- the commands the table knows about, in a cluster
        same("COMMAND GETKEYS in a cluster", 0, "COMMAND", "GETKEYS", "GET", "foo")
        # --- a map that is not one stops the start
        for bad_map in ("--cluster=0-5460=127.0.0.1:7220,5000-6000=127.0.0.1:7201", "--cluster=0-16384=127.0.0.1:7220", "--cluster=0-5460=127.0.0.1:7201", "--cluster=0-5460=:7220", "--cluster=x=127.0.0.1:7220", "--cluster=0-5460=127.0.0.1", "--cluster=5-3=127.0.0.1:7220", "--cluster=", "--cluster=0-5460"):
            p = subprocess.Popen([CACHE, "7220", "64", "1000", "noeviction", "1", bad_map], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                code = p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                code = None
            check("a map that is not one stops the start: %s" % bad_map, code, 2)
        # --- a map with a slot nobody serves: CLUSTERDOWN, and the state is fail
        port = 7210
        p = subprocess.Popen([CACHE, str(port), "64", "1000", "noeviction", "1", "--cluster=0-100=127.0.0.1:7210"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        time.sleep(0.5)
        x = Client(port)
        check("a slot nobody serves", (x.ask("SET", "foo", "1"), x.ask("SET", "{a}", "1")), (("-", b"CLUSTERDOWN Hash slot not served"), ("-", b"CLUSTERDOWN Hash slot not served")))
        check("CLUSTER INFO of a map with holes says fail", info_fields(x.ask("CLUSTER", "INFO"))[0], "cluster_state:fail")
        # --- a cache that is not a cluster node
        port = 7211
        p = subprocess.Popen([CACHE, str(port), "64", "1000"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        procs.append(p)
        time.sleep(0.5)
        x = Client(port)
        check("without --cluster: CLUSTER, READONLY and a plain SET", (x.ask("CLUSTER", "KEYSLOT", "foo"), x.ask("READONLY"), x.ask("SET", "foo", "1"), x.ask("SELECT", "1")),
              (("-", b"ERR This instance has cluster support disabled"), ("-", b"ERR This instance has cluster support disabled"), ("+", b"OK"), ("-", b"ERR DB index is out of range")))
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        shutil.rmtree(tmp, ignore_errors=True)


main()
if failures:
    print("\n".join(failures[:20]))
    print("%d of %d comparisons differ" % (len(failures), checked))
    sys.exit(1)
print("cluster-lite answers as a Redis cluster does (%d comparisons)" % checked)
