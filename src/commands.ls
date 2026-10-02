edition 5;

module commands;

import store;

// `commands` -- what each command means, over the store (`docs/design.md` §4). Redis is the oracle: the argument
// checks, the order they are made in, and the text of every error are Redis 7.0's, and `tests/differential.py` holds
// them to it byte for byte.
//
// `execute` answers one command into the scratch buffer `sc` from `at` and returns where the answer ends. It never
// traps and never writes more than `reserve()` bytes.
//
// Not here, and said so: the commands that name a time of day (`EXAT`, `PXAT`, `EXPIREAT`, `PEXPIREAT`), because the
// cache has a monotonic clock and no calendar; and everything that is not a string key (lists, hashes, sets, sorted
// sets, streams), which are non-goals.

// The most bytes the answer to one command can take: `MGET` of the most keys the parser allows, each value as large as
// a command can carry (the input buffer, 16 KiB), plus framing.
pub fn reserve() -> [] int {
    return 1114112;
}

// ---------------------------------------------------------------------
// Writing answers
// ---------------------------------------------------------------------

pub fn put[&b, &d](sc: &!b [byte], at: int, data: &d [byte]) -> [] int {
    var i = 0;
    while i < len(data) {
        sc[at + i] = data[i];
        i = i + 1;
    }
    return at + len(data);
}

pub fn put_byte[&b](sc: &!b [byte], at: int, c: int) -> [] int {
    sc[at] = byte_of(c);
    return at + 1;
}

// Decimal digits of `n >= 0`.
fn put_nat[&b](sc: &!b [byte], at: int, n: int) -> [] int {
    var digits = 1;
    var rest = n / 10;
    while rest > 0 {
        digits = digits + 1;
        rest = rest / 10;
    }
    var value = n;
    var i = digits;
    while i > 0 {
        i = i - 1;
        sc[at + i] = byte_of('0' + value % 10);
        value = value / 10;
    }
    return at + digits;
}

fn min_int() -> [] int {
    return 0 - 9223372036854775807 - 1;
}

fn max_int() -> [] int {
    return 9223372036854775807;
}

// Decimal digits of any `n`, with the sign.
fn put_int[&b](sc: &!b [byte], at: int, n: int) -> [] int {
    if n >= 0 {
        return put_nat(sc, at, n);
    }
    if n == min_int() {
        return put(sc, at, "-9223372036854775808");
    }
    return put_nat(sc, put_byte(sc, at, '-'), 0 - n);
}

// `$<length>\r\n<data>\r\n`
fn put_bulk[&b, &d](sc: &!b [byte], at: int, data: &d [byte]) -> [] int {
    var o = put(sc, at, "$");
    o = put_nat(sc, o, len(data));
    o = put(sc, o, "\r\n");
    o = put(sc, o, data);
    return put(sc, o, "\r\n");
}

// `:<n>\r\n`
fn put_integer[&b](sc: &!b [byte], at: int, n: int) -> [] int {
    return put(sc, put_int(sc, put(sc, at, ":"), n), "\r\n");
}

// The answer to a write that was refused: the arena is full, or there is no room for another key. (Redis's text for
// the same thing under its default `noeviction` policy.)
fn refused[&b](sc: &!b [byte], at: int) -> [] int {
    return put(sc, at, "-OOM command not allowed when used memory > 'maxmemory'.\r\n");
}

fn not_an_integer[&b](sc: &!b [byte], at: int) -> [] int {
    return put(sc, at, "-ERR value is not an integer or out of range\r\n");
}

fn syntax_error[&b](sc: &!b [byte], at: int) -> [] int {
    return put(sc, at, "-ERR syntax error\r\n");
}

// An ASCII letter in upper case, anything else as it is.
fn upper(c: int) -> [] int {
    if c >= 'a' && c <= 'z' {
        return c - 32;
    }
    return c;
}

fn lower(c: int) -> [] int {
    if c >= 'A' && c <= 'Z' {
        return c + 32;
    }
    return c;
}

// Is `word` the command or option `name` (written in upper case), whatever case it came in?
fn is_word[&w](word: &w [byte], name: &static [byte]) -> [] bool {
    if len(word) != len(name) {
        return false;
    }
    var i = 0;
    while i < len(word) {
        if upper(int_of(word[i])) != int_of(name[i]) {
            return false;
        }
        i = i + 1;
    }
    return true;
}

fn wrong_arguments[&b, &w](sc: &!b [byte], at: int, name: &w [byte]) -> [] int {
    var o = put(sc, at, "-ERR wrong number of arguments for '");
    var i = 0;
    while i < len(name) {
        o = put_byte(sc, o, lower(int_of(name[i])));
        i = i + 1;
    }
    return put(sc, o, "' command\r\n");
}

// ---------------------------------------------------------------------
// Reading arguments
// ---------------------------------------------------------------------

// Argument `i` of the command `table` describes in `view`.
fn arg_of[&v, &t](view: &v [byte], table: &t [int], i: int) -> [] &v [byte] {
    return view[table[1 + 2 * i]..table[1 + 2 * i] + table[2 + 2 * i]];
}

// `(0, n)` if `text` is a whole decimal integer that fits 64 bits, `(1, 0)` if it is not: an optional minus sign, then digits
// with no leading zero, and nothing else (no plus sign, no spaces). What Redis's `string2ll` takes.
fn parse_int[&t](text: &t [byte]) -> [] (int, int) {
    let n = len(text);
    if n == 0 || n > 20 {
        return (1, 0);
    }
    var i = 0;
    var negative = false;
    if int_of(text[0]) == '-' {
        negative = true;
        i = 1;
        if n == 1 {
            return (1, 0);
        }
    }
    if int_of(text[i]) == '0' && (n - i > 1 || negative) {
        return (1, 0);
    }
    // The magnitude is built as a negative number, because -2^63 fits and 2^63 does not.
    var v = 0;
    while i < n {
        let c = int_of(text[i]);
        if c < '0' || c > '9' {
            return (1, 0);
        }
        let d = c - '0';
        // v * 10 - d must not go below -2^63: v must be at least -922337203685477580, and if it is exactly that, d at most 8.
        if v < 0 - 922337203685477580 || v == 0 - 922337203685477580 && d == 9 {
            return (1, 0);
        }
        v = v * 10 - d;
        i = i + 1;
    }
    if negative {
        return (0, v);
    }
    if v == min_int() {
        return (1, 0);
    }
    return (0, 0 - v);
}

// ---------------------------------------------------------------------
// The commands
// ---------------------------------------------------------------------

// When an expiry `amount` (in seconds if `seconds`, else milliseconds) from now is, in the store's milliseconds: `(0,
// when)`, or `(1, 0)` if the amount does not fit.
fn expire_at(amount: int, seconds: bool, now: int) -> [] (int, int) {
    var ms = amount;
    if seconds {
        if amount > max_int() / 1000 || amount < min_int() / 1000 {
            return (1, 0);
        }
        ms = amount * 1000;
    }
    if ms > max_int() - now {
        return (1, 0);
    }
    return (0, ms + now);
}

// `SET key value [NX | XX] [GET] [EX seconds | PX milliseconds | KEEPTTL]`
fn command_set[&v, &t, &b, &s](view: &v [byte], table: &t [int], sc: &!b [byte], at: int, st: &!s store.Store) -> [] int {
    let argc = table[0];
    var nx = false;
    var xx = false;
    var get = false;
    var keepttl = false;
    // 0 none, 1 EX, 2 PX; and where its argument is.
    var kind = 0;
    var amount_at = 0;
    var i = 3;
    while i < argc {
        let opt = arg_of(view, table, i);
        if is_word(opt, "NX") && !xx {
            nx = true;
        } else if is_word(opt, "XX") && !nx {
            xx = true;
        } else if is_word(opt, "GET") {
            get = true;
        } else if is_word(opt, "KEEPTTL") && kind == 0 {
            keepttl = true;
        } else if is_word(opt, "EX") && !keepttl && kind != 2 && i + 1 < argc {
            kind = 1;
            amount_at = i + 1;
            i = i + 1;
        } else if is_word(opt, "PX") && !keepttl && kind != 1 && i + 1 < argc {
            kind = 2;
            amount_at = i + 1;
            i = i + 1;
        } else {
            return syntax_error(sc, at);
        }
        i = i + 1;
    }
    var expiry = 0;
    if keepttl {
        expiry = 0 - 1;
    }
    if kind != 0 {
        let (status, amount) = parse_int(arg_of(view, table, amount_at));
        if status != 0 {
            return not_an_integer(sc, at);
        }
        if amount <= 0 {
            return put(sc, at, "-ERR invalid expire time in 'set' command\r\n");
        }
        let (bad, when) = expire_at(amount, kind == 1, store.now_of(st));
        if bad != 0 {
            return put(sc, at, "-ERR invalid expire time in 'set' command\r\n");
        }
        expiry = when;
    }
    let key = arg_of(view, table, 1);
    let h = store.hash_of(st, key);
    let e = store.find(st, key, h);
    // What the answer is if the write happens: the old value (`GET`), or OK. The old value is copied out before the write
    // can overwrite it.
    var o = at;
    if get {
        if e >= 0 {
            o = put_bulk(sc, at, store.value(st, e));
        } else {
            o = put(sc, at, "$-1\r\n");
        }
    }
    if nx && e >= 0 || xx && e < 0 {
        if get {
            return o;
        }
        return put(sc, at, "$-1\r\n");
    }
    if store.set(st, key, h, arg_of(view, table, 2), expiry) != 0 {
        return refused(sc, at);
    }
    if get {
        return o;
    }
    return put(sc, at, "+OK\r\n");
}

// `INCR`, `DECR`, `INCRBY`, `DECRBY`: add `delta` to the integer in the key, which starts at 0.
fn add_to[&k, &b, &s, &t](key: &k [byte], delta: int, sc: &!b [byte], at: int, st: &!s store.Store, tmp: &!t [byte]) -> [] int {
    let h = store.hash_of(st, key);
    let e = store.find(st, key, h);
    var current = 0;
    if e >= 0 {
        let (status, n) = parse_int(store.value(st, e));
        if status != 0 {
            return not_an_integer(sc, at);
        }
        current = n;
    }
    if delta < 0 && current < min_int() - delta || delta > 0 && current > max_int() - delta {
        return put(sc, at, "-ERR increment or decrement would overflow\r\n");
    }
    let next = current + delta;
    let size = put_int(tmp, 0, next);
    if store.set(st, key, h, tmp[0..size], 0 - 1) != 0 {
        return refused(sc, at);
    }
    return put_integer(sc, at, next);
}

// `EXPIRE key seconds [NX | XX | GT | LT]` and `PEXPIRE key milliseconds [...]`.
fn command_expire[&v, &t, &b, &s](view: &v [byte], table: &t [int], sc: &!b [byte], at: int, st: &!s store.Store, seconds: bool) -> [] int {
    let argc = table[0];
    let (status, amount) = parse_int(arg_of(view, table, 2));
    if status != 0 {
        return not_an_integer(sc, at);
    }
    var nx = false;
    var xx = false;
    var gt = false;
    var lt = false;
    var i = 3;
    while i < argc {
        let opt = arg_of(view, table, i);
        if is_word(opt, "NX") {
            nx = true;
        } else if is_word(opt, "XX") {
            xx = true;
        } else if is_word(opt, "GT") {
            gt = true;
        } else if is_word(opt, "LT") {
            lt = true;
        } else {
            return put(sc, put(sc, put(sc, at, "-ERR Unsupported option "), opt), "\r\n");
        }
        i = i + 1;
    }
    if nx && (xx || gt || lt) {
        return put(sc, at, "-ERR NX and XX, GT or LT options at the same time are not compatible\r\n");
    }
    if gt && lt {
        return put(sc, at, "-ERR GT and LT options at the same time are not compatible\r\n");
    }
    let now = store.now_of(st);
    let (bad, when) = expire_at(amount, seconds, now);
    if bad != 0 {
        if seconds {
            return put(sc, at, "-ERR invalid expire time in 'expire' command\r\n");
        }
        return put(sc, at, "-ERR invalid expire time in 'pexpire' command\r\n");
    }
    let key = arg_of(view, table, 1);
    let e = store.find(st, key, store.hash_of(st, key));
    if e < 0 {
        return put_integer(sc, at, 0);
    }
    let current = store.expiry_of(st, e);
    // A key with no expiry counts as infinitely far away for GT and LT.
    if nx && current != 0 || xx && current == 0 {
        return put_integer(sc, at, 0);
    }
    if gt && (current == 0 || when <= current) {
        return put_integer(sc, at, 0);
    }
    if lt && current != 0 && when >= current {
        return put_integer(sc, at, 0);
    }
    if when <= now {
        // Already over: the key goes at once.
        store.delete(st, key, store.hash_of(st, key));
    } else {
        store.set_expiry(st, e, when);
    }
    return put_integer(sc, at, 1);
}

// `TTL key` (seconds) and `PTTL key` (milliseconds): -2 if there is no such key, -1 if it has no expiry.
fn command_ttl[&v, &t, &b, &s](view: &v [byte], table: &t [int], sc: &!b [byte], at: int, st: &!s store.Store, seconds: bool) -> [] int {
    let key = arg_of(view, table, 1);
    let e = store.find(st, key, store.hash_of(st, key));
    if e < 0 {
        return put_integer(sc, at, 0 - 2);
    }
    let expiry = store.expiry_of(st, e);
    if expiry == 0 {
        return put_integer(sc, at, 0 - 1);
    }
    var left = expiry - store.now_of(st);
    if left < 0 {
        left = 0;
    }
    if seconds {
        return put_integer(sc, at, (left + 500) / 1000);
    }
    return put_integer(sc, at, left);
}

// `SETEX key seconds value` and `PSETEX key milliseconds value`.
fn command_setex[&v, &t, &b, &s](view: &v [byte], table: &t [int], sc: &!b [byte], at: int, st: &!s store.Store, seconds: bool) -> [] int {
    let (status, amount) = parse_int(arg_of(view, table, 2));
    if status != 0 {
        return not_an_integer(sc, at);
    }
    var bad = amount <= 0;
    var when = 0;
    if !bad {
        let (over, at_ms) = expire_at(amount, seconds, store.now_of(st));
        bad = over != 0;
        when = at_ms;
    }
    if bad {
        if seconds {
            return put(sc, at, "-ERR invalid expire time in 'setex' command\r\n");
        }
        return put(sc, at, "-ERR invalid expire time in 'psetex' command\r\n");
    }
    let key = arg_of(view, table, 1);
    if store.set(st, key, store.hash_of(st, key), arg_of(view, table, 3), when) != 0 {
        return refused(sc, at);
    }
    return put(sc, at, "+OK\r\n");
}

// Answer the command `table` describes in `view`, into `sc` from `at`. `tmp` is a few bytes of scratch (24 or more).
pub fn execute[&v, &t, &b, &s, &x](view: &v [byte], table: &t [int], sc: &!b [byte], at: int, st: &!s store.Store, tmp: &!x [byte]) -> [] int {
    let argc = table[0];
    if argc == 0 {
        return at;
    }
    let name = arg_of(view, table, 0);
    if is_word(name, "GET") {
        if argc != 2 {
            return wrong_arguments(sc, at, name);
        }
        let key = arg_of(view, table, 1);
        let e = store.find(st, key, store.hash_of(st, key));
        if e < 0 {
            return put(sc, at, "$-1\r\n");
        }
        return put_bulk(sc, at, store.value(st, e));
    }
    if is_word(name, "SET") {
        if argc < 3 {
            return wrong_arguments(sc, at, name);
        }
        return command_set(view, table, sc, at, st);
    }
    if is_word(name, "PING") {
        if argc == 1 {
            return put(sc, at, "+PONG\r\n");
        }
        if argc == 2 {
            return put_bulk(sc, at, arg_of(view, table, 1));
        }
        return wrong_arguments(sc, at, name);
    }
    if is_word(name, "ECHO") {
        if argc == 2 {
            return put_bulk(sc, at, arg_of(view, table, 1));
        }
        return wrong_arguments(sc, at, name);
    }
    if is_word(name, "DEL") || is_word(name, "UNLINK") {
        if argc < 2 {
            return wrong_arguments(sc, at, name);
        }
        var removed = 0;
        var k = 1;
        while k < argc {
            let key = arg_of(view, table, k);
            removed = removed + store.delete(st, key, store.hash_of(st, key));
            k = k + 1;
        }
        return put_integer(sc, at, removed);
    }
    if is_word(name, "EXISTS") || is_word(name, "TOUCH") {
        if argc < 2 {
            return wrong_arguments(sc, at, name);
        }
        var found = 0;
        var k = 1;
        while k < argc {
            let key = arg_of(view, table, k);
            if store.find(st, key, store.hash_of(st, key)) >= 0 {
                found = found + 1;
            }
            k = k + 1;
        }
        return put_integer(sc, at, found);
    }
    if is_word(name, "INCR") || is_word(name, "DECR") {
        if argc != 2 {
            return wrong_arguments(sc, at, name);
        }
        var delta = 1;
        if is_word(name, "DECR") {
            delta = 0 - 1;
        }
        return add_to(arg_of(view, table, 1), delta, sc, at, st, tmp);
    }
    if is_word(name, "INCRBY") || is_word(name, "DECRBY") {
        if argc != 3 {
            return wrong_arguments(sc, at, name);
        }
        let (status, n) = parse_int(arg_of(view, table, 2));
        if status != 0 {
            return not_an_integer(sc, at);
        }
        if is_word(name, "DECRBY") {
            if n == min_int() {
                return put(sc, at, "-ERR decrement would overflow\r\n");
            }
            return add_to(arg_of(view, table, 1), 0 - n, sc, at, st, tmp);
        }
        return add_to(arg_of(view, table, 1), n, sc, at, st, tmp);
    }
    if is_word(name, "EXPIRE") || is_word(name, "PEXPIRE") {
        if argc < 3 {
            return wrong_arguments(sc, at, name);
        }
        return command_expire(view, table, sc, at, st, is_word(name, "EXPIRE"));
    }
    if is_word(name, "TTL") || is_word(name, "PTTL") {
        if argc != 2 {
            return wrong_arguments(sc, at, name);
        }
        return command_ttl(view, table, sc, at, st, is_word(name, "TTL"));
    }
    if is_word(name, "PERSIST") {
        if argc != 2 {
            return wrong_arguments(sc, at, name);
        }
        let key = arg_of(view, table, 1);
        let e = store.find(st, key, store.hash_of(st, key));
        if e < 0 || store.expiry_of(st, e) == 0 {
            return put_integer(sc, at, 0);
        }
        store.set_expiry(st, e, 0);
        return put_integer(sc, at, 1);
    }
    if is_word(name, "SETNX") {
        if argc != 3 {
            return wrong_arguments(sc, at, name);
        }
        let key = arg_of(view, table, 1);
        let h = store.hash_of(st, key);
        if store.find(st, key, h) >= 0 {
            return put_integer(sc, at, 0);
        }
        if store.set(st, key, h, arg_of(view, table, 2), 0) != 0 {
            return refused(sc, at);
        }
        return put_integer(sc, at, 1);
    }
    if is_word(name, "SETEX") || is_word(name, "PSETEX") {
        if argc != 4 {
            return wrong_arguments(sc, at, name);
        }
        return command_setex(view, table, sc, at, st, is_word(name, "SETEX"));
    }
    if is_word(name, "GETSET") {
        if argc != 3 {
            return wrong_arguments(sc, at, name);
        }
        let key = arg_of(view, table, 1);
        let h = store.hash_of(st, key);
        let e = store.find(st, key, h);
        var o = at;
        if e >= 0 {
            o = put_bulk(sc, at, store.value(st, e));
        } else {
            o = put(sc, at, "$-1\r\n");
        }
        if store.set(st, key, h, arg_of(view, table, 2), 0) != 0 {
            return refused(sc, at);
        }
        return o;
    }
    if is_word(name, "GETDEL") {
        if argc != 2 {
            return wrong_arguments(sc, at, name);
        }
        let key = arg_of(view, table, 1);
        let h = store.hash_of(st, key);
        let e = store.find(st, key, h);
        if e < 0 {
            return put(sc, at, "$-1\r\n");
        }
        let o = put_bulk(sc, at, store.value(st, e));
        store.delete(st, key, h);
        return o;
    }
    if is_word(name, "MGET") {
        if argc < 2 {
            return wrong_arguments(sc, at, name);
        }
        var o = put(sc, put_nat(sc, put(sc, at, "*"), argc - 1), "\r\n");
        var k = 1;
        while k < argc {
            let key = arg_of(view, table, k);
            let e = store.find(st, key, store.hash_of(st, key));
            if e < 0 {
                o = put(sc, o, "$-1\r\n");
            } else {
                o = put_bulk(sc, o, store.value(st, e));
            }
            k = k + 1;
        }
        return o;
    }
    if is_word(name, "MSET") {
        if argc < 3 || argc % 2 == 0 {
            return wrong_arguments(sc, at, name);
        }
        var k = 1;
        while k < argc {
            let key = arg_of(view, table, k);
            if store.set(st, key, store.hash_of(st, key), arg_of(view, table, k + 1), 0) != 0 {
                return refused(sc, at);
            }
            k = k + 2;
        }
        return put(sc, at, "+OK\r\n");
    }
    if is_word(name, "STRLEN") {
        if argc != 2 {
            return wrong_arguments(sc, at, name);
        }
        let key = arg_of(view, table, 1);
        let e = store.find(st, key, store.hash_of(st, key));
        if e < 0 {
            return put_integer(sc, at, 0);
        }
        return put_integer(sc, at, len(store.value(st, e)));
    }
    if is_word(name, "TYPE") {
        if argc != 2 {
            return wrong_arguments(sc, at, name);
        }
        let key = arg_of(view, table, 1);
        if store.find(st, key, store.hash_of(st, key)) < 0 {
            return put(sc, at, "+none\r\n");
        }
        return put(sc, at, "+string\r\n");
    }
    if is_word(name, "DBSIZE") {
        if argc != 1 {
            return wrong_arguments(sc, at, name);
        }
        return put_integer(sc, at, store.live(st));
    }
    if is_word(name, "FLUSHALL") || is_word(name, "FLUSHDB") {
        if argc > 2 {
            return syntax_error(sc, at);
        }
        if argc == 2 && !is_word(arg_of(view, table, 1), "ASYNC") && !is_word(arg_of(view, table, 1), "SYNC") {
            return syntax_error(sc, at);
        }
        store.clear(st);
        return put(sc, at, "+OK\r\n");
    }
    if is_word(name, "SELECT") {
        if argc != 2 {
            return wrong_arguments(sc, at, name);
        }
        let (status, n) = parse_int(arg_of(view, table, 1));
        if status != 0 {
            return not_an_integer(sc, at);
        }
        if n != 0 {
            return put(sc, at, "-ERR DB index is out of range\r\n");
        }
        return put(sc, at, "+OK\r\n");
    }
    // Redis names the command (at most 128 bytes of it) and then the arguments, as `'arg' ` each, until the
    // text so far is 128 bytes: the quotes and spaces count, and the last argument is cut to what is left.
    var shown = len(name);
    if shown > 128 {
        shown = 128;
    }
    var o = put(sc, at, "-ERR unknown command '");
    o = put(sc, o, name[0..shown]);
    o = put(sc, o, "', with args beginning with: ");
    var spent = 0;
    var k = 1;
    while k < argc && spent < 128 {
        var take = table[2 + 2 * k];
        if take > 128 - spent {
            take = 128 - spent;
        }
        o = put(sc, o, "'");
        o = put(sc, o, arg_of(view, table, k)[0..take]);
        o = put(sc, o, "' ");
        spent = spent + take + 3;
        k = k + 1;
    }
    return put(sc, o, "\r\n");
}
