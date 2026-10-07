#!/usr/bin/env python3
"""Mutation test of the keyspace commands of issue #10, slice 2 (`docs/design.md` section 16): each mutant is a deliberately wrong cache, and
the harnesses must fail on it.

    CANCHO=path/to/cancho python3 tests/keyspace_mutants.py [substring of a mutant's name]

The machinery is `tests/mutlib.py`. A mutant the harness does not fail is a hole in the harness, and this script exits 1.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mutlib import run_mutants

# (name, file, old text, new text, how to test)
MUTANTS = [
    ("the matcher's `*` does not give bytes back", "glob.cho", "                star_s = star_s + 1;\n                s = star_s;\n                p = star_p;", "                return false;", ("globunit",)),
    ("the matcher ignores the `^` of a set", "glob.cho", "        if not {\n            matched = !matched;\n        }", "        if not && false {\n            matched = !matched;\n        }", ("globunit",)),
    ("the matcher does not turn a backwards range round", "glob.cho", "                if first > last {\n                    let t = first;\n                    first = last;\n                    last = t;\n                }", "", ("globunit",)),
    ("the matcher does not read `\\` as an escape", "glob.cho", "    if ch == '\\\\' && p + 1 < n {\n        return (int_of(pattern[p + 1]) == c, p + 2);\n    }", "", ("globunit",)),
    ("KEYS answers all the keys whatever the pattern", "commands.cho", "            if all || glob.matches(pattern, key) {\n                count = count + 1;", "            if true {\n                count = count + 1;", ("py", "keyspace.py")),
    ("SCAN always says it is done", "commands.cho", "    var next = e;\n    if e >= n {\n        next = 0;\n    }", "    var next = 0;", ("py", "keyspace.py")),
    ("SCAN ignores MATCH", "commands.cho", "        filtered = !(len(pattern) == 1 && int_of(pattern[0]) == '*');", "        filtered = false;", ("py", "keyspace.py")),
    ("SCAN lets a key that has run out through", "store.cho", "    return meta[m + 6] == 0 - 2 && (meta[m + 4] == 0 || meta[m + 4] > st.now);", "    return meta[m + 6] == 0 - 2;", ("unit",)),
    ("SCAN takes a cursor that is not a number", "commands.cho", "        if c < '0' || c > '9' {\n            return (1, 0);\n        }\n        if c != '0' && first < 0 {", "        if c < '0' || c > '9' {\n            return (0, 0);\n        }\n        if c != '0' && first < 0 {", ("diff", "SCAN cursor errors")),
    ("RENAME leaves the key it overwrites", "store.cho", "    if d >= 0 && d != e {\n        remove_entry(st, d);\n    }", "", ("diff", "RENAME over an existing key")),
    ("RENAME drops the expiry", "store.cho", "    meta[m + 5] = hd;\n    meta[m + 7] = st.now;", "    meta[m + 5] = hd;\n    put_expiry(st, m, 0);\n    meta[m + 7] = st.now;", ("diff", "RENAME moves the expiry")),
    ("RENAME forgets the old record is garbage", "store.cho", "    st.dead = st.dead + header() + meta[m + 1] + meta[m + 3];\n    meta[m] = at + header();\n    meta[m + 1] = len(dst);", "    meta[m] = at + header();\n    meta[m + 1] = len(dst);", ("unit",)),
    ("RENAME leaves the old name in the index", "store.cho", "    bump(st, meta[m + 5]);\n    close_hole(st, slot);\n    st.dead", "    bump(st, meta[m + 5]);\n    st.dead", ("py", "keyspace.py")),
    ("RENAME does not record the new hash", "store.cho", "    meta[m + 5] = hd;\n    meta[m + 7] = st.now;", "    meta[m + 7] = st.now;", ("py", "keyspace.py")),
    ("RENAME does not mark the old name for WATCH", "store.cho", "    let slot = slot_of(st, e);\n    bump(st, meta[m + 5]);\n    close_hole(st, slot);", "    let slot = slot_of(st, e);\n    close_hole(st, slot);", ("diff", "RENAME dirties the old name for WATCH")),
    ("RENAMENX renames onto an existing key", "commands.cho", "    if nx && store.find(st, dst, hd) >= 0 {\n        return reply.put_integer(sc, at, 0);\n    }", "", ("diff", "RENAMENX onto an existing key")),
    ("RANDOMKEY answers nothing", "commands.cho", "        let e = store.random_visible(st);\n        if e < 0 {", "        let e = 0 - 1;\n        if e < 0 {", ("diff", "RANDOMKEY of one key")),
]

run_mutants(MUTANTS, sys.argv[1] if len(sys.argv) > 1 else "")
