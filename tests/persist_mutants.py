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
    ("the directory is not synced", "persist.cho", "    if status == 0 {\n        match dir_sync(dir) {\n            Done::Ok(n) => {\n            }\n            Done::Failed(e) => {\n                status = e;\n            }\n        }\n    }\n    return status;", "    return status;", S),
        ("a slice has no budget: the whole arena in one turn", "persist.cho", "job[4] - start < budget && status == 0", "status == 0", S),
    ("BGSAVE answers started when it could not start", "cache.cho", "        let failed = persist.begin(fs, contents(core.dir)[0..core.dir_len], core.store, job, store.wall_of(core.store) + store.now_of(core.store));\n        if failed != 0 {", "        let failed = persist.begin(fs, contents(core.dir)[0..core.dir_len], core.store, job, store.wall_of(core.store) + store.now_of(core.store));\n        if failed == 12345 {", S),
    ("a second BGSAVE is accepted", "cache.cho", "        let job = contents(core.info)[stats.persist_at()..stats.persist_at() + stats.persist_ints()];\n        if job[0] != 0 {\n            return reply.put(sc, at, \"-ERR Background save already in progress\\r\\n\");\n        }\n        let failed = persist.begin(", "        let job = contents(core.info)[stats.persist_at()..stats.persist_at() + stats.persist_ints()];\n        let failed = persist.begin(", S),
    ("SAVE during a BGSAVE is accepted", "cache.cho", "        if job[0] != 0 {\n            return reply.put(sc, at, \"-ERR Background save already in progress\\r\\n\");\n        }\n        let now_ms = store.wall_of(core.store) + store.now_of(core.store);", "        let now_ms = store.wall_of(core.store) + store.now_of(core.store);", S),
    ("an old temporary file is not removed before a BGSAVE", "persist.cho", "                match dir_remove(d, \"dump.tmp\") {\n                    Done::Ok(n) => {\n                    }\n                    Done::Failed(e) => {\n                    }\n                }\n                match dir_open_new_mode(d, \"dump.tmp\", 420) {\n                    Opened::Ok(of) => {\n                        var file = of;\n                        region r {\n                            let head", "                match dir_open_new_mode(d, \"dump.tmp\", 420) {\n                    Opened::Ok(of) => {\n                        var file = of;\n                        region r {\n                            let head", S),
    ("a save that finishes does not move LASTSAVE", "persist.cho", "                job[7] = now_ms / 1000;\n                job[8] = 0;\n                job[9] = now_ms - job[6];", "                job[8] = 0;\n                job[9] = now_ms - job[6];", S),
    ("a failed save is not recorded as failed", "persist.cho", "            if status != 0 {\n                job[0] = 0;\n                job[8] = status;", "            if status != 0 {\n                job[0] = 0;", S),
    ("a save that has finished is still running", "persist.cho", "            if finished {\n                if job[0] == 1 {\n                    job[0] = 0;\n                }\n                job[7] = now_ms / 1000;", "            if finished {\n                job[7] = now_ms / 1000;", S),
    ("the header of a sliced save is left out of its checksum", "persist.cho", "                            let start = crc.crc64_update(crc.crc64_start(), head);", "                            let start = crc.crc64_start();", S),
    ("a sliced save is renamed before it is complete", "persist.cho", "                if finished {\n                    // The old snapshot is moved aside", "                if true {\n                    // The old snapshot is moved aside", S),
    ("a count that is wrong is accepted", "persist.cho", "    if count >= 0 && count != read {\n        return (15, kept);\n    }", "", S),
    ("the expiry is saved as the store's time, not the clock's", "persist.cho", "        expiry = store.wall_of(st) + store.expiry_of(st, e);", "        expiry = store.expiry_of(st, e);", S),
    ("the kind is not saved", "persist.cho", "    put_u64(head, 16, store.kind_of(st, e));", "    put_u64(head, 16, 0);", S),
    ("the value is cut one byte short", "persist.cho", "    put_u64(head, 8, len(store.value(st, e)));", "    put_u64(head, 8, len(store.value(st, e)) - 1);", S),
    ("SAVE writes a key that has run out", "persist.cho", "        while e < store.entries(st) && acc[0] == 0 {\n            if store.visible(st, e) {", "        while e < store.entries(st) && acc[0] == 0 {\n            if true {", S),
        ("SAVE without a directory says OK", "cache.cho", "'save' command\\r\\n\");\n        }\n        if core.dir_len == 0 {", "'save' command\\r\\n\");\n        }\n        if core.dir_len == 12345 {", S),
    ("BGSAVE without a directory starts one", "cache.cho", "        if tb[0] == 2 && !reply.is_word(reply.arg_of(view, tb, 1), \"SCHEDULE\") {\n            return reply.put(sc, at, \"-ERR syntax error\\r\\n\");\n        }\n        if core.dir_len == 0 {", "        if tb[0] == 2 && !reply.is_word(reply.arg_of(view, tb, 1), \"SCHEDULE\") {\n            return reply.put(sc, at, \"-ERR syntax error\\r\\n\");\n        }\n        if core.dir_len == 12345 {", S),
    ("SAVE reports success when it failed", "cache.cho", "        if failed != 0 {\n            job[8] = failed;\n            let o = reply.put_nat(sc, reply.put(sc, at, \"-ERR the snapshot could not be written", "        if failed == 12345 {\n            job[8] = failed;\n            let o = reply.put_nat(sc, reply.put(sc, at, \"-ERR the snapshot could not be written", S),
    ("a start does not look at dump.old", "persist.cho", "            if e == 2 {\n                name = \"dump.old\";\n            }", "", S),
    ("a start prefers dump.old", "persist.cho", "    var name = \"dump.cho\";", "    var name = \"dump.old\";", S),
    ("the old snapshot is replaced, not moved aside", "persist.cho", "                    match dir_rename(d, \"dump.cho\", \"dump.old\") {", "                    match dir_rename(d, \"dump.nothing\", \"dump.old\") {", S),
    ("the old snapshot is never given back", "persist.cho", "    if job[0] == 2 {\n        return retire(fs, path, job);\n    }", "    if job[0] == 2 {\n        job[0] = 0;\n        return 0;\n    }", S),
    ("the version is not checked", "persist.cho", "    if get_u64(bytes, 8) != version() {", "    if get_u64(bytes, 8) != version() && false {", S),
    ("the magic is not checked", "persist.cho", "        if int_of(bytes[i]) != int_of(\"CANCHODB\"[i]) {", "        if false {", S),
    ("the checksum is not checked", "persist.cho", "    if get_u32(bytes, size - 8) != sum.1 || get_u32(bytes, size - 4) != sum.0 {", "    if false {", S),
    ("only the low half of the checksum is checked", "persist.cho", "    if get_u32(bytes, size - 8) != sum.1 || get_u32(bytes, size - 4) != sum.0 {", "    if get_u32(bytes, size - 8) != sum.1 {", S),
    ("the checksum leaves out the last record", "persist.cho", "    let sum = crc.crc64_of(bytes[0..size - trailer_size()]);", "    let sum = crc.crc64_of(bytes[0..size - trailer_size() - 1]);", S),
    ("a record longer than the file is read", "persist.cho", "        if klen < 0 || vlen < 0 || klen > end || vlen > end || at + record_size() + klen + vlen > end {", "        if false {", S),
    ("a negative length is not refused", "persist.cho", "        if klen < 0 || vlen < 0 || klen > end || vlen > end || at + record_size() + klen + vlen > end {", "        if klen > end || vlen > end || at + record_size() + klen + vlen > end {", S),
    ("an unknown kind is stored", "persist.cho", "        if kind != store.kind_string() && kind != store.kind_hash() {", "        if false {", S),
        ("a count that is too small is accepted", "persist.cho", "    let count = get_u64(bytes, 16);", "    let count = get_u64(bytes, 16) + 1000000;", S),
    ("a key that ended is loaded", "persist.cho", "            if relative <= store.now_of(st) {\n                live = false;\n            }", "", S),
    ("an expiry is loaded as if it were relative", "persist.cho", "            relative = ends - store.wall_of(st);", "            relative = ends;", S),
        ("a hash is loaded as a string", "persist.cho", "                store.set_kind(st, e, kind);", "", S),
                ("a store that cannot take a value is not refused", "persist.cho", "            if store.set(st, key, h, value, relative) != 0 {\n                return (16, kept);\n            }", "            store.set(st, key, h, value, relative);", S),
    ("a missing file is a refusal", "persist.cho", "            if e == 2 {\n                return (1, 0);\n            }", "", S),
    ("a refusal does not stop the start", "cache.cho", "                    if loaded > persist.absent() {\n                        bad_dump = loaded;\n                    }", "", S),
            ("SAVE takes arguments", "cache.cho", "        if tb[0] != 1 {\n            return reply.put(sc, at, \"-ERR wrong number of arguments for 'save' command", "        if tb[0] != 12345 {\n            return reply.put(sc, at, \"-ERR wrong number of arguments for 'save' command", S),
]

run_mutants(MUTANTS, sys.argv[1] if len(sys.argv) > 1 else "")
