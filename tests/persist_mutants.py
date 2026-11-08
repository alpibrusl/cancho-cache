#!/usr/bin/env python3
"""Mutation test of the snapshot (`docs/design.md` section 35): each mutant is a deliberately wrong cache, and `tests/persist.py` must fail on it.

    CANCHO=path/to/cancho python3 tests/persist_mutants.py [substring of a mutant's name]

The machinery is `tests/mutlib.py`. A mutant the harness does not fail is a hole in the harness, and this script exits 1.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mutlib import run_mutants

S = ("py", "persist.py")
# (name, file, old text, new text, how to test)
MUTANTS = [
    ("the snapshot is written to the file it replaces, not a temporary one", "persist.cho", "dir_rename(dir, \"dump.tmp\", \"dump.cho\")", "dir_rename(dir, \"dump.tmp\", \"dump.tmp\")", S),
    ("a left-over temporary file is not removed first", "persist.cho", "    match dir_remove(dir, \"dump.tmp\") {", "    match dir_remove(dir, \"dump.nothing\") {", S),
    ("the file is not synced", "persist.cho", "                    match file_sync(w) {\n                        Done::Ok(n) => {\n                        }\n                        Done::Failed(e) => {\n                            status = e;\n                        }\n                    }\n                }\n            }\n            file_close(file);", "                }\n            }\n            file_close(file);", S),
    ("the version is not checked", "persist.cho", "    if get_u64(bytes, 8) != version() {", "    if get_u64(bytes, 8) != version() && false {", S),
    ("the magic is not checked", "persist.cho", "        if int_of(bytes[i]) != int_of(\"CANCHODB\"[i]) {", "        if false {", S),
    ("the checksum is not checked", "persist.cho", "    if get_u32(bytes, size - 8) != sum.1 || get_u32(bytes, size - 4) != sum.0 {", "    if false {", S),
    ("only the low half of the checksum is checked", "persist.cho", "    if get_u32(bytes, size - 8) != sum.1 || get_u32(bytes, size - 4) != sum.0 {", "    if get_u32(bytes, size - 8) != sum.1 {", S),
    ("the checksum leaves out the last record", "persist.cho", "    let sum = crc.crc64_of(bytes[0..size - trailer_size()]);", "    let sum = crc.crc64_of(bytes[0..size - trailer_size() - 1]);", S),
    ("a record longer than the file is read", "persist.cho", "        if klen < 0 || vlen < 0 || klen > end || vlen > end || at + record_size() + klen + vlen > end {", "        if false {", S),
    ("a negative length is not refused", "persist.cho", "        if klen < 0 || vlen < 0 || klen > end || vlen > end || at + record_size() + klen + vlen > end {", "        if klen > end || vlen > end || at + record_size() + klen + vlen > end {", S),
    ("an unknown kind is stored", "persist.cho", "        if kind != store.kind_string() && kind != store.kind_hash() {", "        if false {", S),
    ("bytes after the last record are accepted", "persist.cho", "    if at != end {\n        return (15, kept);\n    }", "", S),
    ("a count that is too small is accepted", "persist.cho", "    let count = get_u64(bytes, 16);", "    let count = get_u64(bytes, 16) + 1000000;", S),
    ("a key that ended is loaded", "persist.cho", "            if relative <= store.now_of(st) {\n                live = false;\n            }", "", S),
    ("an expiry is loaded as if it were relative", "persist.cho", "            relative = ends - store.wall_of(st);", "            relative = ends;", S),
    ("the expiry is saved as the store's time, not the clock's", "persist.cho", "                    expiry = store.wall_of(st) + store.expiry_of(st, e);", "                    expiry = store.expiry_of(st, e);", S),
    ("a hash is loaded as a string", "persist.cho", "                store.set_kind(st, e, kind);", "", S),
    ("the kind is not saved", "persist.cho", "                put_u64(head, 16, store.kind_of(st, e));", "                put_u64(head, 16, 0);", S),
    ("a key that has run out is saved", "persist.cho", "            if store.visible(st, e) {\n                let key = store.key_at(st, e);", "            if true {\n                let key = store.key_at(st, e);", S),
    ("the value is cut one byte short", "persist.cho", "                put_u64(head, 8, len(value));", "                put_u64(head, 8, len(value) - 1);", S),
    ("a store that cannot take a value is not refused", "persist.cho", "            if store.set(st, key, h, value, relative) != 0 {\n                return (16, kept);\n            }", "            store.set(st, key, h, value, relative);", S),
    ("a missing file is a refusal", "persist.cho", "            if e == 2 {\n                return (1, 0);\n            }", "", S),
    ("a refusal does not stop the start", "cache.cho", "                    if loaded > persist.absent() {\n                        bad_dump = loaded;\n                    }", "", S),
    ("SAVE without a directory says OK", "cache.cho", "        if core.dir_len == 0 {\n            return reply.put(sc, at, \"-ERR persistence is off", "        if core.dir_len == 12345 {\n            return reply.put(sc, at, \"-ERR persistence is off", S),
    ("SAVE reports success when it failed", "cache.cho", "        if failed != 0 {\n            let o = reply.put_nat", "        if failed == 12345 {\n            let o = reply.put_nat", S),
    ("SAVE takes arguments", "cache.cho", "        if tb[0] != 1 {\n            return reply.put(sc, at, \"-ERR wrong number of arguments for 'save' command", "        if tb[0] != 12345 {\n            return reply.put(sc, at, \"-ERR wrong number of arguments for 'save' command", S),
]

run_mutants(MUTANTS, sys.argv[1] if len(sys.argv) > 1 else "")
