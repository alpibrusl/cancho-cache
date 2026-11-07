#!/usr/bin/env python3
"""Mutation test of the slow log (`docs/design.md` section 31): each mutant is a deliberately wrong cache, and `tests/slowlog.py` must fail on it.

    CANCHO=path/to/cancho python3 tests/slowlog_mutants.py [substring of a mutant's name]

The machinery is `tests/mutlib.py`. A mutant the harness does not fail is a hole in the harness, and this script exits 1.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mutlib import run_mutants

S = ("py", "slowlog.py")
# (name, file, old text, new text, how to test)
MUTANTS = [
    ("an argument is not cut at 128 bytes", "slowlog.cho", "fn most_bytes() -> [] int {\n    return 128;", "fn most_bytes() -> [] int {\n    return 100000;", S),
    ("an argument is cut at 127 bytes", "slowlog.cho", "fn most_bytes() -> [] int {\n    return 128;", "fn most_bytes() -> [] int {\n    return 127;", S),
    ("every argument is kept", "slowlog.cho", "fn most_arguments() -> [] int {\n    return 32;", "fn most_arguments() -> [] int {\n    return 1000;", S),
    ("the marker counts the arguments wrong", "slowlog.cho", "argc - shown);", "argc - shown + 1);", S),
    ("AUTH is logged in the clear", "slowlog.cho", "        return i >= 1;", "        return false;", S),
    ("only the first argument of AUTH is redacted", "slowlog.cho", "        return i >= 1;", "        return i == 1;", S),
    ("HELLO's credentials are logged", "slowlog.cho", "(i == j + 1 || i == j + 2)", "(i == j + 5)", S),
    ("HELLO redacts the password only", "slowlog.cho", "(i == j + 1 || i == j + 2)", "(i == j + 2)", S),
    ("entries are listed oldest first", "slowlog.cho", "        let id = next - 1 - k;", "        let id = next - n + k;", S),
    ("the default count is 9", "slowlog.cho", "        var wanted = 10;", "        var wanted = 9;", S),
    ("a count of -1 is an error", "slowlog.cho", "n < 0 - 1 {", "n < 0 {", S),
    ("GET 0 answers everything", "slowlog.cho", "    if wanted >= 0 && wanted < n {", "    if wanted > 0 && wanted < n {", S),
    ("the client's name is not logged", "slowlog.cho", "    text[o] = byte_of(len(name));\n    o = reply.put(text, o + 1, name);", "    text[o] = byte_of(0);\n    o = o + 1;", S),
    ("RESET does not clear", "slowlog.cho", "        store.slow_clear(st);\n", "", S),
    ("a negative threshold does not switch the log off", "cache.cho", "    if limit >= 0 && took >= limit &&", "    if took >= limit &&", S),
    ("a command as slow as the threshold is not logged", "cache.cho", "    if limit >= 0 && took >= limit &&", "    if limit >= 0 && took > limit &&", S),
    ("commands that did not run are logged", "cache.cho", " && !stats.did_not_run(sc, from, to) {", " {", S),
    ("a queued command is logged", "stats.cho", "reply_starts(sc, from, to, \"+QUEUED\") || ", "", S),
    ("an unknown command is logged", "stats.cho", " || reply_starts(sc, from, to, \"-ERR unknown command\")", "", S),
    ("CONFIG is not timed, so it is not logged when it switches the log on", "cache.cho", " || reply.is_word(reply.arg_of(view, tb, 0), \"CONFIG\");", ";", S),
    ("MULTI is not logged", "cache.cho", "                    if txn_timed && status == 1 {", "                    if false && status == 1 {", S),
]

run_mutants(MUTANTS, sys.argv[1] if len(sys.argv) > 1 else "")
