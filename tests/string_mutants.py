#!/usr/bin/env python3
"""Mutation test of the string commands of issue #10, slice 1 (`docs/design.md` section 15): each mutant is a deliberately wrong cache, and the
harnesses must fail on it.

    CANCHO=path/to/cancho python3 tests/string_mutants.py [substring of a mutant's name]

The machinery is `tests/mutlib.py`. A mutant the harness does not fail is a hole in the harness, and this script exits 1.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mutlib import run_mutants

# (name, file, old text, new text, how to test)
MUTANTS = [
    ("APPEND and SETRANGE may build a value longer than a reply buffer holds", "commands.cho", "fn max_string() -> [] int {\n    return 16384;\n}", "fn max_string() -> [] int {\n    return 536870912;\n}", ("py", "limits.py")),
    ("APPEND writes at the start instead of the end", "commands.cho", "    if store.splice(st, key, h, old, value) != 0 {", "    if store.splice(st, key, h, 0, value) != 0 {", ("diff", "APPEND to an existing key")),
    ("APPEND answers the old length", "commands.cho", "    return reply.put_integer(sc, at, old + len(value));\n}\n\n// `SETRANGE", "    return reply.put_integer(sc, at, old);\n}\n\n// `SETRANGE", ("diff", "APPEND to a new key")),
    ("splice leaves the gap unzeroed when the value moves", "store.cho", "        let base = at + header() + meta[m + 1];\n        if offset > old {", "        let base = at + header() + meta[m + 1];\n        if offset > old + 1000000000 {", ("diff", "SETRANGE pads with zeros over old garbage, a new key")),
    ("splice leaves the gap unzeroed in place", "store.cho", "        let base = meta[m] + meta[m + 1];\n        if offset > old {", "        let base = meta[m] + meta[m + 1];\n        if offset > old + 1000000000 {", ("diff", "SETRANGE pads with zeros over old garbage, in place")),
    ("splice forgets the old record is garbage", "store.cho", "        st.dead = st.dead + header() + meta[m + 1] + meta[m + 3];\n        meta[m] = at + header();\n        meta[m + 2] = total;", "        meta[m] = at + header();\n        meta[m + 2] = total;", ("unit",)),
    ("splice leaves a key it created when the arena is full", "store.cho", "            if created {\n                remove_entry(st, e);\n            }\n            return 1;", "            return 1;", ("unit",)),
    ("splice shortens a value it writes into the middle of", "store.cho", "    var total = old;\n    if offset + len(bytes) > total {\n        total = offset + len(bytes);\n    }", "    var total = offset + len(bytes);", ("diff", "SETRANGE that ends before the end leaves the length")),
    ("GETRANGE leaves out the last byte", "commands.cho", "    return reply.put_bulk(sc, at, value[start..end + 1]);", "    return reply.put_bulk(sc, at, value[start..end]);", ("diff", "GETRANGE ranges")),
    ("GETRANGE does not see start past end for two negatives", "commands.cho", "    if start < 0 && end < 0 && start > end {", "    if start < 0 && end < 0 && start > end + 1000 {", ("diff", "GETRANGE ranges")),
    ("SUBSTR is not GETRANGE", "commands.cho", "reply.is_word(name, \"GETRANGE\") || reply.is_word(name, \"SUBSTR\") {\n        if argc != 4 {", "reply.is_word(name, \"GETRANGE\") {\n        if argc != 4 {", ("diff", "SUBSTR")),
    ("SETRANGE with nothing to write creates the key", "commands.cho", "    if len(value) == 0 {\n        return reply.put_integer(sc, at, old);", "    if len(value) == 0 && e >= 0 {\n        return reply.put_integer(sc, at, old);", ("diff", "SETRANGE with an empty value on a missing key")),
    ("SETRANGE accepts a negative offset", "commands.cho", "    if offset < 0 {\n        return reply.put(sc, at, \"-ERR offset is out of range", "    if offset < 0 - 5 {\n        return reply.put(sc, at, \"-ERR offset is out of range", ("diff", "SETRANGE negative offset")),
    ("GETEX PERSIST does not persist", "commands.cho", "    } else if persist {\n        store.set_expiry(st, e, 0);\n    }", "    }", ("diff", "GETEX PERSIST removes the expiry")),
    ("GETEX EX does not set the expiry", "commands.cho", "        } else {\n            store.set_expiry(st, e, when);\n        }\n    } else if persist {", "        } else {\n            store.set_expiry(st, e, 0);\n        }\n    } else if persist {", ("diff", "GETEX EX sets the expiry")),
    ("GETEX of a missing key answers an empty string", "commands.cho", "    let e = store.find(st, key, h);\n    if e < 0 {\n        return reply.put_null(sc, at, proto);\n    }\n    var when = 0;", "    let e = store.find(st, key, h);\n    if e < 0 {\n        return reply.put(sc, at, \"$0\\r\\n\\r\\n\");\n    }\n    var when = 0;", ("diff", "GETEX of a missing key")),
    ("MSETNX sets the keys even when one exists", "commands.cho", "            if store.find(st, key, store.hash_of(st, key)) >= 0 {\n                return reply.put_integer(sc, at, 0);\n            }\n            j = j + 2;", "            j = j + 2;", ("diff", "MSETNX with one existing sets none")),
]

run_mutants(MUTANTS, sys.argv[1] if len(sys.argv) > 1 else "")
