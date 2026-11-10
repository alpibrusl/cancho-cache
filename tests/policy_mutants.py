#!/usr/bin/env python3
"""Mutation test of the snapshot policy and SHUTDOWN (`docs/design.md` section 37): each mutant is a deliberately wrong cache, and `tests/policy.py` must fail on it.

    CANCHO=path/to/cancho python3 tests/policy_mutants.py [substring of a mutant's name]

The machinery is `tests/mutlib.py`. A mutant the harness does not fail is a hole in the harness, and this script exits 1.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mutlib import run_mutants

S = ("py", "policy.py")
NOTE = "    if idx >= 0 && cmdnames.is_write(idx) && to > from && !stats.did_not_run(sc, from, to) && int_of(sc[from]) != '-' {\n        if !(to - from == 4 && int_of(sc[from]) == ':' && int_of(sc[from + 1]) == '0') {"
DUE = "if seconds > 0 && store.dirty_of(cw.store) >= store.save_at(cw.store, 2 * pair + 1) && now_s - job[7] >= seconds && (job[8] == 0 || now_s - job[14] >= 5) {"
SETTLE = "                        if job[0] == 0 && job[8] == 0 {\n                            var left = store.dirty_of(cw.store) - job[12];"
SHUT = "        let policy = store.save_at(core.store, 0) > 0;\n        if (save || policy && !nosave) && core.dir_len > 0 {"
# (name, file, old text, new text, how to test)
MUTANTS = [
    ("a read counts as a change", "cache.cho", NOTE, NOTE.replace("cmdnames.is_write(idx)", "true"), S),
    ("an error counts as a change", "cache.cho", NOTE, NOTE.replace(" && int_of(sc[from]) != '-'", ""), S),
    ("a DEL of nothing counts as a change", "cache.cho", NOTE, NOTE.replace("if !(to - from == 4 && int_of(sc[from]) == ':' && int_of(sc[from + 1]) == '0') {", "if true {"), S),
    ("EXEC counts nothing", "cache.cho", "                                    note_change(core, cmdnames.index_of(reply.arg_of(qview, tb, 0)), sc, exec_from, at);\n", "", S),
    ("a command is counted twice", "cache.cho", "            store.add_dirty(core.store, 1);", "            store.add_dirty(core.store, 2);", S),
    ("a policy pair needs one more change than it says", "cache.cho", DUE, DUE.replace(">= store.save_at(cw.store, 2 * pair + 1)", "> store.save_at(cw.store, 2 * pair + 1)"), S),
    ("a policy pair ignores its seconds", "cache.cho", DUE, DUE.replace("now_s - job[7] >= seconds", "true"), S),
    ("a pair with 0 seconds is a pair", "cache.cho", DUE, DUE.replace("seconds > 0 &&", ""), S),
    ("the policy never starts a save", "cache.cho", "                        if due {\n", "                        if due && false {\n", S),
    ("a failed policy save is not recorded", "cache.cho", "                            if failed != 0 {\n                                job[8] = failed;\n                            } else {\n                                job[12] = store.dirty_of(cw.store);\n                            }", "                            if failed == 0 {\n                                job[12] = store.dirty_of(cw.store);\n                            }", S),
    ("a save that began a policy save does not remember the writes it had", "cache.cho", "                            } else {\n                                job[12] = store.dirty_of(cw.store);\n                            }", "                            }", S),
    ("a finished save settles nothing", "cache.cho", SETTLE, SETTLE.replace("job[0] == 0 && job[8] == 0", "false"), S),
    ("a finished save settles every write, even those after it began", "cache.cho", "                            store.set_dirty(cw.store, left);", "                            store.set_dirty(cw.store, 0);", S),
    ("a failed save settles the writes", "cache.cho", SETTLE, SETTLE.replace("job[0] == 0 && job[8] == 0", "job[0] == 0"), S),
    ("SAVE leaves the writes counted", "cache.cho", "        store.set_dirty(core.store, 0);\n        return reply.put(sc, at, \"+OK\\r\\n\");", "        return reply.put(sc, at, \"+OK\\r\\n\");", S),
    ("--save= is not read", "cache.cho", "                    if persist.read_save(savetext, pairs) < 0 {", "                    if persist.read_save(savetext[0..0], pairs) < 0 {", S),
    ("a wrong --save= starts the cache", "cache.cho", "                    if persist.read_save(savetext, pairs) < 0 {\n                        bad_map = true;\n                    }", "                    persist.read_save(savetext, pairs);", S),
    ("SHUTDOWN does not stop the cache", "cache.cho", "        job[13] = 1;\n        st[p + 7] = 1;", "        st[p + 7] = 1;", S),
    ("SHUTDOWN never saves", "cache.cho", SHUT, SHUT.replace("(save || policy && !nosave)", "false"), S),
    ("SHUTDOWN ignores the policy", "cache.cho", SHUT, SHUT.replace("(save || policy && !nosave)", "(save)"), S),
    ("SHUTDOWN NOSAVE saves", "cache.cho", SHUT, SHUT.replace("(save || policy && !nosave)", "(save || policy)"), S),
    ("SHUTDOWN SAVE NOSAVE is accepted", "cache.cho", "        if save && nosave {\n            return reply.put(sc, at, \"-ERR syntax error\\r\\n\");\n        }\n", "", S),
    ("SHUTDOWN stops when it could not save", "cache.cho", "            if persist.save(fs, contents(core.dir)[0..core.dir_len], core.store, now_ms) != 0 {\n                return reply.put(sc, at, \"-ERR Errors trying to SHUTDOWN. Check logs.\\r\\n\");", "            if persist.save(fs, contents(core.dir)[0..core.dir_len], core.store, now_ms) == 12345 {\n                return reply.put(sc, at, \"-ERR Errors trying to SHUTDOWN. Check logs.\\r\\n\");", S),
    ("SHUTDOWN SAVE without a directory stops the cache", "cache.cho", "        } else if save {\n            return reply.put(sc, at, \"-ERR Errors trying to SHUTDOWN. Check logs.\\r\\n\");\n        }", "        }", S),
    ("SHUTDOWN ABORT stops the cache", "cache.cho", "            } else if reply.is_word(word, \"ABORT\") {\n                return reply.put(sc, at, \"-ERR No shutdown in progress.\\r\\n\");", "            } else if reply.is_word(word, \"ABORT\") {\n                i = i + 0;", S),
    ("SHUTDOWN takes any word", "cache.cho", "            } else if !reply.is_word(word, \"NOW\") && !reply.is_word(word, \"FORCE\") {\n                return reply.put(sc, at, \"-ERR syntax error\\r\\n\");\n            }", "            }", S),
    ("the policy is not shown by CONFIG GET", "session.cho", "        var length = 0;\n        var pair = 0;\n        while pair < store.save_pairs() && store.save_at(st, 2 * pair) > 0 {", "        var length = 0;\n        var pair = 0;\n        while pair < 0 && store.save_at(st, 2 * pair) > 0 {", S),
    ("CONFIG SET save does not keep the pairs", "session.cho", "                    points[k] = pairs[k];", "                    points[k] = 0;", S),
    ("a refused policy is applied", "session.cho", "                if found == 0 - 1 {\n                    return set_failed(sc, at, setting_name(idx), \"Invalid save parameters\");\n                }", "", S),
    ("a policy of too many pairs is accepted", "session.cho", "                if found == 0 - 2 {\n                    return set_failed(sc, at, setting_name(idx), \"this cache keeps 8 save points (docs/design.md section 37)\");\n                }", "", S),
    ("an odd number of numbers is a policy", "persist.cho", "    if numbers % 2 == 1 {\n        return 0 - 1;\n    }", "", S),
    ("0 seconds is a policy", "persist.cho", "        if out[2 * pair] < 1 {\n            return 0 - 1;\n        }", "", S),
    ("a word is a number", "persist.cho", "            if digits == 0 || at < len(text)", "            if at < len(text)", S),
    ("a number glued to a letter is a number", "persist.cho", "            if digits == 0 || at < len(text) && int_of(text[at]) != ' ' && int_of(text[at]) != ':' && int_of(text[at]) != ',' && int_of(text[at]) != 9 && int_of(text[at]) != 10 && int_of(text[at]) != 13 {", "            if digits == 0 {", S),
    ("a refused policy leaves the pairs it read", "persist.cho", "    var i = 0;\n    while i < len(out) {\n        out[i] = 0;\n        i = i + 1;\n    }\n    var numbers = 0;", "    var i = 0;\n    var numbers = 0;", S),
]

run_mutants(MUTANTS, sys.argv[1] if len(sys.argv) > 1 else "")
