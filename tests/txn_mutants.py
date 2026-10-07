#!/usr/bin/env python3
"""Mutation test of the transaction layer (`docs/design.md` section 14.4): each mutant is a deliberately wrong cache, and the harnesses must fail on it.

    CANCHO=path/to/cancho python3 tests/txn_mutants.py

Each mutant changes one place in a copy of `src/`, builds it, and runs the harness meant to catch it (`tests/differential.py` on the
transaction cases, or `tests/transactions.py`). A mutant the harness does not fail is a hole in the harness, and this script exits 1.
"""
import os, shutil, subprocess, sys, tempfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
CANCHO = os.environ.get("CANCHO")
if not CANCHO:
    raise SystemExit("set CANCHO to the compiler")
FILES = ["cache", "resp", "store", "commands", "reply", "session", "txn"]

# (name, file, old text, new text, how to test: ("diff", substring) or ("two",))
MUTANTS = [
    ("commands run as they arrive instead of being queued", "txn.cho", "    tx[2] = tx[2] + n;\n    tx[3] = tx[3] + 1;\n    return (reply.put(sc, at, \"+QUEUED\\r\\n\"), 1);", "    return (at, 0);", ("diff", "MULTI EXEC, several")),
    ("WATCH is ignored", "txn.cho", "    if tx[4] == 0 {\n        return false;\n    }", "    if tx[4] >= 0 {\n        return false;\n    }", ("diff", "WATCH, own write, EXEC aborts")),
    ("EXEC runs a queue that had an error", "txn.cho", "        if tx[1] == 1 {\n            clear(tx);", "        if tx[1] == 2 {\n            clear(tx);", ("diff", "MULTI, unknown command, EXEC")),
    ("a wrong arity does not mark the transaction", "txn.cho", "    if a > 0 && argc != a || a < 0 && argc < 0 - a {\n        tx[1] = 1;", "    if a > 0 && argc != a || a < 0 && argc < 0 - a {\n        tx[1] = 0;", ("diff", "MULTI, wrong arity, EXEC")),
    ("an unknown command does not mark the transaction", "txn.cho", "        // Unknown: Redis refuses it, with the error the command gives, and the transaction can no longer run.\n        tx[1] = 1;", "        tx[1] = 0;", ("diff", "MULTI, unknown command, EXEC")),
    ("DISCARD leaves the queue for the next MULTI", "txn.cho", "    if reply.is_word(name, \"DISCARD\") {\n        clear(tx);", "    if reply.is_word(name, \"DISCARD\") {\n        tx[0] = 0;", ("diff", "MULTI DISCARD, then a new MULTI")),
    ("EXEC does not clear the watches", "cache.cho", "                    txn.clear(tx);\n                }\n", "                    tx[0] = 0;\n                    tx[2] = 0;\n                    tx[3] = 0;\n                }\n", ("diff", "WATCH cleared by a successful EXEC")),
    ("a runtime error stops the rest of the queue", "cache.cho", "                            qi = qi + qn;\n                            c = c + 1;", "                            qi = qi + qn;\n                            c = c + 1;\n                            if sc[at - 2] == byte_of(13) && at > 4 && sc[at - 3] == byte_of(101) {\n                                c = tx[3];\n                            }", ("diff", "runtime error in the middle")),
    ("RESET does not clear the watches", "txn.cho", "        if reply.is_word(name, \"RESET\") && argc == 1 {\n            clear(tx);\n        }\n        return (at, 0);", "        return (at, 0);", ("diff", "WATCH, RESET clears the watch")),
    ("DEL does not touch the watched key's version", "store.cho", "    let slot = slot_of(st, e);\n    bump(st, meta[m + 5]);", "    let slot = slot_of(st, e);", ("diff", "WATCH, own DEL")),
    ("EXPIRE does not touch the watched key's version", "store.cho", "    bump(st, contents(st.meta)[stride() * e + 5]);\n    put_expiry(st, stride() * e, at);", "    put_expiry(st, stride() * e, at);", ("diff", "WATCH, own EXPIRE")),
    ("SET does not touch the version", "store.cho", "    bump(st, h);\n    let e0 = find(st, key, h);", "    let e0 = find(st, key, h);", ("diff", "WATCH, own write, EXEC aborts")),
    ("FLUSHALL does not change the epoch", "store.cho", "    st.epoch = st.epoch + 1;\n", "", ("diff", "WATCH, own FLUSHALL")),
    ("EXEC aborts with an array instead of a null", "txn.cho", "            return (reply.put(sc, at, \"*-1\\r\\n\"), 1);", "            return (reply.put(sc, at, \"*0\\r\\n\"), 1);", ("diff", "WATCH, own write, EXEC aborts")),
    ("a watch is compared against the wrong bucket's version", "txn.cho", "        if store.version_of(st, tx[6 + 2 * i]) != tx[7 + 2 * i] {", "        if store.version_of(st, tx[6 + 2 * i] + 1) != tx[7 + 2 * i] {", ("two",)),
    ("a queued command's bytes are cut short", "txn.cho", "    while i < n {\n        queue[tx[2] + i] = view[i];", "    while i < n - 1 {\n        queue[tx[2] + i] = view[i];", ("diff", "MULTI EXEC, several")),
]


def run(args, env=None, timeout=600):
    try:
        return subprocess.run(args, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        # A harness that hangs on a wrong cache has not passed it.
        return subprocess.CompletedProcess(args, 124, "", "timed out")


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    holes = []
    for name, fname, old, new, how in MUTANTS:
        if only not in name:
            continue
        tmp = tempfile.mkdtemp(prefix="mutant-")
        try:
            shutil.copytree(os.path.join(ROOT, "src"), os.path.join(tmp, "src"))
            path = os.path.join(tmp, "src", fname)
            text = open(path).read()
            if text.count(old) != 1:
                print("BROKEN mutant (its text is not in %s exactly once): %s" % (fname, name))
                holes.append(name)
                continue
            open(path, "w").write(text.replace(old, new))
            binary = os.path.join(tmp, "cache")
            built = run([CANCHO, "build", "--std"] + [os.path.join(tmp, "src", f + ".cho") for f in FILES] + ["-o", binary])
            if built.returncode != 0:
                print("BROKEN mutant (does not build): %s\n%s" % (name, built.stderr[-300:]))
                holes.append(name)
                continue
            if how[0] == "diff":
                result = run([sys.executable, os.path.join(ROOT, "tests", "differential.py"), binary], env={**os.environ, "DIFF_ONLY": how[1]})
            else:
                result = run([sys.executable, os.path.join(ROOT, "tests", "transactions.py"), binary])
            if result.returncode == 0:
                print("SURVIVED  %s" % name)
                holes.append(name)
            else:
                print("caught    %s" % name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if holes:
        print("%d mutant(s) not caught" % len(holes))
        sys.exit(1)
    print("every mutant was caught")


main()
