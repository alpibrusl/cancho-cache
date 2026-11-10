#!/usr/bin/env python3
"""The append-only log, read at start (docs/design.md section 40): the crash gate.

    python3 tests/aof_replay.py [path/to/cache]

  * a log cut at every byte of its last two batches, at every batch boundary, in its header and at a sample of other places starts with the state after the last whole batch (what a
    replay of the file in Python gives), the file is cut to exactly those batches, and a write after the cut is in the log the next start reads;
  * `kill -9` while a write is in flight, under each of the three policies: the log holds every write that was answered and at most the one in flight, and the start has the state the
    log says;
  * damage: a byte changed, a kind that is none, a trailer that lies, a stretch of zeros, in a batch with others after it, refuses the start with a rule tag; the same in the last batch,
    or garbage after it, is a torn tail and is cut; a wrong magic or version refuses; a header the crash cut short is made again;
  * a log older than the snapshot does not undo it; a key whose deadline was moved out by a later record is alive; a key that ended while the cache was down is not;
  * `--appendonly=no` leaves a log alone.
"""
import os, random, shutil, socket, struct, subprocess, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aoflib

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
PORT = 6924
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
        self.s.settimeout(20)
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


def start(directory, *extra, memory="8"):
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.2).close()
        raise SystemExit("something is already listening on %d (a cache left by an earlier run?)" % PORT)
    except OSError:
        pass
    argv = [CACHE, str(PORT), memory, "100000", "noeviction", "1", "--dir=" + directory] + list(extra)
    p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    processes.append(p)
    for _ in range(200):
        if p.poll() is not None:
            return p, None
        try:
            return p, Client()
        except OSError:
            time.sleep(0.02)
    raise SystemExit("did not start")


def kill(p):
    p.kill()
    p.wait()
    p.stderr.close()


def refused(directory, *extra, memory="8"):
    """(exit status, stderr) if the cache stopped at start, or None if it is serving (and then it is killed)"""
    p, c = start(directory, *extra, memory=memory)
    if c is not None:
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            kill(p)
            return None
    err = p.stderr.read().decode()
    p.wait()
    p.stderr.close()
    return p.returncode, err


def info(c):
    return dict(l.split(":", 1) for l in c.ask("INFO", "persistence").decode().split("\r\n") if ":" in l)


def matches(c, model, label):
    """does the cache hold exactly `model` ({key: (kind, value, ends)})?"""
    have = c.ask("KEYS", "*")
    if sorted(model.keys()) != sorted(have):
        failures.append("%s: keys %r, wanted %r" % (label, sorted(have)[:6], sorted(model.keys())[:6]))
        return False
    for key in have:
        kind, value, ends = model[key]
        if c.ask("TYPE", key) != ("+", "string" if kind == 0 else "hash"):
            failures.append("%s: %r has the wrong type" % (label, key))
            return False
        if kind == 0 and c.ask("GET", key) != value:
            failures.append("%s: %r has the wrong value" % (label, key))
            return False
        if kind == 1:
            pairs = c.ask("HGETALL", key)
            if not pairs or any(x not in value for x in pairs):
                failures.append("%s: %r has the wrong fields" % (label, key))
                return False
        got = c.ask("PEXPIRETIME", key)
        if (ends == 0) != (got == -1) or (ends and abs(got - ends) > 2):
            failures.append("%s: %r has the wrong deadline (%r vs %r)" % (label, key, got, ends))
            return False
    return True


def ops(rng, n):
    """n writes that always succeed (so each leaves one batch): strings, counters, hashes, deadlines"""
    out = []
    for i in range(n):
        r = rng.random()
        k = "s%d" % rng.randint(0, 5)
        if r < 0.25:
            out.append(("SET", k, "v%d" % i))
        elif r < 0.33:
            out.append(("SETEX", k, "1000", "x%d" % i))
        elif r < 0.43:
            out.append(("INCR", "n%d" % rng.randint(0, 2)))
        elif r < 0.55:
            out.append(("HSET", "h%d" % rng.randint(0, 2), "f%d" % rng.randint(0, 3), "y%d" % i))
        elif r < 0.65:
            out.append(("DEL", k))
        elif r < 0.73:
            out.append(("APPEND", k, "a"))
        elif r < 0.80:
            out.append(("MSET", k, "m", "s%d" % rng.randint(0, 5), "n"))
        elif r < 0.88:
            out.append(("EXPIRE", k, str(rng.randint(100, 900))))
        elif r < 0.94:
            out.append(("PERSIST", k))
        elif r < 0.97:
            out.append(("FLUSHDB",))
        else:
            out.append(("SET", k, "w" * 300))
    return out


work = tempfile.mkdtemp(prefix="aofr-")
logp = lambda d: os.path.join(d, "appendonly.cho")
n_dirs = [0]


def fresh(label="d"):
    n_dirs[0] += 1
    d = os.path.join(work, "%s%d" % (label, n_dirs[0]))
    os.mkdir(d)
    return d


def build_log(seed, n, tail_small=True):
    """a directory with a log of n writes (made by a cache that was stopped with SIGKILL), and its parsed batches and batch ends"""
    d = fresh("src")
    p, c = start(d, "--appendonly=yes", "--appendfsync=no")
    rng = random.Random(seed)
    for o in ops(rng, n):
        c.ask(*o)
    if tail_small:
        c.ask("SET", "tail1", "1")
        c.ask("SET", "tail2", "2")
    kill(p)
    batches, rest, problems = aoflib.parse(logp(d))
    failures.extend(problems)
    ends, at = [], 32
    for _, rs in batches:
        at += sum(32 + len(k) + len(v) for _, k, v, _ in rs) + 32
        ends.append(at)
    return d, batches, ends


try:
    # --- cut at every byte of the last two batches, and elsewhere
    src, batches, ends = build_log(7, 42)
    data = open(logp(src), "rb").read()
    check("the log the sweep starts from is all whole batches", (ends[-1], len(batches)), (len(data), 44))
    cuts = set(range(max(0, ends[-3]), len(data) + 1)) | set(ends) | {0, 5, 8, 16, 31, 32, 33}
    rng = random.Random(1)
    cuts |= set(rng.randrange(0, len(data)) for _ in range(20))
    for cut in sorted(cuts):
        d = fresh("cut")
        open(logp(d), "wb").write(data[:cut])
        p, c = start(d, "--appendonly=yes", "--appendfsync=no")
        if c is None:
            failures.append("cut at %d: refused to start (%r)" % (cut, p.stderr.read().decode()[-80:]))
            p.stderr.close()
            continue
        whole = sum(1 for e in ends if e <= cut)
        good = ends[whole - 1] if whole else 32
        model = aoflib.replay(batches[:whole])
        ok = matches(c, model, "cut at %d" % cut)
        check("cut at %d: the file is cut to the whole batches" % cut, os.path.getsize(logp(d)), good)
        check("cut at %d: the batches replayed" % cut, info(c)["aof_replayed_batches"], str(whole))
        i = info(c)
        check("cut at %d: INFO has the size of what is left" % cut, i["aof_current_size"], str(good))
        check("cut at %d: and the bytes cut off" % cut, i["aof_truncated_bytes"], str(cut - good if cut >= 32 else cut))
        if ok and cut % 5 == 0:
            check("cut at %d: a write after it" % cut, c.ask("SET", "after", "cut"), ("+", "OK"))
            kill(p)
            p, c = start(d, "--appendonly=yes", "--appendfsync=no")
            model2 = dict(model)
            model2[b"after"] = (0, b"cut", 0)
            matches(c, model2, "cut at %d, after a write and a second start" % cut)
        kill(p)
        shutil.rmtree(d)

    # --- kill -9 with a write in flight, under each policy
    for policy in ("no", "everysec", "always"):
        for trial in range(4):
            d = fresh("kill")
            rng = random.Random(100 + trial)
            plan = ops(rng, 30)
            acked = rng.randint(3, 25)
            p, c = start(d, "--appendonly=yes", "--appendfsync=" + policy)
            for o in plan[:acked]:
                c.ask(*o)
            c.s.sendall(cmd(*plan[acked]))
            kill(p)
            batches, rest, problems = aoflib.parse(logp(d))
            failures.extend(problems)
            check("%s, trial %d: the log has every answered write and at most the one in flight" % (policy, trial), len(batches) in (acked, acked + 1), True)
            p, c = start(d, "--appendonly=yes", "--appendfsync=" + policy)
            if c is None:
                failures.append("%s, trial %d: refused to start after kill -9" % (policy, trial))
                continue
            matches(c, aoflib.replay(batches), "%s, trial %d" % (policy, trial))
            check("%s, trial %d: replayed as many batches as the log has" % (policy, trial), info(c)["aof_replayed_batches"], str(len(batches)))
            kill(p)
            shutil.rmtree(d)

    # --- damage
    def damaged(label, mutate, want):
        """want: a tag prefix (refused), or ('cut', n bytes dropped)"""
        d = fresh("dmg")
        buf = bytearray(data)
        mutate(buf)
        open(logp(d), "wb").write(bytes(buf))
        before = open(logp(d), "rb").read()
        r = refused(d, "--appendonly=yes", "--appendfsync=no")
        if isinstance(want, str):
            check("%s: refused" % label, r is not None and r[0] == 4 and any(("refusing to start: " + w) in r[1] for w in want.split("|")), True)
            if r is not None:
                check("%s: and the file is left alone" % label, open(logp(d), "rb").read(), before)
        else:
            check("%s: started" % label, r, None)
            check("%s: and the file is cut by the torn bytes" % label, len(before) - os.path.getsize(logp(d)), want[1])
        shutil.rmtree(d)

    mid = ends[10]                    # the start of batch 11
    first_record = mid + 32           # a record of it
    damaged("a byte changed in the middle of a batch with others after it", lambda b: b.__setitem__(first_record + 40, b[first_record + 40] ^ 0xFF), "aof.checksum|aof.bounds")
    damaged("a key length changed in the middle", lambda b: b.__setitem__(mid + 3, 0x7F), "aof.checksum|aof.bounds")
    damaged("a length that is negative (the top bit of a value length)", lambda b: b.__setitem__(mid + 15, 0x80), "aof.checksum|aof.bounds")
    damaged("a length that is larger than the file", lambda b: b.__setitem__(mid + 6, 0x7F), "aof.checksum|aof.bounds")
    damaged("a kind that is none", lambda b: b.__setitem__(mid + 16, 7), "aof.bad-kind")
    damaged("a trailer that lies about its length", lambda b: b.__setitem__(ends[10] - 32, b[ends[10] - 32] ^ 1), "aof.checksum|aof.bounds")
    damaged("a stretch of zeros in the middle", lambda b: b.__setitem__(slice(mid + 8, mid + 48), bytes(40)), "aof.checksum|aof.bounds|aof.bad-kind")
    damaged("the magic", lambda b: b.__setitem__(slice(0, 8), b"CANCHOLX"), "aof.bad-magic")
    damaged("the version", lambda b: b.__setitem__(8, 2), "aof.version")
    last_start = ends[-2]
    damaged("a byte changed in the last batch", lambda b: b.__setitem__(last_start + 40, b[last_start + 40] ^ 0xFF), ("cut", len(data) - last_start))
    damaged("a byte changed in the last trailer", lambda b: b.__setitem__(len(b) - 3, b[len(b) - 3] ^ 0xFF), ("cut", len(data) - last_start))
    junk = bytes(random.Random(5).randrange(256) for _ in range(100))
    damaged("random bytes after the last batch (not a prefix of anything this cache writes)", lambda b: b.extend(junk), "aof.bad-kind")
    damaged("zeros after the last batch (what a file that grew and was not written looks like)", lambda b: b.extend(bytes(100)), ("cut", 100))
    # the header cut short is made again, and a short wrong one is refused
    for label, content, expect_ok in (("an empty file", b"", True), ("a header cut at 5", b"CANCH", True), ("a short wrong header", b"XXXXX", False)):
        d = fresh("hdr")
        open(logp(d), "wb").write(content)
        r = refused(d, "--appendonly=yes", "--appendfsync=no")
        if expect_ok:
            check("%s: starts" % label, r, None)
            check("%s: and has a header again" % label, open(logp(d), "rb").read()[:8], b"CANCHOLG")
        else:
            check("%s: refused" % label, r is not None and "aof.bad-magic" in r[1], True)
        shutil.rmtree(d)
    # --appendonly=no leaves a log alone
    d = fresh("off")
    open(logp(d), "wb").write(b"not a log at all")
    p, c = start(d)
    check("--appendonly=no ignores a log, even a bad one", c.ask("DBSIZE"), 0)
    kill(p)
    check("and leaves it as it was", open(logp(d), "rb").read(), b"not a log at all")

    # a log that does not fit the arena, and one that is too big to read
    d = fresh("noroom")
    p, c = start(d, "--appendonly=yes", "--appendfsync=no", memory="8")
    check("a 3 MiB value", c.ask("SET", "big", b"z" * (3 << 20)), ("+", "OK"))
    kill(p)
    r = refused(d, "--appendonly=yes", "--appendfsync=no", memory="1")
    check("a log that does not fit the arena is refused", r is not None and r[0] == 4 and "refusing to start: aof.no-room" in r[1], True)
    d = fresh("toolarge")
    with open(logp(d), "wb") as f:
        f.truncate((1 << 30) + 1)
    r = refused(d, "--appendonly=yes", "--appendfsync=no")
    check("a log over a gigabyte is refused unread", r is not None and r[0] == 4 and "refusing to start: aof.too-large" in r[1], True)
    shutil.rmtree(d)

    # --- the log and the snapshot
    d = fresh("stale")
    p, c = start(d, "--appendonly=yes", "--appendfsync=no")
    c.ask("SET", "a", "1")
    kill(p)
    time.sleep(0.05)
    p, c = start(d)                     # without the log: the writes of this run are not in it
    c.ask("SET", "a", "2")
    time.sleep(0.05)
    check("SAVE", c.ask("SAVE"), ("+", "OK"))
    kill(p)
    p, c = start(d, "--appendonly=yes", "--appendfsync=no")
    check("a log older than the snapshot does not undo it", c.ask("GET", "a"), b"2")
    check("and says it skipped it", info(c)["aof_skipped_batches"], "1")
    c.ask("SET", "b", "3")
    kill(p)
    p, c = start(d, "--appendonly=yes", "--appendfsync=no")
    check("a write after the snapshot is in the log", (c.ask("GET", "a"), c.ask("GET", "b")), (b"2", b"3"))
    kill(p)

    d = fresh("save-inside")
    p, c = start(d, "--appendonly=yes", "--appendfsync=no")
    c.ask("SET", "a", "1")
    c.ask("SET", "b", "1")
    time.sleep(0.05)
    c.ask("SAVE")
    time.sleep(0.05)
    c.ask("SET", "a", "2")
    kill(p)
    p, c = start(d, "--appendonly=yes", "--appendfsync=no")
    check("a snapshot and the log that goes on from it", (c.ask("GET", "a"), c.ask("GET", "b")), (b"2", b"1"))
    kill(p)

    # --- deadlines
    d = fresh("deadline")
    p, c = start(d, "--appendonly=yes", "--appendfsync=no")
    c.ask("SET", "moved", "v", "EX", "1")
    c.ask("EXPIRE", "moved", "1000")
    c.ask("SET", "ended", "v", "EX", "1")
    c.ask("SET", "far", "v", "EX", "1000")
    time.sleep(1.5)
    kill(p)
    p, c = start(d, "--appendonly=yes", "--appendfsync=no")
    check("a deadline moved out by a later record: the key is alive", c.ask("TTL", "moved") > 990, True)
    check("a key that ended while the cache was down is not there", c.ask("EXISTS", "ended"), 0)
    check("a deadline in the future is kept", 990 < c.ask("TTL", "far") <= 1000, True)
    kill(p)
finally:
    for p in processes:
        if p.poll() is None:
            p.kill()
            p.wait()
    shutil.rmtree(work, ignore_errors=True)

if failures:
    print("\n".join(failures[:40]))
    sys.exit(1)
print("the log is read at start: cut anywhere, killed anywhere, it gives the state after a whole batch, and damage in the middle refuses")
