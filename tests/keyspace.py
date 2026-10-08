#!/usr/bin/env python3
"""The keyspace commands, compared with Redis where the order of the keys and the cursors differ (`docs/design.md` section 16).

    python3 tests/keyspace.py [path/to/cache]        (needs redis-server)

`tests/differential.py` compares byte for byte what does not depend on the order of the keys. Here:
  * `KEYS` with thousands of random patterns (every special byte, unclosed sets, escapes) over random keys, the *sets* of keys compared with Redis;
  * `SCAN` over a keyspace read to the end with several `COUNT`s, `MATCH` and `TYPE`: the union of the pages equals what `KEYS` gives;
  * `SCAN` while keys are deleted and added between the pages: every key that was there the whole time is answered, exactly once;
  * a long random run of `SET`/`DEL`/`APPEND`/`RENAME`/`RENAMENX` against a model, on a default cache and on one with a 1 MiB arena where
    compaction and refusals happen, ending with every key and value checked.
"""
import os, random, socket, subprocess, sys, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
REDIS_PORT, CACHE_PORT, SMALL_PORT = 6482, 6483, 6486
failures = []


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


class Client:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port))
        self.s.settimeout(10)
        self.buf = b""

    def _more(self):
        d = self.s.recv(1 << 20)
        if not d:
            raise EOFError
        self.buf += d

    def _line(self):
        while b"\r\n" not in self.buf:
            self._more()
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def read(self):
        line = self._line()
        t, rest = line[:1], line[1:]
        if t in (b"+", b"-"):
            return (t, rest)
        if t == b":":
            return int(rest)
        if t == b"$":
            n = int(rest)
            if n < 0:
                return None
            while len(self.buf) < n + 2:
                self._more()
            v, self.buf = self.buf[:n], self.buf[n + 2:]
            return v
        if t == b"*":
            n = int(rest)
            return None if n < 0 else [self.read() for _ in range(n)]
        raise ValueError(line)

    def ask(self, *args):
        self.s.sendall(cmd(*args))
        return self.read()

    def many(self, commands):
        """Pipeline several commands in one write and read all the answers."""
        self.s.sendall(b"".join(cmd(*c) for c in commands))
        return [self.read() for _ in commands]


def check(what, cond, detail=""):
    print(("ok    " if cond else "FAIL  ") + what)
    if not cond:
        failures.append(what)
        if detail:
            print("      " + detail)


def scan_all(c, *options, count=10):
    """Read a whole `SCAN` to the end; answers the list of keys (with repeats, if any)."""
    cursor, out, pages = b"0", [], 0
    while True:
        reply = c.ask("SCAN", cursor, *options, "COUNT", str(count))
        cursor, keys = reply[0], reply[1]
        out += keys
        pages += 1
        if cursor == b"0" or pages > 100000:
            return out


redis_proc = start(["redis-server", "--port", str(REDIS_PORT), "--save", "", "--appendonly", "no", "--protected-mode", "no"], REDIS_PORT)
cache_proc = start([CACHE, str(CACHE_PORT)], CACHE_PORT)
small_proc = start([CACHE, str(SMALL_PORT), "1", "200"], SMALL_PORT)
try:
    r, c = Client(REDIS_PORT), Client(CACHE_PORT)
    rnd = random.Random(20261007)
    alphabet = "ab-c*?[]\\^"

    # --- KEYS: random patterns over random keys, the sets compared with Redis
    keys = set()
    while len(keys) < 400:
        keys.add("".join(rnd.choice(alphabet + "abcabc") for _ in range(rnd.randrange(0, 7))))
    keys.discard("")
    for x in (r, c):
        x.ask("FLUSHALL")
        x.many([("SET", k, "v") for k in keys])
    check("both hold the same keys", sorted(r.ask("KEYS", "*")) == sorted(c.ask("KEYS", "*")) and len(c.ask("KEYS", "*")) == len(keys))
    bad = []
    for _ in range(4000):
        pat = "".join(rnd.choice(alphabet + "abc") for _ in range(rnd.randrange(0, 8)))
        want, got = sorted(r.ask("KEYS", pat)), sorted(c.ask("KEYS", pat))
        if want != got:
            bad.append((pat, want[:5], got[:5]))
            if len(bad) > 3:
                break
    check("KEYS: 4,000 random patterns give Redis's sets of keys", not bad, repr(bad))

    # --- SCAN read to the end
    for x in (r, c):
        x.ask("FLUSHALL")
        x.many([("SET", "k:%d" % i, "v") for i in range(1000)] + [("SET", "other:%d" % i, "v") for i in range(100)])
    everything = sorted(c.ask("KEYS", "*"))
    for count in (1, 7, 100, 100000):
        got = scan_all(c, count=count)
        check("SCAN COUNT %d reads every key exactly once" % count, sorted(got) == everything, "got %d of %d, distinct %d" % (len(got), len(everything), len(set(got))))
        check("SCAN COUNT %d, Redis reads the same set" % count, sorted(set(scan_all(r, count=count))) == everything)
    for pat in ("k:*", "other:*", "k:1?", "*:9", "nothing*"):
        want = sorted(set(scan_all(r, "MATCH", pat)))
        got = sorted(scan_all(c, "MATCH", pat))
        check("SCAN MATCH %s agrees with Redis" % pat, want == got, "%d vs %d" % (len(want), len(got)))
    check("SCAN TYPE string reads everything", sorted(scan_all(c, "TYPE", "string")) == everything)
    check("SCAN TYPE hash reads nothing", scan_all(c, "TYPE", "hash") == [])
    check("SCAN pages stop at the end of the keys", c.ask("SCAN", "0", "COUNT", "100000")[0] == b"0")

    # --- a key that has run out is not there, for KEYS, SCAN and RANDOMKEY, whether or not anything has swept it
    for x in (r, c):
        x.ask("FLUSHALL")
        x.many([("SET", "live", "v"), ("SET", "soon", "v", "PX", "60")])
    time.sleep(0.2)
    check("KEYS, SCAN and RANDOMKEY leave out a key that has expired", all(x.ask("KEYS", "*") == [b"live"] and x.ask("SCAN", "0")[1] == [b"live"] and x.ask("RANDOMKEY") == b"live" for x in (r, c)))

    # --- SCAN while the keyspace changes
    c.ask("FLUSHALL")
    stable = {"s:%d" % i for i in range(1500)}
    churn = {"c:%d" % i for i in range(1500)}
    c.many([("SET", k, "v") for k in sorted(stable | churn)])
    seen, cursor, step, added = [], b"0", 0, set()
    while True:
        reply = c.ask("SCAN", cursor, "COUNT", "37")
        cursor = reply[0]
        seen += [k.decode() for k in reply[1]]
        step += 1
        if step % 3 == 0:                       # between pages: delete some of the churn, add new keys
            doomed = [k for k in sorted(churn) if rnd.random() < 0.2][:60]
            c.many([("DEL", k) for k in doomed])
            churn -= set(doomed)
            fresh = ["n:%d:%d" % (step, i) for i in range(20)]
            c.many([("SET", k, "v") for k in fresh])
            added |= set(fresh)
        if cursor == b"0":
            break
    once = {k for k in stable if seen.count(k) == 1}
    check("SCAN while keys come and go: every key present throughout is answered, once", once == stable, "%d of %d once" % (len(once), len(stable)))
    check("SCAN while keys come and go: nothing else is made up", set(seen) <= stable | churn | added | {"c:%d" % i for i in range(1500)})

    # --- a random run against a model, on both caches
    def run_model(port, arena_note, longest=300):
        x = Client(port)
        x.ask("FLUSHALL")
        model = {}
        names = ["m:%d" % i for i in range(30)]
        refused = 0
        for step in range(3000):
            op, k, k2 = rnd.random(), rnd.choice(names), rnd.choice(names)
            if op < 0.35:
                v = bytes([97 + rnd.randrange(26)]) * rnd.choice([0, 1, 5, 9, 30, rnd.randrange(longest)])
                rep = x.ask("SET", k, v)
                if rep == (b"+", b"OK"):
                    model[k] = v
                else:
                    refused += 1
            elif op < 0.45:
                x.ask("DEL", k)
                model.pop(k, None)
            elif op < 0.6:
                v = bytes([65 + rnd.randrange(26)]) * rnd.randrange(1, 40)
                rep = x.ask("APPEND", k, v)
                if isinstance(rep, int):
                    model[k] = model.get(k, b"") + v
                else:
                    refused += 1
            elif op < 0.85:
                rep = x.ask("RENAME", k, k2)
                if rep == (b"+", b"OK"):
                    model[k2] = model.pop(k)
                elif rep == (b"-", b"ERR no such key"):
                    if k in model:
                        return False, "RENAME said no such key for %s" % k
                else:
                    refused += 1
            else:
                rep = x.ask("RENAMENX", k, k2)
                if rep == 1:
                    model[k2] = model.pop(k)
                elif rep == 0:
                    if k not in model or (k2 not in model and k != k2):
                        return False, "RENAMENX said 0 for %s -> %s" % (k, k2)
                elif rep == (b"-", b"ERR no such key"):
                    if k in model:
                        return False, "RENAMENX said no such key for %s" % k
                else:
                    refused += 1
        have = sorted(x.ask("KEYS", "*"))
        if have != sorted(k.encode() for k in model):
            return False, "keys differ: %s vs %s" % (have[:5], sorted(model)[:5])
        for k, v in model.items():
            if x.ask("GET", k) != v:
                return False, "value of %s differs" % k
        if x.ask("DBSIZE") != len(model):
            return False, "DBSIZE"
        info = x.ask("INFO")
        compactions = int([l for l in info.split(b"\r\n") if l.startswith(b"compactions:")][0].split(b":")[1])
        return True, "%d keys, %d writes refused as out of memory, %d compactions (%s)" % (len(model), refused, compactions, arena_note)

    ok, note = run_model(CACHE_PORT, "64 MiB")
    check("a random run of SET/DEL/APPEND/RENAME/RENAMENX agrees with a model: " + note, ok, note)
    ok, note = run_model(SMALL_PORT, "1 MiB", longest=6000)
    check("the same on a 1 MiB arena, where it compacts and refuses: " + note, ok and "0 compactions" not in note, note)
finally:
    for p in (redis_proc, cache_proc, small_proc):
        p.terminate()

if failures:
    print("%d failed" % len(failures))
    sys.exit(1)
print("the keyspace commands behave as Redis's do")
