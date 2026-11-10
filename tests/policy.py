#!/usr/bin/env python3
"""The snapshot policy and SHUTDOWN (docs/design.md section 37).

    python3 tests/policy.py [path/to/cache]

  * `CONFIG SET save` takes pairs of numbers (as Redis does), answers what it holds, clears with "", and refuses what is not pairs, with Redis's words;
  * the changes counter (`rdb_changes_since_last_save`) counts a write that did something: not a read, not an error, not a delete of nothing; EXEC counts what it ran;
  * a `save <seconds> <changes>` pair starts a background save when both are reached, and only then; the save settles the writes it began with and no others;
  * `SHUTDOWN` saves when a policy is set (or `SAVE` is given) and not with `NOSAVE`, answers nothing, and the cache stops with status 0; what it saved is there at the next start;
  * `--save=` sets the policy at start and a wrong one stops the start.
"""
import os, shutil, socket, subprocess, sys, tempfile, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
PORT = 6922
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


def start(directory, *extra):
    try:
        socket.create_connection(("127.0.0.1", PORT), 0.2).close()
        raise SystemExit("something is already listening on %d (a cache left by an earlier run?)" % PORT)
    except OSError:
        pass
    argv = [CACHE, str(PORT), "64", "1000000", "noeviction", "1"] + (["--dir=" + directory] if directory else []) + list(extra)
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


def info(c, field):
    for line in c.ask("INFO", "persistence").decode().split("\r\n"):
        if line.startswith(field + ":"):
            return line.split(":")[1]
    return None


def changes(c):
    return int(info(c, "rdb_changes_since_last_save"))


def wait_for(what, test, seconds=8):
    end = time.time() + seconds
    while time.time() < end:
        if test():
            return True
        time.sleep(0.05)
    failures.append("%s: did not happen within %d s" % (what, seconds))
    return False


work = tempfile.mkdtemp(prefix="policy-")
try:
    # --- CONFIG SET save
    p, c = start(None)
    check("the default policy is empty", c.ask("CONFIG", "GET", "save"), [b"save", b""])
    for text in ("900 1 300 10", "3600 1", "900 0", "1 1 2 2 3 3 4 4 5 5 6 6 7 7 8 8"):
        check("CONFIG SET save %r" % text, c.ask("CONFIG", "SET", "save", text), ("+", "OK"))
        check("CONFIG GET save after %r" % text, c.ask("CONFIG", "GET", "save"), [b"save", text.encode()])
    check("CONFIG SET save clears", c.ask("CONFIG", "SET", "save", ""), ("+", "OK"))
    check("CONFIG GET save after clearing", c.ask("CONFIG", "GET", "save"), [b"save", b""])
    c.ask("CONFIG", "SET", "save", "60 5")
    for bad in ("900", "900 1 300", "0 1", "900 -1", "abc", "900 x", "900x 1", "9 1x", "1 1 2 2 3 3 4 4 5 5 6 6 7 7 8 8 9 9"):
        r = c.ask("CONFIG", "SET", "save", bad)
        check("CONFIG SET save %r is refused" % bad, r[0] == "-" and "(possibly related to argument 'save')" in r[1], True)
        if not bad.endswith("9 9"):
            check("CONFIG SET save %r is refused as invalid" % bad, r[0] == "-" and "Invalid save parameters" in r[1], True)
        check("CONFIG SET save %r leaves the policy" % bad, c.ask("CONFIG", "GET", "save"), [b"save", b"60 5"])
    r = c.ask("CONFIG", "SET", "save", "900")
    check("the words of a refused policy", "Invalid save parameters" in r[1], True)
    stop(p)

    # --- the counter
    p, c = start(None)
    check("a fresh cache has no changes", changes(c), 0)
    c.ask("GET", "nothing")
    c.ask("PING")
    check("reads are not changes", changes(c), 0)
    c.ask("SET", "k", "v")
    check("SET is a change", changes(c), 1)
    c.ask("GET", "k")
    check("GET after it is not", changes(c), 1)
    c.ask("DEL", "missing")
    check("a DEL of nothing is not", changes(c), 1)
    c.ask("DEL", "k")
    check("a DEL of something is", changes(c), 2)
    c.ask("HSET", "k", "f", "x")
    c.ask("SET", "s", "1")
    r = c.ask("HSET", "s", "f", "x")
    check("a command that errs is not a change", changes(c), 4)
    other = Client()
    c.ask("MULTI")
    c.ask("SET", "a", "1")
    c.ask("SET", "b", "2")
    check("queued commands are not changes yet", changes(other), 4)
    c.ask("EXEC")
    check("EXEC counts what it ran", changes(other), 6)
    c.ask("MULTI")
    c.ask("SET", "a", "1")
    c.ask("DISCARD")
    check("DISCARD counts nothing", changes(other), 6)
    stop(p)

    # --- the policy starts a save
    dd = os.path.join(work, "policy")
    os.mkdir(dd)
    p, c = start(dd, "--save=1:2")
    check("--save= sets the policy", c.ask("CONFIG", "GET", "save"), [b"save", b"1 2"])
    c.ask("SET", "one", "1")
    time.sleep(2.5)
    check("one change is not enough", os.path.exists(os.path.join(dd, "dump.cho")), False)
    check("the change is still counted", changes(c), 1)
    c.ask("SET", "two", "2")
    wait_for("the policy's save", lambda: os.path.exists(os.path.join(dd, "dump.cho")) and changes(c) == 0)
    check("the save was a success", info(c, "rdb_last_bgsave_status"), "ok")
    last = int(c.ask("LASTSAVE"))
    check("LASTSAVE is now", abs(last - time.time()) < 5, True)
    c.ask("SET", "three", "3")
    check("SAVE", c.ask("SAVE"), ("+", "OK"))
    check("SAVE settles the writes", changes(c), 0)
    first = os.stat(os.path.join(dd, "dump.cho")).st_mtime
    time.sleep(2.5)
    check("no writes, no further save", os.stat(os.path.join(dd, "dump.cho")).st_mtime, first)
    # a write that arrives while the save is under way stays counted
    c.ask("CONFIG", "SET", "save", "")
    c.s.sendall(cmd("BGSAVE") + cmd("SET", "late", "1"))
    check("BGSAVE", c.read(), ("+", "Background saving started"))
    check("the SET", c.read(), ("+", "OK"))
    wait_for("the save ends", lambda: info(c, "rdb_bgsave_in_progress") == "0")
    check("a write after the save began is still to save", changes(c), 1)
    stop(p)
    p, c = start(dd)
    check("the policy's save is what the next start has", (c.ask("GET", "one"), c.ask("GET", "two")), (b"1", b"2"))
    stop(p)

    # the seconds count: three seconds from the start, not at once
    dd = os.path.join(work, "slow")
    os.mkdir(dd)
    p, c = start(dd, "--save=3:1")
    c.ask("SET", "x", "1")
    time.sleep(1.2)
    check("a pair is not due before its seconds", os.path.exists(os.path.join(dd, "dump.cho")), False)
    wait_for("the pair falls due", lambda: os.path.exists(os.path.join(dd, "dump.cho")))
    stop(p)

    # a save that cannot be made is a failed save, and the cache goes on
    dd = os.path.join(work, "gone")
    os.mkdir(dd)
    p, c = start(dd, "--save=1:1")
    shutil.rmtree(dd)
    c.ask("SET", "x", "1")
    wait_for("the failure is reported", lambda: info(c, "rdb_last_bgsave_status") == "err")
    check("the failed save leaves the writes counted", changes(c), 1)
    check("the cache serves after it", c.ask("GET", "x"), b"1")
    r = c.ask("SHUTDOWN")
    check("SHUTDOWN that cannot save says so", r[0] == "-" and "Errors trying to SHUTDOWN" in r[1], True)
    check("and stays up", c.ask("PING"), ("+", "PONG"))
    c.s.sendall(cmd("SHUTDOWN", "NOSAVE"))
    p.wait(timeout=10)
    check("SHUTDOWN NOSAVE stops it anyway", p.returncode, 0)
    p.stderr.close()

    # a save that fails part way settles nothing: 40,000 keys of 1 KiB take many turns, and the directory goes while it runs
    dd = os.path.join(work, "midway")
    os.mkdir(dd)
    p, c = start(dd)
    for i in range(40):
        c.s.sendall(b"".join(cmd("SET", "k%d" % (1000 * i + j), "x" * 1024) for j in range(1000)))
        for j in range(1000):
            c.read()
    before = changes(c)
    check("BGSAVE over a big arena", c.ask("BGSAVE"), ("+", "Background saving started"))
    shutil.rmtree(dd)
    wait_for("the save fails", lambda: info(c, "rdb_last_bgsave_status") == "err")
    check("a save that failed part way settles nothing", changes(c), before)
    stop(p)

    # a policy without a directory does nothing, and nothing breaks
    p, c = start(None, "--save=1:1")
    c.ask("SET", "x", "1")
    time.sleep(2.2)
    check("a policy without a directory leaves the count", changes(c), 1)
    check("and the cache serves", c.ask("GET", "x"), b"1")
    stop(p)

    # a wrong --save= stops the start
    for bad in ("--save=900", "--save=0:1", "--save=x"):
        p, c = start(None, bad)
        if c is not None:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stop(p)
                failures.append("%s was accepted" % bad)
                continue
        check("%s stops the start" % bad, p.returncode, 2)
        p.stderr.close()

    # --- SHUTDOWN
    def shutdown(directory, flags, *extra, expect_file, label):
        p, c = start(directory, *extra)
        c.ask("SET", "kept", "yes")
        c.s.sendall(cmd("SHUTDOWN", *flags))
        try:
            got = c.read()
        except EOFError:
            got = "closed"
        check("%s: no answer, the connection closes" % label, got, "closed")
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            failures.append("%s: did not stop" % label)
            p.kill()
            p.wait()
        check("%s: exit status" % label, p.returncode, 0)
        p.stderr.close()
        check("%s: a snapshot written" % label, os.path.exists(os.path.join(directory, "dump.cho")) if directory else False, expect_file)

    for name, flags, extra, want in (
        ("no policy, plain", (), (), False),
        ("no policy, NOSAVE", ("NOSAVE",), (), False),
        ("no policy, SAVE", ("SAVE",), (), True),
        ("a policy, plain", (), ("--save=900:1",), True),
        ("a policy, NOSAVE", ("NOSAVE",), ("--save=900:1",), False),
        ("a policy, NOW FORCE", ("NOW", "FORCE"), ("--save=900:1",), True),
    ):
        dd = os.path.join(work, "sd-" + name.replace(" ", "-").replace(",", ""))
        os.mkdir(dd)
        shutdown(dd, flags, *extra, expect_file=want, label="SHUTDOWN " + name)
        if want:
            p, c = start(dd)
            check("SHUTDOWN %s: what it saved is there" % name, c.ask("GET", "kept"), b"yes")
            stop(p)

    dd = os.path.join(work, "sdrefuse")
    os.mkdir(dd)
    p, c = start(dd, "--save=900:1")
    check("SHUTDOWN SAVE NOSAVE", c.ask("SHUTDOWN", "SAVE", "NOSAVE"), ("-", "ERR syntax error"))
    check("SHUTDOWN word", c.ask("SHUTDOWN", "foo"), ("-", "ERR syntax error"))
    check("SHUTDOWN ABORT", c.ask("SHUTDOWN", "ABORT"), ("-", "ERR No shutdown in progress."))
    check("the cache is still there", c.ask("PING"), ("+", "PONG"))
    stop(p)
    p, c = start(None)
    r = c.ask("SHUTDOWN", "SAVE")
    check("SHUTDOWN SAVE without a directory", r[0] == "-" and "SHUTDOWN" in r[1], True)
    check("and the cache stays up", c.ask("PING"), ("+", "PONG"))
    c.s.sendall(cmd("SHUTDOWN"))
    p.wait(timeout=10)
    check("SHUTDOWN without a directory and without a policy stops", p.returncode, 0)
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
print("the policy saves when it is due and not before, the counter counts what changed, and SHUTDOWN saves when it should")
