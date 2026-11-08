#!/usr/bin/env python3
"""Mutation test of LATENCY and MEMORY (`docs/design.md` section 33): each mutant is a deliberately wrong cache, and `tests/latency.py` must fail on it.

    CANCHO=path/to/cancho python3 tests/latency_mutants.py [substring of a mutant's name]

The machinery is `tests/mutlib.py`. A mutant the harness does not fail is a hole in the harness, and this script exits 1.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mutlib import run_mutants

S = ("py", "latency.py")
# (name, file, old text, new text, how to test)
MUTANTS = [
    ("the monitor is on whatever the threshold", "cache.cho", "    let lat = store.lat_threshold_of(core.store);\n    if (limit", "    let lat = 1;\n    if (limit", S),
        ("an event is noted whatever the threshold", "cache.cho", "|| lat > 0 && ms > lat) && !stats", "|| lat > 0 && ms >= 0) && !stats", S),
    ("an event is noted whatever the threshold (the second test)", "cache.cho", "        if lat > 0 && ms > lat {\n            var event = 1;", "        if lat > 0 && ms >= 0 {\n            var event = 1;", S),
    ("a slow command is a fast-command event", "cache.cho", "                event = 0;", "                event = 1;", S),
        ("LATEST leaves out the maximum", "latency.cho", "                o = reply.put_integer(sc, o, ints[b + 1]);\n", "", S),
        ("LATEST lists events with no sample", "latency.cho", "            if seen(st, e) {\n                let b = base(e);", "            if true {\n                let b = base(e);", S),
    ("HISTORY of an unknown event is an error", "latency.cho", "        if e < 0 {\n            return reply.put(sc, at, \"*0\\r\\n\");", "        if e < 0 {\n            return reply.put(sc, at, \"-ERR unknown event\\r\\n\");", S),
    ("RESET does not forget", "latency.cho", "                reset_event(st, e);\n                e = e + 1;", "                e = e + 1;", S),
    ("RESET counts events that had nothing", "latency.cho", "                if seen(st, e) {\n                    count = count + 1;\n                }\n                reset_event(st, e);", "                count = count + 1;\n                reset_event(st, e);", S),
    ("DOCTOR with the monitor off says there was no spike", "latency.cho", "        if store.lat_threshold_of(st) == 0 {", "        if store.lat_threshold_of(st) == 12345 {", S),
    ("a negative threshold is accepted", "session.cho", "        } else if idx == 13 {\n            let (status, n) = reply.parse_int(value);\n            if status != 0 {\n                return set_failed(sc, at, setting_name(idx), \"argument couldn't be parsed into an integer\");\n            }\n            if n < 0 {", "        } else if idx == 13 {\n            let (status, n) = reply.parse_int(value);\n            if status != 0 {\n                return set_failed(sc, at, setting_name(idx), \"argument couldn't be parsed into an integer\");\n            }\n            if n < -5 {", S),
    ("MEMORY USAGE of a missing key is 0", "memory.cho", "        return reply.put_null(sc, at, proto);", "        return reply.put_integer(sc, at, 0);", S),
    ("MEMORY USAGE leaves out the value", "memory.cho", "len(key) + len(store.value(st, e)) + store.record_overhead()", "len(key) + store.record_overhead()", S),
    ("MEMORY USAGE takes SAMPLES without a number", "memory.cho", "        let (status, n) = reply.parse_int(reply.arg_of(view, table, i + 1));\n        if status != 0 {\n            return reply.not_an_integer(sc, at);\n        }", "        let (status, n) = reply.parse_int(reply.arg_of(view, table, i + 1));", S),
    ("MEMORY DOCTOR always says it is empty", "memory.cho", "        if store.used(st) - store.dead(st) < small() {", "        if true {", S),
    ("MEMORY STATS counts the wrong keys", "memory.cho", "field(sc, o, \"keys.count\", store.live(st))", "field(sc, o, \"keys.count\", store.live(st) + 1)", S),
    ("MEMORY STATS names a field Redis does not have", "memory.cho", "\"dataset.bytes\"", "\"dataset.size\"", S),
]

run_mutants(MUTANTS, sys.argv[1] if len(sys.argv) > 1 else "")
