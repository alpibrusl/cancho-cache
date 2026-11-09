#!/usr/bin/env python3
"""The snapshot (docs/design.md section 35): SAVE, the load at start, and what a damaged file does.

    python3 tests/persist.py [path/to/cache]

  * a randomised dataset (strings of every size class, binary bytes, empty values, hashes, keys with and without an expiry) goes through SAVE and a restart and comes back as it was,
    each expiry to within the time the restart took; a key that ended while the cache was down is not there;
  * the records of two snapshots of a loaded cache are the same bytes (the format is canonical for what the store holds);
  * every way a file can be wrong refuses to start, with exit status 4 and a rule tag, and never anything else: a file cut at each length class, a byte altered at many places, a
    wrong magic and version, and files that are internally wrong but carry a valid checksum (a count that is not the records', a record longer than the file, an unknown kind, a
    value that does not fit the arena);
  * SAVE without a directory, with arguments, a directory that is not there, and a left-over temporary file.
"""
import os, random, shutil, socket, struct, subprocess, sys, tempfile, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
PORT = 6921
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
            self.buf += self.s.recv(1 << 20)
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


def start(directory, *extra, memory="64"):
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.2).close()
        raise SystemExit("something is already listening on %d (a cache left by an earlier run?)" % PORT)
    except OSError:
        pass
    argv = [CACHE, str(PORT), memory, "1000000", "noeviction", "1"] + (["--dir=" + directory] if directory else []) + list(extra)
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


def refused(directory, **kw):
    """start on a directory and answer (exit status, the last line of stderr) if the cache stopped, or None if it is serving"""
    p, c = start(directory, **kw)
    if c is not None:
        # The socket is bound before the file is read, so a connection can be taken by the kernel just before the cache stops: give it a moment to do so.
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            stop(p)
            return None
    err = p.stderr.read().decode()
    p.wait()
    p.stderr.close()
    return p.returncode, err.strip().split("\n")[-1]


# --- CRC-64/XZ, to make files that are wrong inside and right outside
def crc64(data):
    poly = 0xC96C5795D7870F42
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ poly if c & 1 else c >> 1
        table.append(c)
    crc = 0xFFFFFFFFFFFFFFFF
    for b in data:
        crc = table[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFFFFFFFFFF


def seal(body):
    return body + struct.pack("<Q", crc64(body))


def header(count, version=1, magic=b"CANCHODB", when=0):
    return magic + struct.pack("<QQQ", version, count, when)


def record(key, value, kind=0, ends=0, klen=None, vlen=None):
    return struct.pack("<QQQQ", len(key) if klen is None else klen, len(value) if vlen is None else vlen, kind, ends) + key + value


work = tempfile.mkdtemp(prefix="persist-")
try:
    rnd = random.Random(20261108)
    # --- SAVE needs a directory
    p, c = start(None)
    check("SAVE without --dir", c.ask("SAVE")[0], "-")
    check("SAVE without --dir says so", "persistence is off" in c.ask("SAVE")[1], True)
    stop(p)
    d = os.path.join(work, "a")
    os.mkdir(d)
    p, c = start(d)
    check("SAVE with an argument", c.ask("SAVE", "x")[1].startswith("ERR wrong number of arguments"), True)
    check("SAVE of an empty cache", c.ask("SAVE"), ("+", "OK"))
    check("a file is there, and no temporary one", sorted(os.listdir(d)), ["dump.cho"])
    check("an empty snapshot is the header and the trailer", os.path.getsize(os.path.join(d, "dump.cho")), 40)
    # --- a randomised dataset
    model = {}
    sizes = [0, 1, 7, 63, 64, 65, 255, 1000, 4096, 16000, 16384, 100000]
    for i in range(300):
        key = bytes(rnd.randrange(256) for _ in range(rnd.choice([1, 2, 10, 50]))) + b":%d" % i
        value = bytes(rnd.randrange(256) for _ in range(rnd.choice(sizes)))
        if rnd.random() < 0.2:
            fields = {bytes([97 + j]) * rnd.randrange(1, 8): bytes(rnd.randrange(256) for _ in range(rnd.randrange(0, 30))) for j in range(rnd.randrange(1, 6))}
            args = []
            for f, v in fields.items():
                args += [f, v]
            c.ask("HSET", key, *args)
            model[key] = ("hash", fields, None)
        else:
            ttl = rnd.choice([None, None, 100, 3600])
            if ttl:
                c.ask("SETEX", key, str(ttl), value)
            else:
                c.ask("SET", key, value)
            model[key] = ("string", value, ttl)
    c.ask("SET", "ends-soon", "x", "PX", "300")
    c.ask("SET", "ends-while-down", "x", "PX", "1500")
    time.sleep(0.5)
    check("SAVE of the dataset", c.ask("SAVE"), ("+", "OK"))
    snap1 = open(os.path.join(d, "dump.cho"), "rb").read()
    stop(p)
    time.sleep(1.5)
    # --- restart and compare
    p, c = start(d)
    check("DBSIZE after restart", c.ask("DBSIZE"), len(model))
    check("a key that ended while the cache was down is not there", c.ask("GET", "ends-while-down"), None)
    wrong = 0
    for key, (kind, value, ttl) in model.items():
        if kind == "string":
            got = c.ask("GET", key)
            if got != value:
                wrong += 1
            if ttl:
                left = c.ask("TTL", key)
                if not (ttl - 5 <= left <= ttl):
                    wrong += 1
            elif c.ask("TTL", key) != -1:
                wrong += 1
        else:
            flat = c.ask("HGETALL", key)
            got = dict(zip(flat[0::2], flat[1::2]))
            if got != value:
                wrong += 1
    check("keys that came back wrong", wrong, 0)
    check("a key that ended meanwhile is not there", c.ask("GET", "ends-soon"), None)
    # --- the same bytes again
    check("SAVE after the load", c.ask("SAVE"), ("+", "OK"))
    snap2 = open(os.path.join(d, "dump.cho"), "rb").read()
    def records(snapshot):
        out, at = [], 32
        while at < len(snapshot) - 8:
            klen, vlen, kind, ends = struct.unpack("<QQQQ", snapshot[at:at + 32])
            end = at + 32 + klen + vlen
            out.append((snapshot[at + 32:at + 32 + klen], snapshot[at:end]))
            at = end
        return out

    kept = [r for k, r in records(snap1) if k != b"ends-while-down"]
    check("a snapshot of a loaded cache has the same records, in the same order, but for the key that ended", [r for k, r in records(snap2)], kept)
    stop(p)

    # --- every way a file can be wrong
    good = snap1

    def with_file(name, data, expect, **kw):
        dd = os.path.join(work, "bad-" + name)
        os.mkdir(dd)
        open(os.path.join(dd, "dump.cho"), "wb").write(data)
        got = refused(dd, **kw)
        check("%s refuses to start" % name, got is not None and got[0] == 4, True)
        check("%s names its rule" % name, got[1] if got else None, "refusing to start: " + expect)

    for cut in (0, 1, 7, 8, 31, 39, 40, 60, len(good) // 2, len(good) - 9, len(good) - 8, len(good) - 1):
        if cut < len(good):
            with_file("cut-at-%d" % cut, good[:cut], "persist.truncated" if cut < 40 else "persist.checksum")
    for n in range(40):
        at = rnd.randrange(len(good))
        data = bytearray(good)
        data[at] ^= 1 << rnd.randrange(8)
        got_tag = None
        dd = os.path.join(work, "flip-%d" % n)
        os.mkdir(dd)
        open(os.path.join(dd, "dump.cho"), "wb").write(bytes(data))
        got = refused(dd)
        check("a bit altered at %d refuses to start" % at, got is not None and got[0] == 4 and got[1].startswith("refusing to start: persist."), True)
    # each of the eight bytes of the checksum, one at a time (both halves of it count)
    for n in range(8):
        data = bytearray(good)
        data[len(good) - 8 + n] ^= 0x55
        with_file("trailer-byte-%d" % n, bytes(data), "persist.checksum")
    with_file("magic", seal(header(0, magic=b"NOTMINE!")), "persist.bad-magic")
    with_file("version", seal(header(0, version=2)), "persist.version")
    with_file("count-too-many", seal(header(2) + record(b"k", b"v")), "persist.record-bounds")
    with_file("count-too-few", seal(header(0) + record(b"k", b"v")), "persist.count")
    with_file("key-longer-than-file", seal(header(1) + record(b"k", b"v", klen=1 << 40)), "persist.record-bounds")
    with_file("value-longer-than-file", seal(header(1) + record(b"k", b"v", vlen=1 << 62)), "persist.record-bounds")
    with_file("negative-length", seal(header(1) + record(b"k", b"v", klen=(1 << 64) - 1)), "persist.record-bounds")
    with_file("unknown-kind", seal(header(1) + record(b"k", b"v", kind=7)), "persist.bad-kind")
    with_file("does-not-fit", seal(header(1) + record(b"k", b"v" * (3 << 20))), "persist.no-room", memory="1")
    # a good file of this size does load on a 64 MiB arena
    dd = os.path.join(work, "fits")
    os.mkdir(dd)
    open(os.path.join(dd, "dump.cho"), "wb").write(seal(header(1) + record(b"k", b"v" * (3 << 20))))
    p, c = start(dd)
    check("the same value fits a 64 MiB arena", c.ask("STRLEN", "k"), 3 << 20)
    stop(p)
    # a key that has already ended is not loaded at all: this one would not fit a 1 MiB arena, and the start does not notice it
    dd = os.path.join(work, "ended")
    os.mkdir(dd)
    open(os.path.join(dd, "dump.cho"), "wb").write(seal(header(1) + record(b"k", b"v" * (3 << 20), ends=1)))
    p, c = start(dd, memory="1")
    check("an ended key too big for the arena is not loaded, and not refused", c.ask("DBSIZE") if c else "refused", 0)
    stop(p)
    # --- the directory
    got = refused(os.path.join(work, "no-such-dir"))
    check("a directory that is not there refuses to start", got, (4, "refusing to start: persist.io"))
    # a left-over temporary file does not stop a SAVE, and the old snapshot is replaced whole
    dd = os.path.join(work, "tmp")
    os.mkdir(dd)
    open(os.path.join(dd, "dump.tmp"), "wb").write(b"half of a snapshot")
    p, c = start(dd)
    c.ask("SET", "a", "1")
    check("SAVE over a left-over temporary file", c.ask("SAVE"), ("+", "OK"))
    check("only the snapshot is left", sorted(os.listdir(dd)), ["dump.cho"])
    stop(p)
    # --- the order of the calls, read from the kernel's side: the file is synced before it is renamed into place, and the directory after
    if shutil.which("strace"):
        dd = os.path.join(work, "traced")
        os.mkdir(dd)
        trace = os.path.join(work, "trace.txt")
        argv = ["strace", "-f", "-o", trace, "-e", "trace=openat,fsync,fdatasync,rename,renameat,renameat2", CACHE, str(PORT), "64", "1000000", "noeviction", "1", "--dir=" + dd]
        tp = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(tp)
        c = None
        for _ in range(100):
            try:
                c = Client()
                break
            except OSError:
                time.sleep(0.05)
        c.ask("SET", "a", "1")
        check("SAVE under strace", c.ask("SAVE"), ("+", "OK"))
        # strace's child is the cache: stop that, and strace ends with it
        subprocess.run(["pkill", "-TERM", "-P", str(tp.pid)])
        tp.wait(timeout=20)
        lines = open(trace).read().split("\n")
        events = []
        fd_tmp, fd_dir = None, None
        for l in lines:
            body = l.split(None, 1)[1] if len(l.split(None, 1)) > 1 else l
            if "dump.tmp" in body and "openat" in body and "O_CREAT" in body and "= " in body and not body.rstrip().endswith("-1 ENOENT"):
                fd_tmp = int(body.rsplit("= ", 1)[1].split()[0])
                events.append("create")
            elif "openat" in body and "O_DIRECTORY" in body and dd in body and "= " in body:
                fd_dir = int(body.rsplit("= ", 1)[1].split()[0])
            elif body.startswith("fsync(") or body.startswith("fdatasync("):
                fd = int(body[body.index("(") + 1:body.index(")")])
                events.append("sync-file" if fd == fd_tmp else "sync-dir" if fd == fd_dir else "sync-other")
            elif body.startswith("rename") and "dump.tmp" in body and "dump.cho" in body:
                events.append("rename")
        check("create, sync the file, rename, sync the directory (in that order)", events, ["create", "sync-file", "rename", "sync-dir"])
    else:
        print("SKIPPED the order of the calls (no strace)")
    # a SAVE that cannot be written says so, and leaves the old snapshot as it was
    dd = os.path.join(work, "blocked")
    os.mkdir(dd)
    p, c = start(dd)
    c.ask("SET", "a", "1")
    check("a first SAVE", c.ask("SAVE"), ("+", "OK"))
    before = open(os.path.join(dd, "dump.cho"), "rb").read()
    c.ask("SET", "b", "2")
    os.mkdir(os.path.join(dd, "dump.tmp"))
    got = c.ask("SAVE")
    check("a SAVE that cannot be written is an error", got[0], "-")
    check("and says why", "could not be written" in got[1], True)
    check("and the old snapshot is as it was", open(os.path.join(dd, "dump.cho"), "rb").read(), before)
    os.rmdir(os.path.join(dd, "dump.tmp"))
    check("the next SAVE works", c.ask("SAVE"), ("+", "OK"))
    stop(p)
    # an absent file is a first start
    dd = os.path.join(work, "empty")
    os.mkdir(dd)
    p, c = start(dd)
    check("a first start has nothing", c.ask("DBSIZE"), 0)
    stop(p)
finally:
    # whatever happened, no server is left running to fool the next run
    for p in processes:
        if p.poll() is None:
            p.kill()
            p.wait()
    shutil.rmtree(work, ignore_errors=True)

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("the snapshot round-trips, and every damaged file refuses to start with a rule tag")
