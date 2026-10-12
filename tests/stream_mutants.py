#!/usr/bin/env python3
"""Mutation test of the values that are larger than a block (`docs/design.md` section 22): each mutant is a deliberately wrong cache, and `tests/large.py`
(or the store's unit tests) must fail on it.

    CANCHO=path/to/cancho python3 tests/stream_mutants.py [substring of a mutant's name]

The machinery is `tests/mutlib.py`. A mutant the harness does not fail is a hole in the harness, and this script exits 1.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mutlib import run_mutants

L = ("py", "large.py")

# (name, file, old text, new text, how to test)
MUTANTS = [
    ("a value's bytes land one byte late in the record", "cache.cho", "store.staged_size(core.store, st[p + 14]) - st[p + 15], bf[", "store.staged_size(core.store, st[p + 14]) - st[p + 15] + 1, bf[", L),
    ("the CRLF after a value is not looked at", "cache.cho", "} else if int_of(bf[base + used]) != '\\r' || int_of(bf[base + used + 1]) != '\\n' {", "} else if false {", L),
    ("a staged value is not given up when the client leaves", "cache.cho", "        store.abort(core.store, st[stride() * k + 14]);\n    }\n    st[stride() * k + 13] = 0;", "    }\n    st[stride() * k + 13] = 0;", L),
    ("a flush under a value that is arriving is not noticed", "cache.cho", "            if st[p + 14] >= 0 && st[p + 17] != store.epoch_of(core.store) {", "            if false {", L),
    ("a value that is read while it is written is not noticed", "store.cho", "    return e >= 0 && e < st.fresh && meta[stride() * e + 6] == 0 - 2 && meta[stride() * e + 9] == at;", "    return e >= 0 && e < st.fresh && meta[stride() * e + 6] == 0 - 2;", L),
    ("a value written in place keeps its stamp", "store.cho", "            meta[m + 2] = len(value);\n            meta[m + 9] = stamp(st);\n", "            meta[m + 2] = len(value);\n", L),
    # Not here: the 256 KiB a turn may send. On loopback the kernel's send buffer is full long before it is reached, so a cache without the limit behaves the same;
    # it is there for a reader as fast as the writer, and nothing here can make one.
    ("a compaction drops a staged record", "store.cho", "if (meta[m + 6] == 0 - 2 || meta[m + 6] == staging()) && meta[m] == from + header() {", "if meta[m + 6] == 0 - 2 && meta[m] == from + header() {", L),
    ("a committed value keeps the old record too", "store.cho", "        st.dead = st.dead + header() + meta[m0 + 1] + meta[m0 + 3];\n        put32(data, meta[m] - header(), e0);", "        put32(data, meta[m] - header(), e0);", ("unit", "")),
    ("a committed value keeps the old kind", "store.cho", "        meta[m0 + 8] = kind_string();\n        meta[m0 + 9] = stamp(st);", "        meta[m0 + 9] = stamp(st);", L),
    ("a header longer than a block is waited for", "resp.cho", "if k == argc - 1 && start + size + 2 > block() {", "if k == argc - 1 && start + size + 2 > block() + 200 {", L),
    ("a bulk longer than Redis allows is read", "resp.cho", "if size == 0 - 2 || size > max_bulk() {", "if size == 0 - 2 {", L),
    ("any command may stream", "stream.cho", "    return argc == 4 && (reply.is_word(name, \"SETEX\") || reply.is_word(name, \"PSETEX\"));", "    return true;", L),
    ("a big SET in MULTI is not refused", "cache.cho", "                        tx[1] = 1;\n                        at = reply.put(sc, at, \"-ERR transaction queue is full\\r\\n\");\n                    } else {\n                        let (after, e2, x2)", "                        at = reply.put(sc, at, \"-ERR transaction queue is full\\r\\n\");\n                    } else {\n                        let (after, e2, x2)", L),
    ("GETSET copies a value too long for the buffer", "commands.cho", "        if e >= 0 && len(store.value(st, e)) > max_string() {\n            return too_large(sc, at);\n        }\n        if e >= 0 {\n            o = reply.put_bulk(sc, at, store.value(st, e));", "        if e >= 0 {\n            o = reply.put_bulk(sc, at, store.value(st, e));", L),
    ("GETRANGE copies more than the buffer holds", "commands.cho", "    if end + 1 - start > max_string() {\n        return too_large(sc, at);\n    }\n", "", L),
    ("MGET copies a value too long for the buffer", "commands.cho", "            } else if len(store.value(st, e)) > max_string() {\n                // An error as an element keeps the array the shape it was promised.\n                o = too_large(sc, o);\n            } else {", "            } else if false {\n                o = too_large(sc, o);\n            } else {", L),
    ("an abandoned staged value's record is not garbage", "store.cho", "    st.dead = st.dead + header() + meta[m + 1] + meta[m + 3];\n    meta[m + 6] = st.free_head;\n    st.free_head = e;\n    st.staged = st.staged - 1;", "    meta[m + 6] = st.free_head;\n    st.free_head = e;\n    st.staged = st.staged - 1;", ("unit", "")),
    ("a committed value does not touch the version", "store.cho", "    let h = hash_of(st, key);\n    bump(st, h);\n    let e0 = find(st, key, h);", "    let h = hash_of(st, key);\n    let e0 = find(st, key, h);", L),
]

run_mutants(MUTANTS, sys.argv[1] if len(sys.argv) > 1 else "")
