#!/usr/bin/env python3
"""Mutation test of reading the append-only log at start (`docs/design.md` section 40): each mutant is a deliberately wrong cache, and `tests/aof_replay.py` must fail on it.

    CANCHO=path/to/cancho python3 tests/aof_replay_mutants.py [substring of a mutant's name]

The machinery is `tests/mutlib.py`. A mutant the harness does not fail is a hole in the harness, and this script exits 1.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mutlib import run_mutants

S = ("py", "aof_replay.py")
# (name, file, old text, new text, how to test)
MUTANTS = [
    ("a refused log starts the cache", "cache.cho", "                        if replayed != 0 && replayed != 1 {\n                            bad_dump = replayed;\n                        }", "", S),
    ("the records are not applied", "aof.cho", "            let status = apply(bytes, at, end - 32, st);", "            let status = 0;", S),
    ("a key that is gone is kept", "aof.cho", "            store.delete(st, key, store.hash_of(st, key));\n", "", S),
    ("a new deadline is not applied", "aof.cho", "                store.set_expiry(st, e, relative_of(st, ends));\n", "", S),
    ("a flush is not applied", "aof.cho", "        } else {\n            store.clear(st);\n        }", "        } else {\n        }", S),
    ("a hash is a string", "aof.cho", "            if e >= 0 {\n                store.set_kind(st, e, kind);\n            }", "", S),
    ("a deadline already past is stored as past", "aof.cho", "    if relative <= store.now_of(st) {\n        relative = store.now_of(st) + 1;\n    }", "", S),
    ("a key with no deadline is given one", "aof.cho", "    if ends == 0 {\n        return 0;\n    }\n    var relative", "    var relative", S),
    ("a log older than the snapshot is applied", "aof.cho", "        if get_u64(bytes, end - 24) < store.snap_ms_of(st) {", "        if false {", S),
    ("every batch is older than the snapshot", "aof.cho", "        if get_u64(bytes, end - 24) < store.snap_ms_of(st) {", "        if true {", S),
    ("the snapshot's time is not remembered", "persist.cho", "    store.set_snap_ms(st, get_u64(bytes, 24));\n", "", S),
    ("damage in the middle is a torn tail", "aof.cho", "    var p = from;\n    while p + 32 <= len(bytes) {\n        // The kind is the cheap test", "    var p = len(bytes);\n    while p + 32 <= len(bytes) {\n        // The kind is the cheap test", S),
    ("a torn tail is damage", "aof.cho", "    return false;\n}\n\n// The batch that starts at `at`", "    return true;\n}\n\n// The batch that starts at `at`", S),
    ("only half of a batch's checksum is checked", "aof.cho", "    return get_u32(bytes, p + 24) == sum.1 && get_u32(bytes, p + 28) == sum.0;", "    return get_u32(bytes, p + 24) == sum.1;", S),
    ("a kind that is none is a torn tail like another fault", "aof.cho", "            if end == 0 - 3 {\n                return (25, at, 0, applied, skipped);\n            }", "", S),
    ("a kind that is none is taken", "aof.cho", "kind != kind_flush() {\n            return 0 - 3;", "kind != kind_flush() && kind != 7 {\n            return 0 - 3;", S),
    ("a length may be negative", "aof.cho", "        if klen < 0 || vlen < 0 || klen > size || vlen > size {", "        if klen > size || vlen > size {", S),
    ("the magic is not checked", "aof.cho", "    var i = 0;\n    while i < 8 {\n        if int_of(bytes[i]) != int_of(\"CANCHOLG\"[i]) {\n            return (21, 0, 0, 0, 0);\n        }\n        i = i + 1;\n    }\n    if get_u64(bytes, 8) != version() {", "    if get_u64(bytes, 8) != version() {", S),
    ("the version is not checked", "aof.cho", "    if get_u64(bytes, 8) != version() {\n        return (22, 0, 0, 0, 0);\n    }", "", S),
    ("a header cut short is refused", "aof.cho", "        return (0, 0, size, 0, 0);", "        return (21, 0, 0, 0, 0);", S),
    ("a wrong short header is taken", "aof.cho", "            if i < 8 && int_of(bytes[i]) != int_of(\"CANCHOLG\"[i]) {\n                return (21, 0, 0, 0, 0);\n            }", "", S),
    ("an empty log is not made again", "aof.cho", "(answer.2 > 0 || answer.1 == 0)", "(answer.2 > 0)", S),
    ("a torn tail is not cut", "aof.cho", "                if answer.0 == 0 && (answer.2 > 0 || answer.1 == 0) {", "                if answer.0 == 0 && answer.2 > 12345 {", S),
    ("the cut leaves a byte", "aof.cho", "match file_truncate(w, good) {", "match file_truncate(w, good + 1) {", S),
    ("a log with no header is not removed to be made again", "aof.cho", "    if good == 0 {\n        match dir_remove", "    if good == 12345 {\n        match dir_remove", S),
    ("the batches replayed are not counted", "cache.cho", "                            ab[11] = applied;\n", "", S),
    ("the torn bytes are not counted", "cache.cho", "                            ab[12] = torn;\n", "", S),
    ("the batches skipped are not counted", "cache.cho", "                            ab[13] = skipped;\n", "", S),
    ("the size of the log is not known", "cache.cho", "                            if good > 32 {\n                                ab[5] = good - 32;\n                            }", "", S),
    ("the size is off by the header", "cache.cho", "                                ab[5] = good - 32;", "                                ab[5] = good;", S),
    ("a log too big to read is read", "aof.cho", "    if status == 0 && size > most_bytes() {\n                status = 27;\n            }", "", S),
    ("a log that does not fit is taken", "aof.cho", "            if store.set(st, key, h, value, relative_of(st, ends)) != 0 {\n                return 26;\n            }", "            store.set(st, key, h, value, relative_of(st, ends));", S),
]

run_mutants(MUTANTS, sys.argv[1] if len(sys.argv) > 1 else "")
