#!/usr/bin/env python3
"""Mutation test of the answers written in pieces and the lifted argument limit of issue #11, first slice (`docs/design.md` section 17): each mutant is
a deliberately wrong cache, and the harnesses must fail on it.

    CANCHO=path/to/cancho python3 tests/pieces_mutants.py [substring of a mutant's name]

The machinery is `tests/mutlib.py`. A mutant the harness does not fail is a hole in the harness, and this script exits 1.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mutlib import run_mutants

# (name, file, old text, new text, how to test)
MUTANTS = [
    ("MGET writes all its answer at once", "commands.cho", "        while k < argc && o + max_string() + 32 <= at + piece() {", "        while k < argc {", ("py", "limits.py")),
    ("MGET forgets where it was", "commands.cho", "        var k = link[4];", "        var k = 0;", ("py", "limits.py")),
    ("MGET never says it has more", "commands.cho", "        if k < argc {\n            link[4] = k;\n        } else {\n            link[4] = 0;\n        }", "        link[4] = 0;", ("py", "limits.py")),
    ("a command that is part way is taken as done", "cache.cho", "                    st[p + 2] = emit(tab, k, sc[0..at], pd[obase..obase + output_size()], st[p + 2]);\n                    at = 0;\n                    n = 0;", "                    st[p + 2] = emit(tab, k, sc[0..at], pd[obase..obase + output_size()], st[p + 2]);\n                    at = 0;", ("py", "limits.py")),
    ("an EXEC that resumes is taken for a command that is part way", "cache.cho", "                if status == 0 && st[p + 9] != 0 {", "                if st[p + 9] != 0 {", ("py", "limits.py")),
    ("an EXEC that resumes writes its header again", "txn.cho", "    if reply.is_word(name, \"EXEC\") && tx[22] == 1 {", "    if reply.is_word(name, \"EXEC\") && tx[22] == 2 {", ("py", "limits.py")),
    ("an EXEC does not wait for its client", "cache.cho", "                    if qi < tx[2] && st[p + 2] > 0 {", "                    if false {", ("py", "limits.py")),
    ("answers are sent only when the buffer is nearly full", "cache.cho", "fn flush_at() -> [] int {\n    return 32768;\n}", "fn flush_at() -> [] int {\n    return 1073741824;\n}", ("py", "limits.py")),
    ("a command may have 64 arguments again", "resp.cho", "fn max_args() -> [] int {\n    return 2048;\n}", "fn max_args() -> [] int {\n    return 64;\n}", ("diff", "MGET of 100 keys")),
]

run_mutants(MUTANTS, sys.argv[1] if len(sys.argv) > 1 else "")
