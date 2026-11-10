#!/usr/bin/env python3
"""The append-only log, written (docs/design.md section 38, slice 4a).

    python3 tests/aof.py [path/to/cache]

  * the records: what each kind of write leaves in the file (a string, a hash, a deleted key, a new deadline only, a flush), at the right absolute time, and that reads, errors and the
    commands queued in MULTI leave nothing; EXEC leaves what it ran; a pipeline is one batch in order;
  * every batch is whole: its length, kind 9 and CRC-64/XZ are right, and nothing follows the last;
  * the log says what the cache holds: a seeded random run of writes (strings, hashes, renames, deadlines, flushes) under eviction, replayed in Python from the file alone, is the keyspace
    the cache has, key by key, string by string, deadline by deadline (a hash by its kind and its fields being in the logged bytes);
  * a value bigger than the buffer (1 MiB, arriving in pieces) is a batch of its own;
  * the three policies, by the order of the calls (strace): `always` syncs the log before the answer leaves, `no` never syncs it, `everysec` syncs it about once a second and not before the
    answer;
  * start-up: a log is made if there is none and left as it is if there is; --appendonly needs --dir; wrong options stop the start.
"""
import os, random, shutil, socket, struct, subprocess, sys, tempfile, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
PORT = 6923
failures = []
processes = []


def check(what, got, want):
    if got != want:
        failures.append("%s: got %r, wanted %r" % (what, got, want))


def cmd(*args):
    out = b"*%d\r\n" % len(args)
    for a in args:
        a = a.encode("latin1") if isinstance(a, str) else a
        out += b"$%d\r\n%s\r\n" % (len(a), a)
    return out


class Client:
    def __init__(self):
        self.s = socket.create_connection(("127.0.0.1", PORT))
        self.s.settimeout(30)
        self.buf = b""

    def _line(self):
        while b"\r\n" not in self.buf:
            chunk = self.s.recv(1 << 20)
            if not chunk:
                raise EOFError
            self.buf += chunk
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def read(self):
        line = self._line()
        t, rest = line[:1], line[1:]
        if t in (b"+", b"-"):
            return (t.decode(), rest.decode("latin1"))
        if t == b":":
            return int(rest)
        if t == b"$":
            n = int(rest)
            if n < 0:
                return None
            while len(self.buf) < n + 2:
                self.buf += self.s.recv(1 << 20)
            v, self.buf = self.buf[:n], self.buf[n + 2:]
            return v
        if t == b"*":
            n = int(rest)
            return None if n < 0 else [self.read() for _ in range(n)]
        raise ValueError(line)

    def ask(self, *args):
        self.s.sendall(cmd(*args))
        return self.read()

    def pipeline(self, commands):
        self.s.sendall(b"".join(cmd(*a) for a in commands))
        return [self.read() for _ in commands]


def start(directory, *extra, memory="64", policy="noeviction", wrap=None):
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.2).close()
        raise SystemExit("something is already listening on %d (a cache left by an earlier run?)" % PORT)
    except OSError:
        pass
    argv = [CACHE, str(PORT), memory, "1000000", policy, "1"] + (["--dir=" + directory] if directory else []) + list(extra)
    if wrap:
        argv = wrap + argv
    p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    processes.append(p)
    for _ in range(100):
        if p.poll() is not None:
            return p, None
        try:
            return p, Client()
        except OSError:
            time.sleep(0.05)
    raise SystemExit("did not start")


def stop(p):
    p.terminate()
    p.wait()
    p.stderr.close()


# --- CRC-64/XZ
_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0xC96C5795D7870F42 if _c & 1 else _c >> 1
    _TABLE.append(_c)


def crc64(data):
    crc = 0xFFFFFFFFFFFFFFFF
    for b in data:
        crc = _TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFFFFFFFFFF


def parse_log(path):
    """[(unix ms of the batch, [(kind, key, value, ends)])], and the number of bytes after the last whole batch. A batch that does not check out is reported in `failures`."""
    d = open(path, "rb").read()
    if d[:8] != b"CANCHOLG" or struct.unpack_from("<Q", d, 8)[0] != 1:
        failures.append("%s: not a log (%r)" % (path, d[:16]))
        return [], len(d)
    at = 32
    batches, records, start_at = [], [], 32
    while at + 32 <= len(d):
        a, b, kind, c = struct.unpack_from("<QQQQ", d, at)
        if kind == 9:
            body = d[start_at:at]
            if a != len(body) or c != crc64(body):
                failures.append("%s: a batch at %d does not check out (length %d vs %d)" % (path, start_at, a, len(body)))
            batches.append((b, records))
            records = []
            at += 32
            start_at = at
            continue
        key = d[at + 32:at + 32 + a]
        value = d[at + 32 + a:at + 32 + a + b]
        if len(key) != a or len(value) != b:
            break
        records.append((kind, key, value, c))
        at += 32 + a + b
    return batches, len(d) - start_at


def flat(batches):
    return [r for _, rs in batches for r in rs]


work = tempfile.mkdtemp(prefix="aof-")
log = lambda dd: os.path.join(dd, "appendonly.cho")


def fresh(name):
    dd = os.path.join(work, name)
    os.mkdir(dd)
    return dd


try:
    # --- the records
    dd = fresh("records")
    p, c = start(dd, "--appendonly=yes", "--appendfsync=no")
    t0 = int(time.time() * 1000)
    check("SET", c.ask("SET", "k", "v"), ("+", "OK"))
    check("SET EX", c.ask("SET", "e", "1", "EX", "100"), ("+", "OK"))
    check("INCR", c.ask("INCR", "n"), 1)
    check("HSET", c.ask("HSET", "h", "f", "x"), 1)
    check("MSET", c.ask("MSET", "a", "1", "b", "2"), ("+", "OK"))
    check("GET", c.ask("GET", "a"), b"1")
    check("DEL missing", c.ask("DEL", "nothing"), 0)
    check("DEL", c.ask("DEL", "k"), 1)
    check("EXPIRE", c.ask("EXPIRE", "a", "50"), 1)
    check("PERSIST", c.ask("PERSIST", "e"), 1)
    check("WRONGTYPE", c.ask("HSET", "n", "f", "x")[0], "-")
    check("unknown", c.ask("NOSUCH", "x")[0], "-")
    check("PING", c.ask("PING"), ("+", "PONG"))
    check("RENAME", c.ask("RENAME", "b", "c"), ("+", "OK"))
    check("FLUSHALL", c.ask("FLUSHALL"), ("+", "OK"))
    t1 = int(time.time() * 1000)
    batches, rest = parse_log(log(dd))
    check("nothing after the last batch", rest, 0)
    recs = flat(batches)
    names = [(k, key) for k, key, v, e in recs]
    check("kinds and keys, in order", names, [(0, b"k"), (0, b"e"), (0, b"n"), (1, b"h"), (0, b"a"), (0, b"b"), (2, b"nothing"), (2, b"k"), (3, b"a"), (3, b"e"), (2, b"b"), (0, b"c"), (4, b"")])
    by = {}
    for k, key, v, e in recs:
        by.setdefault((k, key), []).append((v, e))
    check("a string's value", by[(0, b"k")][0], (b"v", 0))
    check("INCR's result, not the command", by[(0, b"n")][0], (b"1", 0))
    ends = by[(0, b"e")][0][1]
    check("SET EX is an absolute time", t0 + 99000 <= ends <= t1 + 101000, True)
    check("a hash is its whole value", by[(1, b"h")][0][0].endswith(b"fx"), True)
    exp = by[(3, b"a")][0]
    check("EXPIRE is a deadline and no value", exp[0] == b"" and t0 + 49000 <= exp[1] <= t1 + 51000, True)
    check("PERSIST is no deadline", by[(3, b"e")][0], (b"", 0))
    check("MSET and RENAME are one batch of two keys each", [len(rs) for _, rs in batches if len(rs) == 2], [2, 2])
    check("a read, an error, a PING leave nothing (11 writes' worth of batches)", len(batches), 11)
    check("the batch times are the clock's", all(t0 - 50 <= ms <= t1 + 50 for ms, _ in batches), True)
    # MULTI: the queued commands leave nothing until EXEC, and then what they ran
    n_before = len(parse_log(log(dd))[0])
    c.ask("MULTI")
    c.ask("SET", "m1", "1")
    c.ask("SET", "m2", "2")
    check("queued writes leave nothing", len(parse_log(log(dd))[0]), n_before)
    c.ask("EXEC")
    got = [key for k, key, v, e in flat(parse_log(log(dd))[0][n_before:])]
    check("EXEC leaves what it ran", got, [b"m1", b"m2"])
    n_before = len(parse_log(log(dd))[0])
    c.ask("MULTI")
    c.ask("SET", "m3", "3")
    c.ask("DISCARD")
    check("DISCARD leaves nothing", len(parse_log(log(dd))[0]), n_before)
    # a pipeline is read in one go and logged as it ran
    c.pipeline([("SET", "p%d" % i, str(i)) for i in range(20)])
    got = [key for k, key, v, e in flat(parse_log(log(dd))[0][n_before:])]
    check("a pipeline's records are in order", got, [("p%d" % i).encode() for i in range(20)])
    stop(p)

    # --- a value bigger than the buffer, arriving in pieces, is a batch of its own
    dd = fresh("big")
    p, c = start(dd, "--appendonly=yes", "--appendfsync=no", memory="64")
    big = os.urandom(1 << 20)
    check("a 1 MiB SET", c.ask("SET", "big", big), ("+", "OK"))
    check("a small one after it", c.ask("SET", "small", "s"), ("+", "OK"))
    batches, rest = parse_log(log(dd))
    check("the big one is a batch of its own, whole", [(len(rs), rs[0][1], len(rs[0][2])) for _, rs in batches], [(1, b"big", len(big)), (1, b"small", 1)])
    check("and the bytes are the bytes", flat(batches)[0][2] == big, True)
    stop(p)

    # two big values in one go: the second does not fit beside the first and the first is written
    dd = fresh("two-big")
    p, c = start(dd, "--appendonly=yes", "--appendfsync=no", memory="64")
    one, two = os.urandom(200000), os.urandom(200000)
    check("two 200 KB values pipelined", c.pipeline([("SET", "one", one), ("SET", "two", two)]), [("+", "OK"), ("+", "OK")])
    recs = flat(parse_log(log(dd))[0])
    check("both are in the log, whole, in order", [(key, v == (one if key == b"one" else two)) for k, key, v, e in recs], [(b"one", True), (b"two", True)])
    stop(p)

    # records that do not fit beside each other in one step: the staged ones are written first, in order (RENAME makes a record of the whole value)
    dd = fresh("boundary")
    p, c = start(dd, "--appendonly=yes", "--appendfsync=no", memory="64")
    c.ask("SET", "big", b"x" * 200000)
    got = c.pipeline([("RENAME", "big", "big2"), ("RENAME", "big2", "big3")])
    check("two renames of a 200 KB value in one go", got, [("+", "OK"), ("+", "OK")])
    recs = flat(parse_log(log(dd))[0])
    check("each record is whole, in order", [(key, len(v)) for k, key, v, e in recs], [(b"big", 200000), (b"big", 0), (b"big2", 200000), (b"big2", 0), (b"big3", 200000)])
    c.ask("SET", "huge", b"y" * 400000)
    got = c.pipeline([("SET", "first", "1"), ("RENAME", "huge", "huge2")])
    check("a small write and then a rename of a 400 KB value (bigger than the buffer)", got, [("+", "OK"), ("+", "OK")])
    recs = flat(parse_log(log(dd))[0])
    check("the small record is written before the big one", [(key, len(v)) for k, key, v, e in recs][-3:], [(b"first", 1), (b"huge", 0), (b"huge2", 400000)])
    stop(p)

    # --- the log says what the cache holds
    def replay(batches):
        state = {}
        for k, key, v, e in flat(batches):
            if k in (0, 1):
                state[key] = (k, v, e)
            elif k == 2:
                state.pop(key, None)
            elif k == 3:
                if key in state:
                    state[key] = (state[key][0], state[key][1], e)
            elif k == 4:
                state.clear()
        return state

    for policy, memory, ops, label in (("noeviction", "64", 6000, "no pressure"), ("allkeys-lru", "1", 6000, "under eviction")):
        dd = fresh("model-" + label.replace(" ", "-"))
        p, c = start(dd, "--appendonly=yes", "--appendfsync=no", memory=memory, policy=policy)
        rng = random.Random(20260511)
        keys = ["k%d" % i for i in range(120)]
        hashes = ["h%d" % i for i in range(20)]
        for i in range(ops):
            r = rng.random()
            k = rng.choice(keys)
            h = rng.choice(hashes)
            if r < 0.25:
                c.ask("SET", k, "x" * rng.choice([1, 10, 300, 5000, 30000]))
            elif r < 0.30:
                c.ask("SET", k, "v%d" % i, "EX", str(rng.randint(100, 900)))
            elif r < 0.37:
                c.ask("APPEND", k, "z")
            elif r < 0.42:
                c.ask("INCR", "n%d" % rng.randint(0, 9))
            elif r < 0.50:
                c.ask("DEL", k, rng.choice(keys))
            elif r < 0.55:
                c.ask("MSET", k, "m", rng.choice(keys), "n")
            elif r < 0.60:
                c.ask("RENAME", k, rng.choice(keys))
            elif r < 0.66:
                c.ask("EXPIRE", k, str(rng.randint(100, 900)))
            elif r < 0.69:
                c.ask("PERSIST", k)
            elif r < 0.78:
                c.ask("HSET", h, "f%d" % rng.randint(0, 9), "y" * rng.choice([1, 50, 500]))
            elif r < 0.80:
                c.ask("HDEL", h, "f%d" % rng.randint(0, 9))
            elif r < 0.84:
                c.ask("SETRANGE", k, "3", "ab")
            elif r < 0.87:
                c.ask("GETSET", k, "gs")
            elif r < 0.89:
                c.ask("SETNX", k, "nx")
            elif r < 0.91:
                c.ask("GETDEL", k)
            elif r < 0.9985:
                c.ask("GET", k)
            else:
                c.ask("FLUSHDB")
        def compare(when):
            batches, rest = parse_log(log(dd))
            check("%s, %s: the log ends on a batch" % (label, when), rest, 0)
            model = replay(batches)
            have = c.ask("KEYS", "*")
            check("%s, %s: the same keys" % (label, when), sorted(model.keys()), sorted(have))
            bad = 0
            for key in have:
                kind, value, ends = model.get(key, (None, None, None))
                if c.ask("TYPE", key) != ("+", "string" if kind == 0 else "hash"):
                    bad += 1
                    continue
                if kind == 0 and c.ask("GET", key) != value:
                    bad += 1
                elif kind == 1:
                    pairs = c.ask("HGETALL", key)
                    got = dict(zip(pairs[0::2], pairs[1::2]))
                    # the hash's own layout is the store's; compare the field set and values by looking for each in the bytes
                    for f, val in got.items():
                        if f not in value or val not in value:
                            bad += 1
                have_ends = c.ask("PEXPIRETIME", key)
                if (ends == 0) != (have_ends == -1) or (ends and abs(have_ends - ends) > 2):
                    bad += 1
            check("%s, %s: every key's type, value and deadline" % (label, when), bad, 0)
            return batches

        # twice: after the random stretch, where what the log said about evictions is what the keys that were not written again show (a record about a key that was evicted and
        # is there again is wrong if the log keeps saying it), and after a last stretch of fresh, big keys that nothing writes again, which evict for good (a record that is missing
        # shows there)
        compare("after the random stretch")
        for i in range(40):
            c.ask("SET", "z%d" % i, "q" * 50000)
        batches = compare("after the big keys")
        if label == "under eviction":
            info = c.ask("INFO", "stats").decode()
            ev = [l for l in info.split("\r\n") if l.startswith("evicted_keys:")]
            check("%s: there was eviction" % label, int(ev[0].split(":")[1]) > 0, True)
            check("%s: and the log has it as deleted keys" % label, sum(1 for k, key, v, e in flat(batches) if k == 2) > 0, True)
        stop(p)

    # --- the three policies, by the order of the calls
    if shutil.which("strace"):
        def traced(policy, label, body):
            dd = fresh("trace-" + label)
            trace = os.path.join(dd, "trace.txt")
            wrap = ["strace", "-f", "-o", trace, "-e", "trace=openat,fsync,fdatasync,write,sendto,send"]
            tp = subprocess.Popen(wrap + [CACHE, str(PORT), "64", "1000000", "noeviction", "1", "--dir=" + dd, "--appendonly=yes", "--appendfsync=" + policy], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            processes.append(tp)
            c = None
            for _ in range(100):
                try:
                    c = Client()
                    break
                except OSError:
                    time.sleep(0.05)
            body(c)
            subprocess.run(["pkill", "-TERM", "-P", str(tp.pid)])
            tp.wait(timeout=20)
            events, log_fds, header_done = [], set(), False
            for l in open(trace).read().split("\n"):
                parts = l.split(None, 1)
                if len(parts) < 2:
                    continue
                body_ = parts[1]
                if "openat" in body_ and "appendonly.cho" in body_ and "= " in body_ and not body_.rstrip().endswith("ENOENT") and "EEXIST" not in body_:
                    log_fds.add(int(body_.rsplit("= ", 1)[1].split()[0]))
                elif body_.startswith("fsync(") or body_.startswith("fdatasync("):
                    fd = int(body_[body_.index("(") + 1:body_.index(")")])
                    events.append("sync-log" if fd in log_fds else "sync-other")
                elif (body_.startswith("write(") or body_.startswith("sendto(") or body_.startswith("send(")) and '"+OK\\r\\n"' in body_:
                    events.append("reply")
            return events

        events = traced("no", "header", lambda c: [c.ask("SET", "a", "1")])
        first = [e for e in events if e in ("sync-log", "reply")]
        check("the header is synced before the first answer", first[:2], ["sync-log", "reply"])
        events = traced("always", "always", lambda c: [c.ask("SET", "a", "1"), c.ask("SET", "b", "2"), c.ask("SET", "c", "3")])
        # the header is synced once at the start; after that every answer is preceded by a sync of the log
        tail = [e for e in events if e != "sync-other"]
        check("always: a sync of the log before each answer", tail[-6:], ["sync-log", "reply"] * 3)
        events = traced("no", "no", lambda c: [c.ask("SET", "a", "1"), c.ask("SET", "b", "2"), time.sleep(1.6)])
        syncs_after = [e for e in events if e in ("sync-log", "reply")]
        check("no: no sync of the log after the first answer", "sync-log" in syncs_after[syncs_after.index("reply"):], False)
        events = traced("no", "shutdown", lambda c: [c.ask("SET", "a", "1"), c.s.sendall(cmd("SHUTDOWN")), time.sleep(0.5)])
        check("SHUTDOWN syncs the log, even under no", [e for e in events if e != "sync-other"][-2:], ["reply", "sync-log"])
        events = traced("everysec", "everysec", lambda c: [c.ask("SET", "a", "1"), c.ask("SET", "b", "2"), time.sleep(2.6)])
        syncs_after = [e for e in events if e in ("sync-log", "reply")]
        after_first = syncs_after[syncs_after.index("reply"):]
        check("everysec: the answers leave before the sync", after_first[:2], ["reply", "reply"])
        check("everysec: and the log is synced after them, once for these two writes", after_first.count("sync-log"), 1)
    else:
        print("SKIPPED the order of the calls (no strace)")

    # --- a log that cannot be written
    dd = fresh("gone-no")
    p, c = start(dd, "--appendonly=yes", "--appendfsync=no")
    shutil.rmtree(dd)
    check("under no, the answer still leaves", c.ask("SET", "a", "1"), ("+", "OK"))
    info = dict(l.split(":", 1) for l in c.ask("INFO", "persistence").decode().split("\r\n") if ":" in l)
    check("and INFO says the write failed", (info["aof_last_write_status"], int(info["aof_write_errors"]) >= 1), ("err", True))
    check("the cache goes on", c.ask("SET", "b", "2"), ("+", "OK"))
    stop(p)
    dd = fresh("gone-always")
    p, c = start(dd, "--appendonly=yes", "--appendfsync=always")
    check("a first write is answered", c.ask("SET", "a", "1"), ("+", "OK"))
    shutil.rmtree(dd)
    c.s.sendall(cmd("SET", "b", "2"))
    try:
        got = c.read()
    except (EOFError, ConnectionResetError):
        got = "closed"
    check("under always, a write that cannot be logged is not answered", got, "closed")
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        failures.append("under always, the cache did not stop")
        p.kill()
        p.wait()
    check("and the cache stops with status 6 and says why", (p.returncode, "append-only log could not be written" in p.stderr.read().decode()), (6, True))
    p.stderr.close()

    # --- what a write counts for the policy of section 37
    dd = fresh("counts")
    p, c = start(dd)
    changes = lambda: int(dict(l.split(":", 1) for l in c.ask("INFO", "persistence").decode().split("\r\n") if ":" in l)["rdb_changes_since_last_save"])
    c.ask("HSET", "h", "f", "1")
    base = changes()
    check("HSET of a field that is there is a change (it answers 0)", (c.ask("HSET", "h", "f", "2"), changes()), (0, base + 1))
    base = changes()
    c.ask("SET", "big", b"x" * 100000)
    check("a value that arrives in pieces is a change", changes(), base + 1)
    stop(p)

    # --- CONFIG and INFO
    dd = fresh("config")
    p, c = start(dd, "--appendonly=yes", "--appendfsync=always")
    check("CONFIG GET appendonly", c.ask("CONFIG", "GET", "appendonly"), [b"appendonly", b"yes"])
    check("CONFIG GET appendfsync", c.ask("CONFIG", "GET", "appendfsync"), [b"appendfsync", b"always"])
    for word in ("everysec", "no", "always"):
        check("CONFIG SET appendfsync %s" % word, c.ask("CONFIG", "SET", "appendfsync", word), ("+", "OK"))
        check("and it is what GET says", c.ask("CONFIG", "GET", "appendfsync"), [b"appendfsync", word.encode()])
    r = c.ask("CONFIG", "SET", "appendfsync", "sometimes")
    check("a wrong appendfsync is refused as Redis refuses it", r[0] == "-" and "(possibly related to argument 'appendfsync') - argument(s) must be one of the following: always, everysec, no" in r[1], True)
    r = c.ask("CONFIG", "SET", "appendonly", "no")
    check("appendonly is chosen at start", r[0] == "-" and "chosen at start" in r[1], True)
    check("and is still yes", c.ask("CONFIG", "GET", "appendonly"), [b"appendonly", b"yes"])
    c.ask("SET", "a", "1")
    c.ask("SET", "b", "2")
    info = dict(l.split(":", 1) for l in c.ask("INFO", "persistence").decode().split("\r\n") if ":" in l)
    check("aof_enabled", info["aof_enabled"], "1")
    check("aof_current_size is the file's", int(info["aof_current_size"]), os.path.getsize(log(dd)))
    check("aof_batches", info["aof_batches"], "2")
    check("aof_last_write_status", info["aof_last_write_status"], "ok")
    stop(p)
    dd = fresh("noconfig")
    p, c = start(dd)
    check("CONFIG GET appendonly, off", c.ask("CONFIG", "GET", "appendonly"), [b"appendonly", b"no"])
    check("CONFIG GET appendfsync, the default", c.ask("CONFIG", "GET", "appendfsync"), [b"appendfsync", b"everysec"])
    info = c.ask("INFO", "persistence").decode()
    check("aof_enabled, off", "aof_enabled:0" in info and "aof_current_size" not in info, True)
    stop(p)

    # --- start-up
    dd = fresh("startup")
    p, c = start(dd, "--appendonly=yes")
    check("a log is made", os.path.exists(log(dd)), True)
    head = open(log(dd), "rb").read()
    check("with a header and nothing else", (len(head), head[:8]), (32, b"CANCHOLG"))
    c.ask("SET", "a", "1")
    stop(p)
    before = open(log(dd), "rb").read()
    p, c = start(dd, "--appendonly=yes")
    c.ask("SET", "b", "2")
    stop(p)
    after = open(log(dd), "rb").read()
    check("a log that is there is appended to, not made again", after.startswith(before) and len(after) > len(before), True)
    p, c = start(None, "--appendonly=yes")
    check("--appendonly without --dir stops the start", (c is None or p.wait(timeout=5)) and p.returncode, 2)
    p.stderr.close()
    for bad in ("--appendonly=maybe", "--appendfsync=sometimes"):
        dd = fresh("bad" + bad[-3:])
        p, c = start(dd, bad)
        if c is not None:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stop(p)
                failures.append("%s was accepted" % bad)
                continue
        check("%s stops the start" % bad, p.returncode, 2)
        p.stderr.close()
    dd = fresh("off")
    p, c = start(dd)
    c.ask("SET", "a", "1")
    check("without --appendonly there is no log", os.path.exists(log(dd)), False)
    stop(p)
    # a directory where the log cannot be made stops the start with a rule tag
    dd = fresh("blocked")
    os.mkdir(log(dd))
    p, c = start(dd, "--appendonly=yes")
    if c is not None:
        p.wait(timeout=5)
    check("a log that cannot be made stops the start", (p.returncode, "aof.open" in p.stderr.read().decode()), (4, True))
    p.stderr.close()
finally:
    for p in processes:
        if p.poll() is None:
            p.kill()
            p.wait()
    shutil.rmtree(work, ignore_errors=True)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("the log has the state each write left, in whole batches, and replayed from the file alone it is the cache's keyspace")
