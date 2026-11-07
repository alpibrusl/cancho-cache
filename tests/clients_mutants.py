#!/usr/bin/env python3
"""Mutation test of CLIENT LIST, INFO and KILL (`docs/design.md` section 32): each mutant is a deliberately wrong cache, and `tests/client_commands.py` must fail on it.

    CANCHO=path/to/cancho python3 tests/clients_mutants.py [substring of a mutant's name]

The machinery is `tests/mutlib.py`. A mutant the harness does not fail is a hole in the harness, and this script exits 1.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mutlib import run_mutants

S = ("py", "client_commands.py")
# (name, file, old text, new text, how to test)
MUTANTS = [
    ("LIST leaves out a connection", "clients.cho", "            if st[strd * k + 1] == 1 && (only < 0 || only == k) {", "            if st[strd * k + 1] == 1 && (only < 0 || only == k) && k != 1 {", S),
    ("INFO lists every connection", "clients.cho", "(only < 0 || only == k) {", "(only < 0 || only >= 0) {", S),
    ("the name is not in the entry", "clients.cho", "names[slot * 64..slot * 64 + st[p + 5]]", "names[slot * 64..slot * 64]", S),
    ("a connection in MULTI is flagged N", "clients.cho", "    if multi {\n        o = reply.put(sc, o, \"x\");", "    if false {\n        o = reply.put(sc, o, \"x\");", S),
    ("multi is not the number queued", "clients.cho", "        queued = tx[t + 3];", "        queued = 0;", S),
    ("the protocol is always 2", "clients.cho", "\" resp=\", st[p + 8])", "\" resp=\", 2)", S),
    ("ID filter takes the first id only", "clients.cho", "                    var i = 3;\n                    while i < argc {\n                        let (bad, id) = reply.parse_int(reply.arg_of(view, table, i));\n                        if bad == 0 && id == st[strd * k + 6] {", "                    var i = 3;\n                    while i < 4 {\n                        let (bad, id) = reply.parse_int(reply.arg_of(view, table, i));\n                        if bad == 0 && id == st[strd * k + 6] {", S),
    ("a bad id is not refused", "clients.cho", "                if bad != 0 {\n                    return reply.put(sc, at, \"-ERR Invalid client ID", "                if bad != 0 && false {\n                    return reply.put(sc, at, \"-ERR Invalid client ID", S),
    ("TYPE pubsub lists the normal ones", "clients.cho", "            if t == 1 {\n                return reply.put(sc, at, \"$0\\r\\n\\r\\n\");", "            if t == 2 {\n                return reply.put(sc, at, \"$0\\r\\n\\r\\n\");", S),
    ("an unknown type is a type", "clients.cho", "    return 0 - 1;\n}\n\n// Put the bulk", "    return 0;\n}\n\n// Put the bulk", S),
    ("KILL kills the caller whatever SKIPME says", "clients.cho", "!(skipme && k == me)", "true", S),
    ("KILL ignores ID", "clients.cho", " && (id == 0 || id == st[strd * k + 6]) {", " {", S),
    ("KILL ignores ADDR", "clients.cho", "                    hit = same(buf[0..m], reply.arg_of(view, table, addr_at));", "                    hit = true;", S),
    ("KILL by ID and ADDR kills if either matches", "clients.cho", " && (id == 0 || id == st[strd * k + 6]) {\n                var hit = true;", " {\n                var hit = id == 0 || id == st[strd * k + 6];", S),
    ("KILL TYPE normal kills nobody", "clients.cho", "typed = t == 1;", "typed = t >= 0;", S),
    ("the old form answers a count", "clients.cho", "            return reply.put(sc, at, \"+OK\\r\\n\");\n        }\n        if argc % 2 == 1 {", "            return reply.put_integer(sc, at, count);\n        }\n        if argc % 2 == 1 {", S),
    ("the old form of a missing address is OK", "clients.cho", "            if count == 0 {\n                return reply.put(sc, at, \"-ERR No such client", "            if count < 0 {\n                return reply.put(sc, at, \"-ERR No such client", S),
    ("a filter nobody knows is ignored", "clients.cho", "                } else {\n                    return syntax(sc, at);\n                }\n            }\n            i = i + 2;", "                }\n            }\n            i = i + 2;", S),
    ("a killed connection is not closed", "cache.cho", "                        shut(tab, core, q);\n", "", S),
    ("the caller that kills itself is closed before its answer", "cache.cho", "                    if q == k {\n                        st[p + 7] = 1;", "                    if q == k {\n                        shut(tab, core, q);", S),
    ("CLIENT INFO with an argument is accepted", "cache.cho", "        if info && tb[0] != 2 {", "        if info && tb[0] != 22 {", S),
    ("the last command is not remembered", "cache.cho", "                    if cmd_idx >= 0 {\n                        st[p + 20] = cmd_idx;", "                    if cmd_idx >= 0 && false {\n                        st[p + 20] = cmd_idx;", S),
]

run_mutants(MUTANTS, sys.argv[1] if len(sys.argv) > 1 else "")
