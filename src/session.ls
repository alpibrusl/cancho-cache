edition 5;

module session;

import reply;
import store;

// `session` -- the commands about the connection and the server rather than about keys: what a client library says when it
// connects (`HELLO`, `CLIENT SETNAME`, `INFO` for a ready check, `AUTH`, `CONFIG GET`) and `QUIT`. Redis 7.0 is the oracle for every
// error text that does not depend on the server's own state; where the cache is deliberately not Redis it says so in the answer.
//
// What a command may read and write beyond the store:
//
//   * `session`, three ints of this connection: `[0]` the length of its name (`CLIENT SETNAME`), `[1]` its id, `[2]` set to 1 to ask the
//     loop to close the connection once the answers are sent (`QUIT`);
//   * `names`, 64 bytes for that name;
//   * `world`, read only: `[0]` connections now, `[1]` when the process started (the store's milliseconds), `[2]` commands answered,
//     `[3]` connections ever accepted, `[4]` the port.
//
// Not here: authentication that does anything (the default user has no password, as in Redis's default configuration, and any
// other user is refused), RESP3 (`HELLO 3` is refused with Redis's own text for an unsupported version), `CLIENT LIST`/`KILL`/...,
// `CONFIG SET`, and `INFO` sections beyond the five printed.

// How many commands `COMMAND COUNT` says and `COMMAND LIST` lists: `tests/session_test.py` checks that every one is answered.
pub fn supported() -> [] int {
    return 38;
}

// The most bytes a client name may have here.
fn name_room() -> [] int {
    return 64;
}

fn valid_name[&n](name: &n [byte]) -> [] bool {
    var i = 0;
    while i < len(name) {
        let c = int_of(name[i]);
        if c < 33 || c > 126 {
            return false;
        }
        i = i + 1;
    }
    return true;
}

// `Ok` (0), `1` if the name has a character Redis refuses, `2` if it is longer than this server keeps.
fn remember[&s, &n, &x](session: &!s [int], names: &!n [byte], name: &x [byte]) -> [] int {
    if !valid_name(name) {
        return 1;
    }
    if len(name) > name_room() {
        return 2;
    }
    var i = 0;
    while i < len(name) {
        names[i] = name[i];
        i = i + 1;
    }
    session[0] = len(name);
    return 0;
}

fn name_error[&b](sc: &!b [byte], at: int, code: int) -> [] int {
    if code == 1 {
        return reply.put(sc, at, "-ERR Client names cannot contain spaces, newlines or special characters.\r\n");
    }
    return reply.put(sc, at, "-ERR client name too long: this server keeps at most 64 bytes\r\n");
}

fn wrong_password[&b](sc: &!b [byte], at: int) -> [] int {
    return reply.put(sc, at, "-WRONGPASS invalid username-password pair or user is disabled.\r\n");
}

// The server's description for connection id `id`: a flat array of pairs in RESP2, a map in RESP3.
fn put_hello_map[&b](sc: &!b [byte], at: int, id: int, proto: int) -> [] int {
    var o = reply.put(sc, at, "*14\r\n");
    if proto == 3 {
        o = reply.put(sc, at, "%7\r\n");
    }
    o = reply.put_bulk(sc, o, "server");
    o = reply.put_bulk(sc, o, "redis");
    o = reply.put_bulk(sc, o, "version");
    o = reply.put_bulk(sc, o, "7.0.15");
    o = reply.put_bulk(sc, o, "proto");
    o = reply.put_integer(sc, o, proto);
    o = reply.put_bulk(sc, o, "id");
    o = reply.put_integer(sc, o, id);
    o = reply.put_bulk(sc, o, "mode");
    o = reply.put_bulk(sc, o, "standalone");
    o = reply.put_bulk(sc, o, "role");
    o = reply.put_bulk(sc, o, "master");
    o = reply.put_bulk(sc, o, "modules");
    return reply.put(sc, o, "*0\r\n");
}

// `HELLO [protover [AUTH username password] [SETNAME clientname]]`: the server's description as a flat array (RESP2). Version 3 is
// refused the way Redis refuses a version it does not have. The options are all read first, then the user checked, then the name set,
// in Redis's order.
pub fn hello[&v, &t, &b, &s, &n](view: &v [byte], table: &t [int], sc: &!b [byte], at: int, session: &!s [int], names: &!n [byte]) -> [] int {
    let argc = table[0];
    var next = 1;
    // No version given: the connection keeps the protocol it has.
    var chosen = session[3];
    if argc >= 2 {
        let (status, version) = reply.parse_int(reply.arg_of(view, table, 1));
        if status != 0 {
            return reply.put(sc, at, "-ERR Protocol version is not an integer or out of range\r\n");
        }
        if version != 2 && version != 3 {
            return reply.put(sc, at, "-NOPROTO unsupported protocol version\r\n");
        }
        chosen = version;
        next = 2;
    }
    var user = 0 - 1;
    var named = 0 - 1;
    while next < argc {
        let option = reply.arg_of(view, table, next);
        let more = argc - 1 - next;
        if reply.is_word(option, "AUTH") && more >= 2 {
            user = next + 1;
            next = next + 3;
        } else if reply.is_word(option, "SETNAME") && more >= 1 {
            named = next + 1;
            next = next + 2;
        } else {
            var o = reply.put(sc, at, "-ERR Syntax error in HELLO option '");
            o = reply.put(sc, o, option);
            return reply.put(sc, o, "'\r\n");
        }
    }
    if user >= 0 && !reply.is_word_exact(reply.arg_of(view, table, user), "default") {
        return wrong_password(sc, at);
    }
    if named >= 0 {
        let code = remember(session, names, reply.arg_of(view, table, named));
        if code != 0 {
            return name_error(sc, at, code);
        }
    }
    session[3] = chosen;
    return put_hello_map(sc, at, session[1], chosen);
}

// `AUTH password` and `AUTH username password`. The default user has no password (Redis's default configuration), so the one-argument
// form is refused with Redis's text for that, and the two-argument form succeeds for the default user and fails for any other.
pub fn auth[&v, &t, &b](view: &v [byte], table: &t [int], sc: &!b [byte], at: int) -> [] int {
    let argc = table[0];
    if argc == 2 {
        return reply.put(sc, at, "-ERR AUTH <password> called without any password configured for the default user. Are you sure your configuration is correct?\r\n");
    }
    if argc == 3 {
        if reply.is_word_exact(reply.arg_of(view, table, 1), "default") {
            return reply.put(sc, at, "+OK\r\n");
        }
        return wrong_password(sc, at);
    }
    if argc > 3 {
        return reply.syntax_error(sc, at);
    }
    return reply.wrong_arguments(sc, at, reply.arg_of(view, table, 0));
}

fn client_arity[&b](sc: &!b [byte], at: int, sub: &static [byte]) -> [] int {
    return reply.put(sc, reply.put(sc, reply.put(sc, at, "-ERR wrong number of arguments for 'client|"), sub), "' command\r\n");
}

// `CLIENT ID`, `CLIENT GETNAME`, `CLIENT SETNAME name`. Redis 7.0's other subcommands are answered with its text for a subcommand that
// does not exist, which includes `SETINFO` (added in 7.2: clients ignore that answer).
pub fn client[&v, &t, &b, &s, &n](view: &v [byte], table: &t [int], sc: &!b [byte], at: int, session: &!s [int], names: &!n [byte]) -> [] int {
    let argc = table[0];
    if argc == 1 {
        return reply.wrong_arguments(sc, at, reply.arg_of(view, table, 0));
    }
    let sub = reply.arg_of(view, table, 1);
    if reply.is_word(sub, "ID") {
        if argc != 2 {
            return client_arity(sc, at, "id");
        }
        return reply.put_integer(sc, at, session[1]);
    }
    if reply.is_word(sub, "GETNAME") {
        if argc != 2 {
            return client_arity(sc, at, "getname");
        }
        if session[0] == 0 {
            return reply.put_null(sc, at, session[3]);
        }
        return reply.put_bulk(sc, at, names[0..session[0]]);
    }
    if reply.is_word(sub, "SETNAME") {
        if argc != 3 {
            return client_arity(sc, at, "setname");
        }
        let code = remember(session, names, reply.arg_of(view, table, 2));
        if code != 0 {
            return name_error(sc, at, code);
        }
        return reply.put(sc, at, "+OK\r\n");
    }
    var shown = len(sub);
    if shown > 128 {
        shown = 128;
    }
    var o = reply.put(sc, at, "-ERR unknown subcommand '");
    o = reply.put(sc, o, sub[0..shown]);
    return reply.put(sc, o, "'. Try CLIENT HELP.\r\n");
}

// `QUIT`: OK, then the loop closes the connection once it has been sent. `RESET`: forget the connection's name.
pub fn quit[&b, &s](sc: &!b [byte], at: int, session: &!s [int]) -> [] int {
    session[2] = 1;
    return reply.put(sc, at, "+OK\r\n");
}

pub fn reset[&b, &s](sc: &!b [byte], at: int, session: &!s [int]) -> [] int {
    session[0] = 0;
    session[3] = 2;
    return reply.put(sc, at, "+RESET\r\n");
}

// ---------------------------------------------------------------------
// INFO
// ---------------------------------------------------------------------

fn kv[&b](sc: &!b [byte], at: int, key: &static [byte], value: int) -> [] int {
    return reply.put(sc, reply.put_int(sc, reply.put(sc, reply.put(sc, at, key), ":"), value), "\r\n");
}

fn wants[&v, &t](view: &v [byte], table: &t [int], section: &static [byte]) -> [] bool {
    if table[0] == 1 {
        return true;
    }
    let asked = reply.arg_of(view, table, 1);
    return reply.is_word(asked, section) || reply.is_word(asked, "ALL") || reply.is_word(asked, "EVERYTHING") || reply.is_word(asked, "DEFAULT");
}

// The text of `INFO`: five sections, each as Redis prints it (`# Name`, `field:value` lines, a blank line). `redis_version` is
// what clients read to decide which commands to try, and the command set here is Redis 7.0's, so that is what it says; `server_name`
// says what this is.
fn info_text[&v, &t, &b, &s, &w](view: &v [byte], table: &t [int], sc: &!b [byte], at: int, st: &s store.Store, world: &w [int]) -> [] int {
    var o = at;
    if wants(view, table, "SERVER") {
        o = reply.put(sc, o, "# Server\r\nredis_version:7.0.15\r\nserver_name:lexsys-cache\r\nredis_mode:standalone\r\narch_bits:64\r\n");
        o = kv(sc, o, "tcp_port", world[4]);
        o = kv(sc, o, "uptime_in_seconds", (store.now_of(st) - world[1]) / 1000);
        o = reply.put(sc, o, "\r\n");
    }
    if wants(view, table, "CLIENTS") {
        o = reply.put(sc, o, "# Clients\r\n");
        o = kv(sc, o, "connected_clients", world[0]);
        o = reply.put(sc, o, "\r\n");
    }
    if wants(view, table, "MEMORY") {
        o = reply.put(sc, o, "# Memory\r\n");
        o = kv(sc, o, "used_memory", store.used(st));
        o = kv(sc, o, "used_memory_dead", store.dead(st));
        o = kv(sc, o, "maxmemory", store.capacity(st));
        o = reply.put(sc, o, "maxmemory_policy:");
        if store.policy_of(st) == store.evict_lru() {
            o = reply.put(sc, o, "allkeys-lru");
        } else {
            o = reply.put(sc, o, "noeviction");
        }
        o = reply.put(sc, o, "\r\n\r\n");
    }
    if wants(view, table, "STATS") {
        o = reply.put(sc, o, "# Stats\r\n");
        o = kv(sc, o, "total_connections_received", world[3]);
        o = kv(sc, o, "total_commands_processed", world[2]);
        o = kv(sc, o, "expired_keys", store.expired(st));
        o = kv(sc, o, "evicted_keys", store.evicted(st));
        o = kv(sc, o, "compactions", store.compactions(st));
        o = kv(sc, o, "compactions_forced", store.forced(st));
        o = kv(sc, o, "max_turn_ms", world[6]);
        o = reply.put(sc, o, "\r\n");
    }
    if wants(view, table, "KEYSPACE") {
        o = reply.put(sc, o, "# Keyspace\r\n");
        if store.live(st) > 0 {
            o = reply.put(sc, o, "db0:keys=");
            o = reply.put_int(sc, o, store.live(st));
            o = reply.put(sc, o, ",expires=");
            o = reply.put_int(sc, o, store.keys_with_expiry(st));
            o = reply.put(sc, o, ",avg_ttl=0\r\n");
        }
        o = reply.put(sc, o, "\r\n");
    }
    return o;
}

// `INFO [section]` as one bulk string. The text is written first, a few bytes in, and the header (which needs the length) is written
// against it and the whole answer moved down to where it belongs.
pub fn info[&v, &t, &b, &s, &w](view: &v [byte], table: &t [int], sc: &!b [byte], at: int, st: &s store.Store, world: &w [int]) -> [] int {
    if table[0] > 2 {
        return reply.syntax_error(sc, at);
    }
    let room = 24;
    let body_from = at + room;
    let body_to = info_text(view, table, sc, body_from, st, world);
    let size = body_to - body_from;
    var digits = 1;
    var rest = size / 10;
    while rest > 0 {
        digits = digits + 1;
        rest = rest / 10;
    }
    let header = 1 + digits + 2;
    var o = reply.put(sc, body_from - header, "$");
    o = reply.put_nat(sc, o, size);
    reply.put(sc, o, "\r\n");
    reply.put(sc, body_to, "\r\n");
    copy_within(sc, at, body_from - header, header + size + 2);
    return at + header + size + 2;
}

// ---------------------------------------------------------------------
// CONFIG and COMMAND
// ---------------------------------------------------------------------

// Does `pattern` (a glob: `*` alone, a prefix and `*`, or a name) match `name`, whatever the case?
fn glob[&p](pattern: &p [byte], name: &static [byte]) -> [] bool {
    if len(pattern) == 1 && int_of(pattern[0]) == '*' {
        return true;
    }
    if len(pattern) > 0 && int_of(pattern[len(pattern) - 1]) == '*' {
        let prefix = pattern[0..len(pattern) - 1];
        if len(prefix) > len(name) {
            return false;
        }
        return reply.is_prefix_word(prefix, name);
    }
    return len(pattern) == len(name) && reply.is_prefix_word(pattern, name);
}

// The settings `CONFIG GET` knows, by number.
fn setting_name(i: int) -> [] &static [byte] {
    if i == 0 {
        return "maxmemory";
    }
    if i == 1 {
        return "maxmemory-policy";
    }
    if i == 2 {
        return "save";
    }
    if i == 3 {
        return "appendonly";
    }
    if i == 4 {
        return "databases";
    }
    if i == 5 {
        return "port";
    }
    if i == 6 {
        return "timeout";
    }
    if i == 7 {
        return "maxclients";
    }
    if i == 8 {
        return "hz";
    }
    return "requirepass";
}

// 2^i, for i below 10: the mark a setting has been answered.
fn bit(i: int) -> [] int {
    var b = 1;
    var k = 0;
    while k < i {
        b = b * 2;
        k = k + 1;
    }
    return b;
}

fn settings() -> [] int {
    return 10;
}

// `$<digits>\r\n<number>\r\n`: a number as a bulk string, which is how `CONFIG GET` answers one.
fn put_number_bulk[&b](sc: &!b [byte], at: int, number: int) -> [] int {
    var digits = 1;
    var rest = number / 10;
    while rest > 0 {
        digits = digits + 1;
        rest = rest / 10;
    }
    let o = reply.put(sc, reply.put_nat(sc, reply.put(sc, at, "$"), digits), "\r\n");
    return reply.put(sc, reply.put_nat(sc, o, number), "\r\n");
}

// The value of setting `i`, as a bulk string.
fn put_setting_value[&b, &s, &w](sc: &!b [byte], at: int, i: int, st: &s store.Store, world: &w [int]) -> [] int {
    if i == 0 {
        return put_number_bulk(sc, at, store.capacity(st));
    }
    if i == 1 {
        if store.policy_of(st) == store.evict_lru() {
            return reply.put_bulk(sc, at, "allkeys-lru");
        }
        return reply.put_bulk(sc, at, "noeviction");
    }
    if i == 2 {
        return reply.put_bulk(sc, at, "");
    }
    if i == 3 {
        return reply.put_bulk(sc, at, "no");
    }
    if i == 4 {
        return put_number_bulk(sc, at, 1);
    }
    if i == 5 {
        return put_number_bulk(sc, at, world[4]);
    }
    if i == 6 {
        return put_number_bulk(sc, at, 0);
    }
    if i == 7 {
        return put_number_bulk(sc, at, 256);
    }
    if i == 8 {
        return put_number_bulk(sc, at, 10);
    }
    return reply.put_bulk(sc, at, "");
}

fn has_glob[&p](pattern: &p [byte]) -> [] bool {
    var i = 0;
    while i < len(pattern) {
        let c = int_of(pattern[i]);
        if c == '*' || c == '?' || c == '[' {
            return true;
        }
        i = i + 1;
    }
    return false;
}

// `CONFIG GET pattern...` for the settings that mean something here, in Redis's way: a pattern with no wildcard names one setting and
// is echoed as it was typed; a pattern with one matches settings, which are named as they are; a setting is answered once. `CONFIG SET`
// is refused (the arena, key table and policy are fixed at start), `CONFIG RESETSTAT` is accepted and does nothing. The pairs are
// written a header's room in and counted; the array header (which needs the count) is written against them and the answer moved down.
pub fn config[&v, &t, &b, &s, &w](view: &v [byte], table: &t [int], sc: &!b [byte], at: int, st: &s store.Store, world: &w [int], proto: int) -> [] int {
    let argc = table[0];
    if argc == 1 {
        return reply.wrong_arguments(sc, at, reply.arg_of(view, table, 0));
    }
    let sub = reply.arg_of(view, table, 1);
    if reply.is_word(sub, "GET") {
        if argc < 3 {
            return reply.put(sc, at, "-ERR wrong number of arguments for 'config|get' command\r\n");
        }
        let room = 16;
        var o = at + room;
        var pairs = 0;
        var done = 0;
        var k = 2;
        while k < argc {
            let pattern = reply.arg_of(view, table, k);
            var i = 0;
            while i < settings() {
                let seen = done / bit(i) % 2 == 1;
                if !seen {
                    if has_glob(pattern) {
                        if glob(pattern, setting_name(i)) {
                            o = put_setting_value(sc, reply.put_bulk(sc, o, setting_name(i)), i, st, world);
                            done = done + bit(i);
                            pairs = pairs + 1;
                        }
                    } else if len(pattern) == len(setting_name(i)) && reply.is_prefix_word(pattern, setting_name(i)) {
                        o = put_setting_value(sc, reply.put_bulk(sc, o, pattern), i, st, world);
                        done = done + bit(i);
                        pairs = pairs + 1;
                    }
                }
                i = i + 1;
            }
            k = k + 1;
        }
        let size = o - (at + room);
        // RESP2 answers a flat array of 2n, RESP3 a map of n.
        var count = 2 * pairs;
        var lead = "*";
        if proto == 3 {
            count = pairs;
            lead = "%";
        }
        var digits = 1;
        var rest = count / 10;
        while rest > 0 {
            digits = digits + 1;
            rest = rest / 10;
        }
        let header = 1 + digits + 2;
        reply.put(sc, reply.put_nat(sc, reply.put(sc, at + room - header, lead), count), "\r\n");
        copy_within(sc, at, at + room - header, header + size);
        return at + header + size;
    }
    if reply.is_word(sub, "SET") {
        return reply.put(sc, at, "-ERR CONFIG SET is not supported: this server's settings are fixed when it starts\r\n");
    }
    if reply.is_word(sub, "RESETSTAT") {
        return reply.put(sc, at, "+OK\r\n");
    }
    var shown = len(sub);
    if shown > 128 {
        shown = 128;
    }
    var o = reply.put(sc, at, "-ERR unknown subcommand '");
    o = reply.put(sc, o, sub[0..shown]);
    return reply.put(sc, o, "'. Try CONFIG HELP.\r\n");
}

// The commands this server answers, as `COMMAND LIST` gives them (lower case, one bulk string each): 38 of them.
fn put_command_names[&b](sc: &!b [byte], at: int) -> [] int {
    var o = reply.put_bulk(sc, at, "get");
    o = reply.put_bulk(sc, o, "set");
    o = reply.put_bulk(sc, o, "ping");
    o = reply.put_bulk(sc, o, "echo");
    o = reply.put_bulk(sc, o, "del");
    o = reply.put_bulk(sc, o, "unlink");
    o = reply.put_bulk(sc, o, "exists");
    o = reply.put_bulk(sc, o, "touch");
    o = reply.put_bulk(sc, o, "incr");
    o = reply.put_bulk(sc, o, "decr");
    o = reply.put_bulk(sc, o, "incrby");
    o = reply.put_bulk(sc, o, "decrby");
    o = reply.put_bulk(sc, o, "expire");
    o = reply.put_bulk(sc, o, "pexpire");
    o = reply.put_bulk(sc, o, "ttl");
    o = reply.put_bulk(sc, o, "pttl");
    o = reply.put_bulk(sc, o, "persist");
    o = reply.put_bulk(sc, o, "setnx");
    o = reply.put_bulk(sc, o, "setex");
    o = reply.put_bulk(sc, o, "psetex");
    o = reply.put_bulk(sc, o, "getset");
    o = reply.put_bulk(sc, o, "getdel");
    o = reply.put_bulk(sc, o, "mget");
    o = reply.put_bulk(sc, o, "mset");
    o = reply.put_bulk(sc, o, "strlen");
    o = reply.put_bulk(sc, o, "type");
    o = reply.put_bulk(sc, o, "dbsize");
    o = reply.put_bulk(sc, o, "flushall");
    o = reply.put_bulk(sc, o, "flushdb");
    o = reply.put_bulk(sc, o, "select");
    o = reply.put_bulk(sc, o, "hello");
    o = reply.put_bulk(sc, o, "auth");
    o = reply.put_bulk(sc, o, "client");
    o = reply.put_bulk(sc, o, "info");
    o = reply.put_bulk(sc, o, "config");
    o = reply.put_bulk(sc, o, "command");
    o = reply.put_bulk(sc, o, "quit");
    return reply.put_bulk(sc, o, "reset");
}

// `COMMAND`, `COMMAND COUNT`, `COMMAND LIST`. The full table Redis answers `COMMAND` with (flags, arity, key positions) is not kept:
// clients that read it get an empty one, and `COMMAND DOCS` and `COMMAND INFO` the same.
pub fn command[&v, &t, &b](view: &v [byte], table: &t [int], sc: &!b [byte], at: int) -> [] int {
    let argc = table[0];
    if argc == 1 {
        return reply.put(sc, at, "*0\r\n");
    }
    let sub = reply.arg_of(view, table, 1);
    if reply.is_word(sub, "COUNT") && argc == 2 {
        return reply.put_integer(sc, at, supported());
    }
    if reply.is_word(sub, "LIST") && argc == 2 {
        return put_command_names(sc, reply.put(sc, reply.put_nat(sc, reply.put(sc, at, "*"), supported()), "\r\n"));
    }
    if reply.is_word(sub, "DOCS") || reply.is_word(sub, "INFO") {
        return reply.put(sc, at, "*0\r\n");
    }
    var shown = len(sub);
    if shown > 128 {
        shown = 128;
    }
    var o = reply.put(sc, at, "-ERR unknown subcommand '");
    o = reply.put(sc, o, sub[0..shown]);
    return reply.put(sc, o, "'. Try COMMAND HELP.\r\n");
}
