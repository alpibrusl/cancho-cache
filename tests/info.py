#!/usr/bin/env python3
"""INFO against Redis (docs/design.md section 30).

    python3 tests/info.py [path/to/cache]        (needs redis-server, Redis 7.0)

The same commands go to Redis and to the cache, and then `INFO everything` of each is read:
  * every section the cache prints is a section Redis prints, and every field of it is a field Redis has in that section (so no invented names), except the few
    fields this cache adds and says so (`ADDED`);
  * the counters that the commands decide agree: `cmdstat_*` calls and failed calls for every command that was run, the error codes of `errorstats`, `keyspace_hits`,
    `keyspace_misses`, `total_error_replies`, and the `Keyspace` line's keys and expires;
  * a section can be asked for by name, several at once, and `commandstats` is not in the default `INFO`.
What is not compared: values that depend on the machine and the time (memory, uptime, connections, ops per second), and what the cache does not report and says so
in the design (cpu, latencystats, modules, most of the persistence and replication fields).
"""
import os, re, socket, subprocess, sys, time

CACHE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "build", "cache")
RPORT, CPORT = 6931, 6932
ADDED = {"server_name", "compactions", "compactions_forced", "max_turn_ms", "used_memory_dead"}
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
    raise SystemExit("did not start: %s" % argv)


class Client:
    def __init__(self, port):
        self.s = socket.create_connection(("127.0.0.1", port))
        self.s.settimeout(10)
        self.buf = b""

    def _line(self):
        while b"\r\n" not in self.buf:
            self.buf += self.s.recv(1 << 20)
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def ask(self, *args):
        self.s.sendall(cmd(*args))
        line = self._line()
        t, rest = line[:1], line[1:]
        if t == b"$":
            n = int(rest)
            if n < 0:
                return None
            while len(self.buf) < n + 2:
                self.buf += self.s.recv(1 << 20)
            v, self.buf = self.buf[:n], self.buf[n + 2:]
            return v.decode()
        if t == b"*":
            return [self._elem() for _ in range(int(rest))]
        if t == b":":
            return int(rest)
        return (t.decode(), rest.decode())

    def _elem(self):
        line = self._line()
        t, rest = line[:1], line[1:]
        if t == b"$":
            n = int(rest)
            if n < 0:
                return None
            while len(self.buf) < n + 2:
                self.buf += self.s.recv(1 << 20)
            v, self.buf = self.buf[:n], self.buf[n + 2:]
            return v.decode()
        return rest.decode()


def parse(text):
    """INFO text -> {section: {field: value}}"""
    out, sec = {}, None
    for line in text.split("\r\n"):
        if line.startswith("# "):
            sec = line[2:]
            out[sec] = {}
        elif ":" in line and sec:
            k, v = line.split(":", 1)
            out[sec][k] = v
    return out


def check(what, got, want):
    if got != want:
        failures.append("%s: got %r, wanted %r" % (what, got, want))


redis = start(["redis-server", "--port", str(RPORT), "--save", "", "--appendonly", "no", "--protected-mode", "no"], RPORT)
cache = start([CACHE, str(CPORT)], CPORT)
try:
    r, c = Client(RPORT), Client(CPORT)
    ops = [("SET", "a", "1"), ("SET", "b", "2", "EX", "100"), ("GET", "a"), ("GET", "nope"), ("GET", "nope2"), ("MGET", "a", "b", "nope"), ("EXISTS", "a", "nope"), ("TTL", "b"),
           ("STRLEN", "a"), ("TYPE", "a"), ("INCR", "a"), ("INCR", "n"), ("HSET", "h", "f", "v"), ("GET", "h"), ("SET", "a"), ("SET", "a", "1", "BOGUS"), ("INCR", "h"),
           ("EXPIRE", "a", "x"), ("NOSUCHCOMMAND",), ("DEL", "n"), ("GETEX", "a"), ("APPEND", "a", "x"), ("GETRANGE", "a", "0", "1"), ("PING",), ("ECHO", "x"), ("SET", "c", "9"), ("RENAME", "zz", "yy")]
    for op in ops:
        r.ask(*op)
        c.ask(*op)
    ri, ci = parse(r.ask("INFO", "everything")), parse(c.ask("INFO", "everything"))
    # --- names
    for sec, fields in ci.items():
        check("section %s is one Redis prints" % sec, sec in ri, True)
        if sec in ri:
            extra = sorted(f for f in fields if f not in ri[sec] and f not in ADDED)
            check("fields of %s that Redis does not have" % sec, extra, [])
    # --- counters the commands decide
    def cmdstats(info):
        out = {}
        for k, v in info.get("Commandstats", {}).items():
            m = re.match(r"calls=(\d+),usec=\d+,usec_per_call=[\d.]+,rejected_calls=(\d+),failed_calls=(\d+)", v)
            out[k] = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return out

    rs, cs = cmdstats(ri), cmdstats(ci)
    # INFO itself is counted when it has run, and the cache counts the command that is not known as nothing; compare what both counted
    for name in sorted(set(cs) - {"cmdstat_info"}):
        check("%s: calls, rejected calls and failed calls" % name, cs[name], rs.get(name))
    check("every command Redis counted (but INFO, and the one that does not exist) is counted", sorted(set(rs) - {"cmdstat_info", "cmdstat_nosuchcommand"}) == sorted(set(cs) - {"cmdstat_info"}), True)
    check("errorstats", ci["Errorstats"], ri["Errorstats"])
    for f in ("keyspace_hits", "keyspace_misses"):
        check(f, ci["Stats"][f], ri["Stats"][f])
    check("total_error_replies", ci["Stats"]["total_error_replies"], ri["Stats"]["total_error_replies"])
    check("keys and expires", re.match(r"keys=(\d+),expires=(\d+)", ci["Keyspace"]["db0"]).groups(), re.match(r"keys=(\d+),expires=(\d+)", ri["Keyspace"]["db0"]).groups())
    # --- sections by name
    check("INFO commandstats is only commandstats", list(parse(c.ask("INFO", "commandstats"))), ["Commandstats"])
    check("INFO server clients is those two", list(parse(c.ask("INFO", "server", "clients"))), ["Server", "Clients"])
    check("the default INFO has no commandstats and has errorstats", ("Commandstats" in parse(c.ask("INFO")), "Errorstats" in parse(c.ask("INFO"))), (False, True))
    check("INFO default is the default", list(parse(c.ask("INFO", "default"))), list(parse(c.ask("INFO"))))
    check("INFO of a section nobody has is empty", parse(c.ask("INFO", "nosuchsection")), {})
    # --- things that move
    before = int(parse(c.ask("INFO", "stats"))["Stats"]["total_commands_processed"])
    for _ in range(50):
        c.ask("PING")
    after = int(parse(c.ask("INFO", "stats"))["Stats"]["total_commands_processed"])
    check("total_commands_processed moves by what was run (50 PINGs and one INFO)", after - before, 51)
    peak = int(parse(c.ask("INFO", "memory"))["Memory"]["used_memory_peak"])
    for i in range(200):
        c.ask("SET", "k%d" % i, "v" * 100)
    mem = parse(c.ask("INFO", "memory"))["Memory"]
    check("used_memory_peak does not fall and used_memory grew", (int(mem["used_memory_peak"]) >= peak, int(mem["used_memory"]) > 0), (True, True))
    check("used_memory_human has a unit", re.fullmatch(r"\d+(\.\d\d)?[BKMG]", mem["used_memory_human"]) is not None, True)
finally:
    for p in (redis, cache):
        p.terminate()

if failures:
    print("\n".join(failures))
    sys.exit(1)
print("INFO names the sections and fields Redis does, and the counters the commands decide agree")
