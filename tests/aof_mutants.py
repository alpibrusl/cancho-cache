#!/usr/bin/env python3
"""Mutation test of the append-only log's writing (`docs/design.md` section 38): each mutant is a deliberately wrong cache, and `tests/aof.py` must fail on it.

    CANCHO=path/to/cancho python3 tests/aof_mutants.py [substring of a mutant's name]

The machinery is `tests/mutlib.py`. A mutant the harness does not fail is a hole in the harness, and this script exits 1.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mutlib import run_mutants

S = ("py", "aof.py")
GATE = "    if !log_on(core) || idx < 0 || !cmdnames.is_write(idx) || to <= from || stats.did_not_run(sc, from, to) || int_of(sc[from]) == '-' {"
BARRIER = "    let always = store.fsync_of(core.store) == aof.policy_always();\n    let status = aof.flush("
EVERYSEC = "ab[0] != 0 && store.fsync_of(cw.store) == aof.policy_everysec() && ab[7] > 0 && store.now_of(cw.store) - ab[3] >= 1000 {"
LOOP = "    var i = first;\n    while i <= end && i < tb[0] {"
# (name, file, old text, new text, how to test)
MUTANTS = [
    ("an error is logged", "cache.cho", GATE, GATE.replace(" || int_of(sc[from]) == '-'", ""), S),
    ("a read is logged", "cache.cho", GATE, GATE.replace(" || !cmdnames.is_write(idx)", ""), S),
    ("a command that did not run is logged", "cache.cho", GATE, GATE.replace(" || stats.did_not_run(sc, from, to)", ""), S),
    ("nothing is logged", "cache.cho", GATE, GATE.replace("!log_on(core)", "true"), S),
    ("the keys are stepped one at a time", "cache.cho", "        i = i + step;\n", "        i = i + 1;\n", S),
    ("a command with its last key counted from the end names none", "cache.cho", "    var end = last;\n    if end < 0 {\n        end = tb[0] + end;\n    }", "    var end = last;", S),
    ("a key that is gone is logged as it was", "cache.cho", "        if e < 0 {\n            aof.log_bare(fs, path, core.store, ab, contents(core.aofbuf), aof.kind_gone(), key, 0);\n        } else if changes_only_expiry(name) {", "        if false {\n        } else if changes_only_expiry(name) {", S),
    ("a new deadline copies the value", "cache.cho", "fn changes_only_expiry[&n](name: &n [byte]) -> [] bool {\n    return ", "fn changes_only_expiry[&n](name: &n [byte]) -> [] bool {\n    return false && ", S),
    ("PERSIST copies the value", "cache.cho", " || reply.is_word(name, \"PERSIST\");", ";", S),
    ("a deadline is logged as the store's time, not the clock's", "cache.cho", "                ends = store.wall_of(core.store) + store.expiry_of(core.store, e);\n            }\n            aof.log_bare(", "                ends = store.expiry_of(core.store, e);\n            }\n            aof.log_bare(", S),
    ("a flush is not logged", "cache.cho", "        aof.log_bare(fs, path, core.store, ab, contents(core.aofbuf), aof.kind_flush(), name[0..0], 0);\n        return 0;", "        return 0;", S),
    ("an evicted key is not logged as gone", "store.cho", "    if st.evlog_on != 0 {\n        evlog_push(st, e);\n    }\n", "", S),
    ("evictions are not kept", "cache.cho", "                            store.evlog_start(cw.store);\n", "", S),
    ("the evicted keys are logged and kept for ever", "cache.cho", "    store.evlog_clear(core.store);\n", "", S),
    ("a value that arrived in pieces is not logged", "cache.cho", "    aof.log_entry(fs, contents(core.dir)[0..core.dir_len], core.store, ab, contents(core.aofbuf), e);\n    return 0;\n}\n\n// What the step", "    return 0;\n}\n\n// What the step", S),
    ("a value that arrived in pieces is not a change", "cache.cho", "    store.add_dirty(core.store, 1);\n    if !log_on(core) {\n        return 0;\n    }\n    log_evictions(core, fs);\n    let ab = contents(core.info)[stats.aof_at()..stats.aof_at() + stats.aof_ints()];\n    aof.log_entry(", "    if !log_on(core) {\n        return 0;\n    }\n    log_evictions(core, fs);\n    let ab = contents(core.info)[stats.aof_at()..stats.aof_at() + stats.aof_ints()];\n    aof.log_entry(", S),
    ("HSET of a field that is there is not a change", "cache.cho", " || idx == cmdnames.index_of(\"HSET\") {", " {", S),
    ("the commands inside EXEC are not logged", "cache.cho", "                                    log_write(core, fs, qview, tb, cmdnames.index_of(reply.arg_of(qview, tb, 0)), sc, exec_from, at);\n", "", S),
    ("a command is not logged", "cache.cho", "                        log_write(core, fs, view, tb, cmd_idx, sc, reply_from, at);\n", "", S),
    ("the staged records are never written", "cache.cho", "    if ab[2] == 0 {\n        return 0;\n    }\n    let always = ", "    if ab[2] == 0 || true {\n        return 0;\n    }\n    let always = ", S),
    ("always does not sync before the answer", "cache.cho", BARRIER, BARRIER.replace("== aof.policy_always();", "== 77;"), S),
    ("every policy syncs before the answer", "cache.cho", BARRIER, BARRIER.replace("store.fsync_of(core.store) == aof.policy_always()", "true"), S),
    ("a log that cannot be written under always still answers", "cache.cho", "        contents(core.info)[stats.persist_at() + 13] = 2;\n        return 1;", "        contents(core.info)[stats.persist_at() + 13] = 2;\n        return 0;", S),
    ("a log that cannot be written under always does not stop the cache", "cache.cho", "        contents(core.info)[stats.persist_at() + 13] = 2;\n        return 1;", "        return 1;", S),
    ("everysec never syncs", "cache.cho", EVERYSEC, EVERYSEC.replace(">= 1000", ">= 1000000000"), S),
    ("everysec syncs every turn", "cache.cho", EVERYSEC, EVERYSEC.replace("now_of(cw.store) - ab[3] >= 1000", "now_of(cw.store) - ab[3] >= 0"), S),
    ("everysec syncs when nothing was written", "cache.cho", EVERYSEC, EVERYSEC.replace(" && ab[7] > 0", ""), S),
    ("a sync does not reset the unsynced count", "cache.cho", "                        if synced == 0 {\n                            ab[7] = 0;\n                            ab[8] = ab[8] + 1;", "                        if synced == 0 {\n                            ab[8] = ab[8] + 1;", S),
    ("SHUTDOWN does not sync the log", "cache.cho", "            aof.sync(fs, contents(core.dir)[0..core.dir_len]);\n", "", S),
    ("the log is not started", "cache.cho", "                            opened = aof.open_log(fs, dirpath, store.wall_of(cw.store) + store.now_of(cw.store));", "                            opened = 0;", S),
    ("--appendonly without a directory starts", "cache.cho", "                    if len(dirpath) == 0 {\n                        bad_map = true;\n                    } else {\n                        // The log is read", "                    if false {\n                        bad_map = true;\n                    } else {\n                        // The log is read", S),
    ("a wrong --appendonly is taken", "cache.cho", "                } else if !(len(word) == 2 && int_of(word[0]) == 'n' && int_of(word[1]) == 'o') {\n                    aof_bad = true;\n                }", "                }", S),
    ("a wrong --appendfsync is taken", "cache.cho", "                aof_policy = store.fsync_named(arg(g, k)[14..len(arg(g, k))]);\n                if aof_policy < 0 {\n                    aof_bad = true;\n                }", "                aof_policy = store.fsync_named(arg(g, k)[14..len(arg(g, k))]);\n                if aof_policy < 0 {\n                    aof_policy = 1;\n                }", S),
    ("--appendfsync is not set at start", "cache.cho", "                store.set_fsync(cw.store, aof_policy);\n", "", S),
    ("a log that is there is made again", "aof.cho", "                        if e != 17 {\n                            status = e;", "                        if true {\n                            status = e;", S),
    ("the header is not synced", "aof.cho", "                                status = write_all(w, head);\n                                if status == 0 {\n                                    match file_sync(w) {", "                                status = write_all(w, head);\n                                if status == 0 && false {\n                                    match file_sync(w) {", S),
    ("the magic is wrong", "aof.cho", "                                head[i] = byte_of(int_of(\"CANCHOLG\"[i]));", "                                head[i] = byte_of(int_of(\"CANCHODB\"[i]));", S),
    ("the length of a batch is wrong", "aof.cho", "        put_u64(trailer, 0, used);\n", "        put_u64(trailer, 0, used + 1);\n", S),
    ("the checksum of a batch is half", "aof.cho", "        put_u32(trailer, 28, sum.0);\n        let none", "        put_u32(trailer, 28, 0);\n        let none", S),
    ("the checksum leaves out the last byte", "aof.cho", "crc.crc64_update(crc.crc64_start(), buf[0..used])", "crc.crc64_update(crc.crc64_start(), buf[0..used - 1])", S),
    ("a batch has no time", "aof.cho", "        put_u64(trailer, 8, store.wall_of(st) + store.now_of(st));\n        put_u64(trailer, 16, kind_end());\n        put_u32(trailer, 24, sum.1);\n        put_u32(trailer, 28, sum.0);\n        let none", "        put_u64(trailer, 8, 0);\n        put_u64(trailer, 16, kind_end());\n        put_u32(trailer, 24, sum.1);\n        put_u32(trailer, 28, sum.0);\n        let none", S),
    ("a batch does not end with its kind", "aof.cho", "        put_u64(trailer, 16, kind_end());\n        put_u32(trailer, 24, sum.1);\n        put_u32(trailer, 28, sum.0);\n        let none", "        put_u64(trailer, 16, 8);\n        put_u32(trailer, 24, sum.1);\n        put_u32(trailer, 28, sum.0);\n        let none", S),
    ("a record is written without its value", "aof.cho", "    at = copy_in(buf, at + 32, key);\n    at = copy_in(buf, at, value);\n    ab[2] = at;", "    at = copy_in(buf, at + 32, key);\n    ab[2] = at;", S),
    ("a record is written with the wrong kind", "aof.cho", "    put_head(buf, at, len(key), len(value), store.kind_of(st, e), ends);\n    at = copy_in(buf, at + 32, key);\n    at = copy_in(buf, at, value);", "    put_head(buf, at, len(key), len(value), 0, ends);\n    at = copy_in(buf, at + 32, key);\n    at = copy_in(buf, at, value);", S),
    ("a record has no deadline", "aof.cho", "    var ends = 0;\n    if store.expiry_of(st, e) != 0 {\n        ends = store.wall_of(st) + store.expiry_of(st, e);\n    }\n    let need = 32", "    var ends = 0;\n    let need = 32", S),
    ("a deadline is the store's time", "aof.cho", "        ends = store.wall_of(st) + store.expiry_of(st, e);\n    }\n    let need = 32", "        ends = store.expiry_of(st, e);\n    }\n    let need = 32", S),
    ("a record bigger than the buffer is copied into it", "aof.cho", "    if need > len(buf) {\n        var status = flush(", "    if need > len(buf) * 1000 {\n        var status = flush(", S),
    ("what is staged is not written before a record that does not fit", "aof.cho", "    if ab[2] + n > len(buf) {\n        return flush(fs, path, st, ab, buf, false);\n    }", "    if false {\n        return flush(fs, path, st, ab, buf, false);\n    }", S),
    ("what is staged is not written before a big record", "aof.cho", "        var status = flush(fs, path, st, ab, buf, false);\n        if status != 0 {\n            return status;\n        }\n        region r {\n            let head", "        var status = 0;\n        region r {\n            let head", S),
    ("a big record's checksum leaves out its value", "aof.cho", "            state = crc.crc64_update(state, value);\n", "", S),
    ("a failed write is counted as a batch", "aof.cho", "    if status != 0 {\n        ab[9] = ab[9] + 1;\n        return status;\n    }\n    ab[4]", "    if status != 0 {\n        ab[9] = ab[9] + 1;\n    }\n    ab[4]", S),
    ("a failed write is not counted", "aof.cho", "    if status != 0 {\n        ab[9] = ab[9] + 1;\n        return status;\n    }", "    if status != 0 {\n        return status;\n    }", S),
    ("the size is not counted", "aof.cho", "    ab[5] = ab[5] + written;\n", "", S),
    ("CONFIG GET appendonly is always no", "session.cho", "        if world[stats.aof_at()] != 0 {\n            return reply.put_bulk(sc, at, \"yes\");\n        }", "        if world[stats.aof_at()] == 12345 {\n            return reply.put_bulk(sc, at, \"yes\");\n        }", S),
    ("CONFIG GET appendfsync is a constant", "session.cho", "store.fsync_name(store.fsync_of(st))", "store.fsync_name(1)", S),
    ("CONFIG SET appendfsync is not kept", "session.cho", "            store.set_fsync(st, store.fsync_named(value));", "            store.set_fsync(st, 1);", S),
    ("CONFIG SET appendfsync takes any word", "session.cho", "            if store.fsync_named(value) < 0 {\n                return set_failed(sc, at, setting_name(idx), \"argument(s) must be one of the following: always, everysec, no\");\n            }", "", S),
    ("CONFIG SET appendonly is taken", "session.cho", "        if idx == 3 {\n            return set_failed(sc, at, name, \"appendonly is chosen at start", "        if idx == 12345 {\n            return set_failed(sc, at, name, \"appendonly is chosen at start", S),
    ("always is not a policy", "store.cho", "    if len(word) == 6 && (int_of(word[0]) == 'a' || int_of(word[0]) == 'A') {\n        return 2;\n    }", "", S),
    ("INFO says the log is off", "session.cho", "        o = kv(sc, o, \"aof_enabled\", world[stats.aof_at()]);", "        o = kv(sc, o, \"aof_enabled\", 0);", S),
    ("INFO says a failed write was fine", "session.cho", "        if world[stats.aof_at() + 6] == 0 {\n            o = reply.put(sc, o, \"aof_last_write_status:ok\\r\\n\");", "        if true {\n            o = reply.put(sc, o, \"aof_last_write_status:ok\\r\\n\");", S),
    ("INFO's size is the batches", "session.cho", "world[stats.aof_at() + 5] + 32", "world[stats.aof_at() + 4]", S),
]

run_mutants(MUTANTS, sys.argv[1] if len(sys.argv) > 1 else "")
