#!/usr/bin/env python3
"""Mutation test of the keyspace commands of issue #10, slice 2 (`docs/design.md` section 16): each mutant is a deliberately wrong cache, and
the harnesses must fail on it.

    CANCHO=path/to/cancho python3 tests/keyspace_mutants.py [substring of a mutant's name]

The machinery is `tests/mutlib.py`. A mutant the harness does not fail is a hole in the harness, and this script exits 1.
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mutlib import run_mutants

# (name, file, old text, new text, how to test)
MUTANTS = [
    ("the matcher's `*` does not give bytes back", "glob.cho", "                star_s = star_s + 1;\n                s = star_s;\n                p = star_p;", "                return false;", ("globunit",)),
    ("the matcher ignores the `^` of a set", "glob.cho", "        if not {\n            matched = !matched;\n        }", "        if not && false {\n            matched = !matched;\n        }", ("globunit",)),
    ("the matcher does not turn a backwards range round", "glob.cho", "                if first > last {\n                    let t = first;\n                    first = last;\n                    last = t;\n                }", "", ("globunit",)),
    ("the matcher does not read `\\` as an escape", "glob.cho", "    if ch == '\\\\' && p + 1 < n {\n        return (int_of(pattern[p + 1]) == c, p + 2);\n    }", "", ("globunit",)),
    ("KEYS answers all the keys whatever the pattern", "commands.cho", "            if all || glob.matches(pattern, key) {\n                count = count + 1;", "            if true {\n                count = count + 1;", ("py", "keyspace.py")),
    ("SCAN always says it is done", "commands.cho", "    var next = e;\n    if e >= n {\n        next = 0;\n    }", "    var next = 0;", ("py", "keyspace.py")),
    ("SCAN ignores MATCH", "commands.cho", "pattern_at);\n        filtered = !(len(pattern) == 1 && int_of(pattern[0]) == '*');", "pattern_at);\n        filtered = false;", ("py", "keyspace.py")),
    ("SCAN lets a key that has run out through", "store.cho", "    return meta[m + 6] == 0 - 2 && (meta[m + 4] == 0 || meta[m + 4] > st.now);", "    return meta[m + 6] == 0 - 2;", ("unit",)),
    ("SCAN takes a cursor that is not a number", "commands.cho", "        if c < '0' || c > '9' {\n            return (1, 0);\n        }\n        if c != '0' && first < 0 {", "        if c < '0' || c > '9' {\n            return (0, 0);\n        }\n        if c != '0' && first < 0 {", ("diff", "SCAN cursor errors")),
    ("RENAME leaves the key it overwrites", "store.cho", "    if d >= 0 && d != e {\n        remove_entry(st, d);\n    }", "", ("diff", "RENAME over an existing key")),
    ("RENAME drops the expiry", "store.cho", "    meta[m + 5] = hd;\n    meta[m + 7] = use_stamp(st);", "    meta[m + 5] = hd;\n    put_expiry(st, m, 0);\n    meta[m + 7] = st.now;", ("diff", "RENAME moves the expiry")),
    ("RENAME forgets the old record is garbage", "store.cho", "    st.dead = st.dead + header() + meta[m + 1] + meta[m + 3];\n    meta[m] = at + header();\n    meta[m + 1] = len(dst);", "    meta[m] = at + header();\n    meta[m + 1] = len(dst);", ("unit",)),
    ("RENAME leaves the old name in the index", "store.cho", "    bump(st, meta[m + 5]);\n    close_hole(st, slot);\n    st.dead", "    bump(st, meta[m + 5]);\n    st.dead", ("py", "keyspace.py")),
    ("RENAME does not record the new hash", "store.cho", "    meta[m + 5] = hd;\n    meta[m + 7] = use_stamp(st);", "    meta[m + 7] = st.now;", ("py", "keyspace.py")),
    ("RENAME does not mark the old name for WATCH", "store.cho", "    let slot = slot_of(st, e);\n    bump(st, meta[m + 5]);\n    close_hole(st, slot);", "    let slot = slot_of(st, e);\n    close_hole(st, slot);", ("diff", "RENAME dirties the old name for WATCH")),
    ("RENAMENX renames onto an existing key", "commands.cho", "    if nx && store.find(st, dst, hd) >= 0 {\n        return reply.put_integer(sc, at, 0);\n    }", "", ("diff", "RENAMENX onto an existing key")),
    ("COPY drops the expiry", "store.cho", "    let w = commit(st, ne, meta[m + 4]);", "    let w = commit(st, ne, 0);", ("diff", "COPY keeps the expiry")),
    ("COPY overwrites without REPLACE", "store.cho", "    if !replace && find(st, dst, hd) >= 0 {\n        return 4;\n    }", "", ("diff", "COPY onto an existing key")),
    ("COPY of a hash makes a string", "store.cho", "    set_kind(st, w, kind);\n    return 0;", "    return 0;", ("diff", "COPY of a hash")),
    ("OBJECT ENCODING never says int", "commands.cho", "    if status == 0 {\n        return reply.put_bulk(sc, at, \"int\");", "    if status == 5 {\n        return reply.put_bulk(sc, at, \"int\");", ("diff", "OBJECT ENCODING of strings")),
    ("OBJECT ENCODING lets a hash grow past 512 fields", "commands.cho", "    return n <= 512;", "    return true;", ("diff", "OBJECT ENCODING of a hash of 512")),
    ("EXPIRETIME forgets the offset", "commands.cho", "        let whole = expiry + store.wall_of(st);", "        let whole = expiry;", ("diff", "EXPIREAT, EXPIRETIME and PEXPIRETIME")),
    ("EXPIREAT treats seconds as milliseconds", "commands.cho", "        ms = amount * 1000;\n    }\n    if ms <= now + wall {", "    }\n    if ms <= now + wall {", ("diff", "EXPIREAT, EXPIRETIME and PEXPIRETIME")),
    ("EXPIREAT in the past leaves the key to expire later", "commands.cho", "    if when <= now {\n        // Already over: the key goes at once.", "    if when < now {\n        // Already over: the key goes at once.", ("diff", "EXPIREAT in the past deletes")),
    ("SET takes EXAT as seconds from now", "commands.cho", "        if kind >= 3 {\n            let (b2, w2) = expire_abs(amount, kind == 3, store.wall_of(st), store.now_of(st));\n            bad = b2;\n            when = w2;\n        } else {\n            let (b2, w2) = expire_at(amount, kind == 1, store.now_of(st));\n            bad = b2;\n            when = w2;\n        }\n        if bad != 0 {\n            return reply.put(sc, at, \"-ERR invalid expire time in 'set' command", "        if kind >= 3 {\n            let (b2, w2) = expire_at(amount, kind == 3, store.now_of(st));\n            bad = b2;\n            when = w2;\n        } else {\n            let (b2, w2) = expire_at(amount, kind == 1, store.now_of(st));\n            bad = b2;\n            when = w2;\n        }\n        if bad != 0 {\n            return reply.put(sc, at, \"-ERR invalid expire time in 'set' command", ("diff", "SET with EXAT and PXAT")),
    ("COMMAND INFO of an unknown name answers an empty entry", "session.cho", "                o = reply.put_null(sc, o, proto);\n            }\n            i = i + 1;", "                o = reply.put(sc, o, \"*0\\r\\n\");\n            }\n            i = i + 1;", ("diff", "COMMAND INFO of an unknown name")),
    ("COMMAND INFO answers RESP2 entries to a RESP3 client", "session.cho", "    let resp3 = proto == 3;", "    let resp3 = false;", ("diff", "COMMAND INFO get, RESP3")),
    ("COMMAND COUNT is one more than the list", "session.cho", "        return reply.put_integer(sc, at, cmdinfo.count());", "        return reply.put_integer(sc, at, cmdinfo.count() + 1);", ("py", "session.py")),
    ("COMMAND GETKEYS answers the first key only", "session.cho", "            o = reply.put_bulk(sc, o, reply.arg_of(view, table, 2 + i));\n            i = i + step;", "            o = reply.put_bulk(sc, o, reply.arg_of(view, table, 2 + i));\n            i = i + step + n;", ("diff", "COMMAND GETKEYS, several keys")),
    ("COMMAND GETKEYS looks at the arity before the keys", "session.cho", "    if first == 0 {\n        return reply.put(sc, at, \"-ERR The command has no key arguments\\r\\n\");\n    }\n    if arity > 0", "    if arity > 0", ("diff", "COMMAND GETKEYS, commands without keys")),
    ("COMMAND DOCS answers every name asked, known or not", "session.cho", "                if hit {\n                    found = found + 1;\n                }", "                found = found + 1;", ("diff", "COMMAND DOCS of several names")),
    ("COMMAND GETKEYSANDFLAGS gives SET's key no flags from the GET", "session.cho", "        if reply.is_word(reply.arg_of(view, table, 2 + i), \"GET\") {\n            reads = true;\n        }", "", ("diff", "COMMAND GETKEYSANDFLAGS, SET")),
    ("COMMAND GETKEYSANDFLAGS writes one key of a range", "session.cho", "                o = cmdkeys.put_spec_flags(name, word, k, sc, o, resp3);\n                }\n                i = i + kstep;", "                o = cmdkeys.put_spec_flags(name, word, k, sc, o, resp3);\n                }\n                i = i + kstep + n;", ("diff", "COMMAND GETKEYSANDFLAGS, several keys")),
    ("a use is stamped with the millisecond alone", "store.cho", "    return st.now * stamp_bits() + st.seq;", "    return st.now * stamp_bits();", ("unit",)),
    ("the count of uses starts over at every tick", "store.cho", "    if now != st.now {\n        st.seq = 0;\n    }\n    st.now = now;", "    st.seq = 0;\n    st.now = now;", ("unit",)),
    ("RANDOMKEY answers nothing", "commands.cho", "        let e = store.random_visible(st);\n        if e < 0 {", "        let e = 0 - 1;\n        if e < 0 {", ("diff", "RANDOMKEY of one key")),
]

run_mutants(MUTANTS, sys.argv[1] if len(sys.argv) > 1 else "")
