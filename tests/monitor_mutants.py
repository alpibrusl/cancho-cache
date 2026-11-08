#!/usr/bin/env python3
"""Mutation test of MONITOR (`docs/design.md` section 34): each mutant is a deliberately wrong cache, and `tests/monitor.py` must fail on it.

    CANCHO=path/to/cancho python3 tests/monitor_mutants.py [substring of a mutant's name]

The machinery is `tests/mutlib.py`. A mutant the harness does not fail is a hole in the harness, and this script exits 1.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mutlib import run_mutants

S = ("py", "monitor.py")
# (name, file, old text, new text, how to test)
MUTANTS = [
    ("nothing is shown", "cache.cho", "    if stats.monitors(contents(core.info)) == 0 || tb[0] < 1 {", "    if true {", S),
    ("the time has no microseconds", "cache.cho", "    let us = ms % 1000 * 1000;", "    let us = 0;", S),
    ("the address is left out", "cache.cho", "        o = reply.put(sc, o, buf[0..m]);\n    }", "        o = reply.put(sc, o, buf[0..0]);\n    }", S),
    ("a quote is not escaped", "cache.cho", "        if ch == '\\\\' || ch == '\"' {", "        if ch == '\\\\' {", S),
    ("a newline is shown as it is", "cache.cho", "        } else if ch == 10 {\n            o = reply.put(sc, o, \"\\\\n\");", "        } else if ch == 10 && false {\n            o = reply.put(sc, o, \"\\\\n\");", S),
    ("a binary byte is shown as it is", "cache.cho", "        } else if ch >= 32 && ch < 127 {", "        } else if ch >= 0 {", S),
    ("hex digits are upper case", "cache.cho", "    let digits = \"0123456789abcdef\";", "    let digits = \"0123456789ABCDEF\";", S),
    ("AUTH is shown in the clear", "cache.cho", "        if slowlog.redacted(view, tb, i) {", "        if false {", S),
    ("admin commands are shown", "cache.cho", "    if cmdnames.skips_monitor(idx, sub) {", "    if false {", S),
        ("commands that did not run are shown", "cache.cho", "                        if st[p + 9] == 0 && status != 2 && !stats.did_not_run(sc, reply_from, at) {", "                        if st[p + 9] == 0 && status != 2 {", S),
    ("the commands of an EXEC are not shown", "cache.cho", "                                if st[p + 9] == 0 && !stats.did_not_run(sc, exec_from, at) {", "                                if false {", S),
    ("EXEC is not shown", "cache.cho", "                        if resp.parse(view, tb) > 0 {\n                            at = monitor_feed(tab, core, k, view, tb, sc, at);", "                        if false {\n                            at = monitor_feed(tab, core, k, view, tb, sc, at);", S),
    ("EXEC is shown before the commands it ran", "cache.cho", "                        if st[p + 9] == 0 && status != 2 && !stats.did_not_run(sc, reply_from, at) {", "                        if st[p + 9] == 0 && !stats.did_not_run(sc, reply_from, at) {", S),
    ("a watcher's own line comes before its answer", "cache.cho", "    if st[stride() * k + 22] == 1 {\n        return o;\n    }\n    return at;", "    return at;", S),
    ("only the first watcher is shown", "cache.cho", "        if st[pq + 1] == 1 && st[pq + 22] == 1 && q != k {", "        if st[pq + 1] == 1 && st[pq + 22] == 1 && q != k && q < 1 {", S),
    ("a watcher may touch the keyspace", "cache.cho", "        } else if tb[0] >= 1 && cmdnames.touches_keyspace(cmdnames.index_of(reply.arg_of(view, tb, 0)), sub) {", "        } else if false {", S),
    ("a second MONITOR is answered", "cache.cho", "            // Already watching: Redis ignores it and does not answer.\n            return at;", "            return reply.put(sc, at, \"+OK\\r\\n\");", S),
    ("RESET does not end the watching", "cache.cho", "            st[p + 22] = 0;\n            stats.add_monitors(contents(core.info), 0 - 1);\n        } else if", "        } else if", S),
    ]

run_mutants(MUTANTS, sys.argv[1] if len(sys.argv) > 1 else "")
