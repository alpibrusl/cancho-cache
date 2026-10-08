#!/usr/bin/env python3
"""Mutation test of the transaction layer (`docs/design.md` section 14.4): each mutant is a deliberately wrong cache, and the harnesses must fail on it.

    CANCHO=path/to/cancho python3 tests/txn_mutants.py [substring of a mutant's name]

The machinery is `tests/mutlib.py`. A mutant the harness does not fail is a hole in the harness, and this script exits 1.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mutlib import run_mutants

# (name, file, old text, new text, how to test)
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
    ("DEL does not touch the watched key's version", "store.cho", "    let slot = slot_of(st, e);\n    bump(st, meta[m + 5]);\n    st.dead = st.dead + header() + meta[m + 1] + meta[m + 3];\n    put_expiry(st, m, 0);", "    let slot = slot_of(st, e);\n    st.dead = st.dead + header() + meta[m + 1] + meta[m + 3];\n    put_expiry(st, m, 0);", ("diff", "WATCH, own DEL")),
    ("EXPIRE does not touch the watched key's version", "store.cho", "    bump(st, contents(st.meta)[stride() * e + 5]);\n    put_expiry(st, stride() * e, at);", "    put_expiry(st, stride() * e, at);", ("diff", "WATCH, own EXPIRE")),
    ("SET does not touch the version", "store.cho", "    bump(st, h);\n    let e0 = find(st, key, h);", "    let e0 = find(st, key, h);", ("diff", "WATCH, own write, EXEC aborts")),
    ("FLUSHALL does not change the epoch", "store.cho", "    st.epoch = st.epoch + 1;\n", "", ("diff", "WATCH, own FLUSHALL")),
    ("EXEC aborts with an array instead of a null", "txn.cho", "            return (reply.put(sc, at, \"*-1\\r\\n\"), 1);", "            return (reply.put(sc, at, \"*0\\r\\n\"), 1);", ("diff", "WATCH, own write, EXEC aborts")),
    ("a watch is compared against the wrong bucket's version", "txn.cho", "        if store.version_of(st, tx[6 + 2 * i]) != tx[7 + 2 * i] {", "        if store.version_of(st, tx[6 + 2 * i] + 1) != tx[7 + 2 * i] {", ("two",)),
    ("a queued command's bytes are cut short", "txn.cho", "    while i < n {\n        queue[tx[2] + i] = view[i];", "    while i < n - 1 {\n        queue[tx[2] + i] = view[i];", ("diff", "MULTI EXEC, several")),
]

run_mutants(MUTANTS, sys.argv[1] if len(sys.argv) > 1 else "")
