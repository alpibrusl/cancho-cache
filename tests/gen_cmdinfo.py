#!/usr/bin/env python3
"""Write src/cmdinfo.cho: what `COMMAND INFO` answers for each command the cache implements, as Redis 7.0.15 answers it.

    python3 tests/gen_cmdinfo.py [path/to/cmdinfo.cho]      (needs redis-server 7.0.x; CI runs it and fails if the file changes)

The table is data copied from the oracle, one entry per command in RESP2 and in RESP3 (the two differ in the types of the flag lists and key specs),
so the cache's `COMMAND` and Redis's cannot disagree about arity, flags, key positions or key specs. The list of commands is the one in
`src/*.cho` that the dispatcher knows (`is_word(name, "...")`), minus nothing: a name Redis does not know stops the generator.
"""
import glob, os, re, socket, subprocess, sys, time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PORT = 6794
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "src", "cmdinfo.cho")
DOCS_OUT = os.path.join(os.path.dirname(OUT), "cmddocs.cho") if len(sys.argv) <= 1 else OUT[:-4] + "_docs.cho"


def names():
    found = set()
    for path in glob.glob(os.path.join(ROOT, "src", "*.cho")):
        if path.endswith("cmdinfo.cho"):
            continue
        found |= set(re.findall(r'is_word\(name, "([A-Z]+)"\)', open(path).read()))
    return sorted(found)


def cmd(*args):
    out = b"*%d\r\n" % len(args)
    for a in args:
        a = a.encode() if isinstance(a, str) else a
        out += b"$%d\r\n%s\r\n" % (len(a), a)
    return out


def parse(raw, i=0):
    """One RESP value from raw[i:]: answers (node, next). A leaf is its bytes; an aggregate is (head, children), head being its first line."""
    j = raw.index(b"\r\n", i) + 2
    head = raw[i:j]
    t = head[:1]
    if t in (b"+", b"-", b":", b"_", b","):
        return raw[i:j], j
    if t == b"$":
        n = int(head[1:-2])
        if n < 0:
            return raw[i:j], j
        return raw[i:j + n + 2], j + n + 2
    if t in (b"*", b"~", b">"):
        n, kids = int(head[1:-2]), []
        for _ in range(n):
            kid, j = parse(raw, j)
            kids.append(kid)
        return (head, kids), j
    if t == b"%":
        n, kids = int(head[1:-2]), []
        for _ in range(2 * n):
            kid, j = parse(raw, j)
            kids.append(kid)
        return (head, kids), j
    raise SystemExit("cannot parse %r" % raw[i:i + 20])


def emit(node):
    if isinstance(node, bytes):
        return node
    return node[0] + b"".join(emit(k) for k in node[1])


def name_of(entry):
    leaf = entry[1][0]
    return leaf.split(b"\r\n", 1)[1][:-2]


def canonical(raw):
    """Redis lists a container command's subcommands in the order of a dictionary that differs from one process to the next, so the table
    (which CI regenerates and compares) lists them sorted by name."""
    node, end = parse(raw)
    assert end == len(raw), "trailing bytes after an entry"
    subs = node[1][9]
    subs[1].sort(key=name_of)
    return emit(node), node


def canonical_docs(raw, resp3):
    """`COMMAND DOCS name` answers a one-entry map: the name, then its docs. Answers (name, bytes of the docs value), with a container command's
    "subcommands" map listed sorted by key (Redis's order of a dictionary differs from one process to the next)."""
    node, end = parse(raw)
    assert end == len(raw) and len(node[1]) == 2, "unexpected COMMAND DOCS reply"
    docs = node[1][1]
    kids = docs[1]
    for i in range(0, len(kids), 2):
        if kids[i] == b"$11\r\nsubcommands\r\n":
            sub = kids[i + 1]
            pairs = [(sub[1][j], sub[1][j + 1]) for j in range(0, len(sub[1]), 2)]
            pairs.sort(key=lambda p: p[0])
            sub[1][:] = [x for pair in pairs for x in pair]
    return emit(node[1][0]), emit(docs)


def ints_of(entry):
    """(arity, first key, last key, step) of a parsed entry: its elements 1, 3, 4 and 5."""
    def num(leaf):
        assert leaf[:1] == b":", leaf
        return int(leaf[1:-2])
    k = entry[1]
    return num(k[1]), num(k[3]), num(k[4]), num(k[5])


def ask(sock, data):
    """Send one command and read until the reply is complete (a reply that stops arriving for 10 seconds is an error)."""
    sock.sendall(data)
    sock.settimeout(10)
    got = b""
    while True:
        chunk = sock.recv(1 << 20)
        if not chunk:
            raise SystemExit("Redis closed the connection")
        got += chunk
        try:
            node, end = parse(got)
        except (ValueError, IndexError):
            continue
        if end == len(got):
            return got


def talk(sock, data, quiet=0.15):
    sock.sendall(data)
    sock.settimeout(quiet)
    got = b""
    try:
        while True:
            chunk = sock.recv(1 << 20)
            if not chunk:
                break
            got += chunk
    except socket.timeout:
        pass
    return got


def main():
    proc = subprocess.Popen(["redis-server", "--port", str(PORT), "--save", "", "--appendonly", "no", "--protected-mode", "no"], stdout=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", PORT), 0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        version = subprocess.run(["redis-cli", "-p", str(PORT), "INFO", "server"], capture_output=True, text=True).stdout
        m = re.search(r"redis_version:(\S+)", version)
        if not m or not m.group(1).startswith("7.0."):
            raise SystemExit("the oracle must be Redis 7.0.x, not %s" % (m.group(1) if m else "unknown"))
        entries = []
        docs_tables = []
        for proto in (2, 3):
            s = socket.create_connection(("127.0.0.1", PORT))
            if proto == 3:
                talk(s, cmd("HELLO", "3"))
            table = {}
            docs = {}
            for name in names():
                raw = ask(s, cmd("COMMAND", "INFO", name))
                if not raw.startswith(b"*1\r\n"):
                    raise SystemExit("Redis does not know %s: %r" % (name, raw[:40]))
                table[name] = canonical(raw[4:])
                docs[name] = canonical_docs(ask(s, cmd("COMMAND", "DOCS", name)), proto == 3)
            entries.append(table)
            docs_tables.append(docs)
            s.close()
        lines = []
        w = lines.append
        w("edition 5;")
        w("")
        w("module cmdinfo;")
        w("")
        w("// `cmdinfo` -- what `COMMAND INFO` answers for each command this cache implements, copied from Redis 7.0 (`docs/design.md` section 26).")
        w("// GENERATED by `tests/gen_cmdinfo.py` from the oracle; CI regenerates it and fails if it differs. Do not edit by hand.")
        w("")
        w("import reply;")
        w("")
        w("// How many commands the table holds.")
        w("pub fn count() -> [] int {")
        w("    return %d;" % len(names()))
        w("}")
        w("")

        def lit(b):
            s = b.decode("ascii")
            assert '"' not in s and "\\" not in s and all(c in "\r\n" or 32 <= ord(c) < 127 for c in s), s[:60]
            return '"' + s.replace("\r", "\\r").replace("\n", "\\n") + '"'

        w("// Write the entry of the command `name` (any case) from `at` and answer `(true, where it ends)`, or `(false, at)` if it is not in the table.")
        w("pub fn put_entry[&w, &b](name: &w [byte], sc: &!b [byte], at: int, resp3: bool) -> [] (bool, int) {")
        for n in names():
            w('    if reply.is_word(name, "%s") {' % n)
            w("        if resp3 {")
            w("            return (true, reply.put(sc, at, %s));" % lit(entries[1][n][0]))
            w("        }")
            w("        return (true, reply.put(sc, at, %s));" % lit(entries[0][n][0]))
            w("    }")
        w("    return (false, at);")
        w("}")
        w("")
        w("// What `COMMAND GETKEYS` needs of a command: `(known, arity, first key, last key, step)` as Redis 7.0 gives them. `sub` is the subcommand word of a")
        w("// container command (`OBJECT ENCODING`), and is only read for those; an unknown name or subcommand is `(false, 0, 0, 0, 0)`.")
        w("pub fn key_spec[&w, &s](name: &w [byte], sub: &s [byte]) -> [] (bool, int, int, int, int) {")
        for n in names():
            node = entries[0][n][1]
            subs = node[1][9][1]
            w('    if reply.is_word(name, "%s") {' % n)
            if subs:
                for sb in subs:
                    full = name_of(sb).decode()
                    a, f, l, st = ints_of(sb)
                    w('        if reply.is_word(sub, "%s") {' % full.split("|")[1].upper())
                    w("            return (true, %d, %d, %d, %d);" % (a, f, l, st))
                    w("        }")
                w("        return (false, 0, 0, 0, 0);")
            else:
                a, f, l, st = ints_of(node)
                w("        return (true, %d, %d, %d, %d);" % (a, f, l, st))
            w("    }")
        w("    return (false, 0, 0, 0, 0);")
        w("}")
        w("")
        w("// Whether `name` (any case) is a command in the table.")
        w("pub fn known[&w](name: &w [byte]) -> [] bool {")
        for n in names():
            w('    if reply.is_word(name, "%s") {' % n)
            w("        return true;")
            w("    }")
        w("    return false;")
        w("}")
        w("")
        w("// The names, lower case, as `COMMAND LIST` gives them.")
        w("pub fn put_names[&b](sc: &!b [byte], at: int) -> [] int {")
        w("    var o = at;")
        for n in names():
            w('    o = reply.put_bulk(sc, o, "%s");' % n.lower())
        w("    return o;")
        w("}")
        w("")
        w("// Every entry, one after another (`COMMAND` and `COMMAND INFO` with no names).")
        w("pub fn put_all[&b](sc: &!b [byte], at: int, resp3: bool) -> [] int {")
        w("    var o = at;")
        for n in names():
            w('    o = put_entry("%s", sc, o, resp3).1;' % n)
        w("    return o;")
        w("}")
        while lines[-1] == "":
            lines.pop()
        open(OUT, "w").write("\n".join(lines) + "\n")
        # The docs are 130 KB of text of their own, in a module of their own (a source file may not pass 2,000 lines, and the formatter will not put an
        # entry on fewer than the six lines it has).
        lines = []
        w = lines.append
        w("edition 5;")
        w("")
        w("module cmddocs;")
        w("")
        w("// `cmddocs` -- what `COMMAND DOCS` answers for each command this cache implements, copied from Redis 7.0 (`docs/design.md` section 26).")
        w("// GENERATED by `tests/gen_cmdinfo.py` from the oracle; CI regenerates it and fails if it differs. Do not edit by hand.")
        w("")
        w("import reply;")
        w("")
        w("// `COMMAND DOCS name`: the name and its docs map (summary, since, group, complexity, arguments, and a container's subcommands), as Redis 7.0 gives them,")
        w("// written from `at`; `(false, at)` if the command is not in the table.")
        w("pub fn put_docs[&w, &b](name: &w [byte], sc: &!b [byte], at: int, resp3: bool) -> [] (bool, int) {")
        for n in names():
            w('    if reply.is_word(name, "%s") {' % n)
            w("        if resp3 {")
            w("            return (true, reply.put(sc, at, %s));" % lit(docs_tables[1][n][0] + docs_tables[1][n][1]))
            w("        }")
            w("        return (true, reply.put(sc, at, %s));" % lit(docs_tables[0][n][0] + docs_tables[0][n][1]))
            w("    }")
        w("    return (false, at);")
        w("}")
        w("")
        w("// Every command's docs, one after another (`COMMAND DOCS` with no names).")
        w("pub fn put_all_docs[&b](sc: &!b [byte], at: int, resp3: bool) -> [] int {")
        w("    var o = at;")
        for n in names():
            w('    o = put_docs("%s", sc, o, resp3).1;' % n)
        w("    return o;")
        w("}")
        w("")
        while lines[-1] == "":
            lines.pop()
        open(DOCS_OUT, "w").write("\n".join(lines) + "\n")
        print("%d commands; %s %d bytes, %s %d bytes" % (len(names()), OUT, os.path.getsize(OUT), DOCS_OUT, os.path.getsize(DOCS_OUT)))
    finally:
        proc.terminate()
        proc.wait()


main()
