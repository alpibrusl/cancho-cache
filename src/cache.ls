edition 5;

import std.buffer;
import std.conns;
import std.io;
import resp;
import store;

// `cache` -- a Redis-compatible cache (`docs/design.md`). Step C1: `PING`, `ECHO`, `GET`, `SET`, `DEL` and `EXISTS` over
// the store of `src/store.ls`, and everything else refused the way Redis refuses it.
//
//     cache <port> [<arena MiB> [<most keys>]]          (64 MiB and 1,000,000 keys if not given)
//
// One thread, one poller. A readable connection is read once into its own slot of one slab, every whole command
// in it is parsed and answered into one scratch buffer, and the scratch goes out in a single write: so a client
// that pipelines sixteen commands costs one read and one write, which is what Redis does and what the pipeline
// cells of the gate measure. What the kernel will not take is queued per connection, the connection is then
// watched for room to write instead of for input (so a client that does not read stops being read from), and
// whatever input was already buffered is answered once the queue has drained.
//
// Sizes are fixed at start and nothing is allocated afterwards.

// Connections at once, at most.
fn max_connections() -> [] int {
    return 256;
}

// Each connection's input buffer: a command must fit in it.
fn input_size() -> [] int {
    return 16384;
}

// What one connection may have waiting to be sent.
fn output_size() -> [] int {
    return 65536;
}

// Per connection `k`, `state[8k..8k+8]` is:
//
//     0  bytes of input buffered
//     1  1 if the slot is in use
//     2  bytes of output waiting (-1: the queue could not hold it)
//     3  1 if the connection closes once that output has gone
//     4  what it is watched for (1 to read, 2 to write)
fn stride() -> [] int {
    return 8;
}

res struct Core {
    poller: Poller,
    // `poller_wait` fills these with (token, readiness) pairs.
    events: Box[[int]],
    state: Box[[int]],
    bufs: Box[[byte]],
    pends: Box[[byte]],
    // The parse table of the command in hand.
    table: Box[[int]],
    // Every answer to one read, before it is written.
    scratch: Box[[byte]],
    store: store.Store,
}

// ---------------------------------------------------------------------
// Answers, written into the scratch buffer
// ---------------------------------------------------------------------

fn put[&b, &d](sc: &!b [byte], at: int, data: &d [byte]) -> [] int {
    var i = 0;
    while i < len(data) {
        sc[at + i] = data[i];
        i = i + 1;
    }
    return at + len(data);
}

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

// `$<length>\r\n<data>\r\n`
fn put_bulk[&b, &d](sc: &!b [byte], at: int, data: &d [byte]) -> [] int {
    var o = put(sc, at, "$");
    o = put_nat(sc, o, len(data));
    o = put(sc, o, "\r\n");
    o = put(sc, o, data);
    return put(sc, o, "\r\n");
}

// An ASCII letter in upper case, anything else as it is.
fn upper(c: int) -> [] int {
    if c >= 'a' && c <= 'z' {
        return c - 32;
    }
    return c;
}

// Is `word` the command `name` (which is written in upper case), whatever case it came in?
fn is_command[&w](word: &w [byte], name: &static [byte]) -> [] bool {
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

fn put_byte[&b](sc: &!b [byte], at: int, c: int) -> [] int {
    sc[at] = byte_of(c);
    return at + 1;
}

fn lower(c: int) -> [] int {
    if c >= 'A' && c <= 'Z' {
        return c + 32;
    }
    return c;
}

// `:<n>\r\n`
fn put_integer[&b](sc: &!b [byte], at: int, n: int) -> [] int {
    return put(sc, put_nat(sc, put(sc, at, ":"), n), "\r\n");
}

// The answer to a `set` that was refused: the arena is full, or there is no room for another key. (Redis's text for
// the same thing under its default `noeviction` policy.)
fn refused[&b](sc: &!b [byte], at: int) -> [] int {
    return put(sc, at, "-OOM command not allowed when used memory > 'maxmemory'.\r\n");
}

// Answer the command `table` describes in `view`, into `sc` from `at`. Answers where the answer ends.
fn execute[&v, &t, &b, &s](view: &v [byte], table: &t [int], sc: &!b [byte], at: int, st: &!s store.Store) -> [] int {
    let argc = table[0];
    if argc == 0 {
        return at;
    }
    let name = view[table[1]..table[1] + table[2]];
    if is_command(name, "PING") {
        if argc == 1 {
            return put(sc, at, "+PONG\r\n");
        }
        if argc == 2 {
            return put_bulk(sc, at, view[table[3]..table[3] + table[4]]);
        }
        return wrong_arguments(sc, at, name);
    }
    if is_command(name, "ECHO") {
        if argc == 2 {
            return put_bulk(sc, at, view[table[3]..table[3] + table[4]]);
        }
        return wrong_arguments(sc, at, name);
    }
    if is_command(name, "GET") {
        if argc != 2 {
            return wrong_arguments(sc, at, name);
        }
        let key = view[table[3]..table[3] + table[4]];
        let e = store.find(st, key, store.hash_of(st, key));
        if e < 0 {
            return put(sc, at, "$-1\r\n");
        }
        return put_bulk(sc, at, store.value(st, e));
    }
    if is_command(name, "SET") {
        if argc < 3 {
            return wrong_arguments(sc, at, name);
        }
        // Options (`EX`, `NX`, ...) come with expiry, step C2.
        if argc > 3 {
            return put(sc, at, "-ERR syntax error\r\n");
        }
        let key = view[table[3]..table[3] + table[4]];
        if store.set(st, key, store.hash_of(st, key), view[table[5]..table[5] + table[6]]) != 0 {
            return refused(sc, at);
        }
        return put(sc, at, "+OK\r\n");
    }
    if is_command(name, "DEL") {
        if argc < 2 {
            return wrong_arguments(sc, at, name);
        }
        var removed = 0;
        var k = 1;
        while k < argc {
            let key = view[table[1 + 2 * k]..table[1 + 2 * k] + table[2 + 2 * k]];
            removed = removed + store.delete(st, key, store.hash_of(st, key));
            k = k + 1;
        }
        return put_integer(sc, at, removed);
    }
    if is_command(name, "EXISTS") {
        if argc < 2 {
            return wrong_arguments(sc, at, name);
        }
        var found = 0;
        var k = 1;
        while k < argc {
            let key = view[table[1 + 2 * k]..table[1 + 2 * k] + table[2 + 2 * k]];
            if store.find(st, key, store.hash_of(st, key)) >= 0 {
                found = found + 1;
            }
            k = k + 1;
        }
        return put_integer(sc, at, found);
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
        o = put(sc, o, view[table[1 + 2 * k]..table[1 + 2 * k] + take]);
        o = put(sc, o, "' ");
        spent = spent + take + 3;
        k = k + 1;
    }
    return put(sc, o, "\r\n");
}

// ---------------------------------------------------------------------
// One connection's bytes
// ---------------------------------------------------------------------

// Hand `data` to the kernel without waiting for room, and keep what it did not take.
//
// `pend[0..pending]` is what this connection already has waiting. If there is none, `data` is offered to the
// connection straight away; if there is some, the new bytes queue behind it or the answers would arrive out
// of order. Answers the new `pending`, or -1 if the queue cannot hold the rest (a client that is not reading and
// has asked for more than the buffer) or the connection has failed: the caller closes it.
fn emit[&c, &d, &e](table: &!c conns.Table, slot: int, data: &d [byte], pend: &!e [byte], pending: int) -> [conn_write] int {
    var at = 0;
    if pending == 0 {
        match conns.write(table, slot, data) {
            Sent::Wrote(n) => {
                at = n;
            }
            Sent::Again => {
            }
            Sent::Failed(e) => {
                return 0 - 1;
            }
        }
    }
    if at >= len(data) {
        return pending;
    }
    if pending + len(data) - at > len(pend) {
        return 0 - 1;
    }
    var i = at;
    while i < len(data) {
        pend[pending + i - at] = data[i];
        i = i + 1;
    }
    return pending + len(data) - at;
}

fn shut[&t, &c](tab: &!t conns.Table, core: &!c Core, k: int) -> [] int {
    let st = contents(core.state);
    conns.close(tab, k);
    st[stride() * k + 1] = 0;
    return 0;
}

// Watch `k` for what it now waits on: room to write if output is queued (and read no more), input otherwise.
fn settle[&t, &c](tab: &!t conns.Table, core: &!c Core, k: int) -> [poll] int {
    let st = contents(core.state);
    let p = stride() * k;
    var want = 1;
    if st[p + 2] > 0 {
        want = 2;
    }
    if want != st[p + 4] {
        conns.rewatch(tab, core.poller, k, k + 1, want);
        st[p + 4] = want;
    }
    return 0;
}

// Answer every whole command buffered for connection `k`, write the answers, and keep the start of one that
// is still arriving. Closes the connection if it is to close, or cannot be written to.
fn process[&t, &c](tab: &!t conns.Table, core: &!c Core, k: int) -> [conn_write, poll] int {
    let st = contents(core.state);
    let bf = contents(core.bufs);
    let pd = contents(core.pends);
    let sc = contents(core.scratch);
    let tb = contents(core.table);
    let size = input_size();
    let p = stride() * k;
    let base = k * size;
    let obase = k * output_size();
    var used = 0;
    var at = 0;
    var going = true;
    while going {
        if st[p + 2] != 0 || st[p + 3] != 0 || used >= st[p] {
            going = false;
        } else {
            let view = bf[base + used..base + st[p]];
            let n = resp.parse(view, tb);
            if n == 0 {
                if used == 0 && st[p] >= size {
                    at = put(sc, at, "-ERR Protocol error: command too large\r\n");
                    st[p + 3] = 1;
                }
                going = false;
            } else if n < 0 {
                at = put(sc, at, "-ERR ");
                at = put(sc, at, resp.error_text(n));
                if n == resp.error_expected_bulk() {
                    at = put_byte(sc, at, tb[0]);
                    at = put(sc, at, "'");
                }
                at = put(sc, at, "\r\n");
                st[p + 3] = 1;
                going = false;
            } else {
                at = execute(view, tb, sc, at, core.store);
                used = used + n;
                // Room for the biggest answer one command can make.
                if at + size + 256 > len(sc) {
                    st[p + 2] = emit(tab, k, sc[0..at], pd[obase..obase + output_size()], st[p + 2]);
                    at = 0;
                }
            }
        }
    }
    if at > 0 && st[p + 2] >= 0 {
        st[p + 2] = emit(tab, k, sc[0..at], pd[obase..obase + output_size()], st[p + 2]);
    }
    if st[p + 2] < 0 {
        shut(tab, core, k);
        return 0;
    }
    var at_front = 0;
    while at_front < st[p] - used {
        bf[base + at_front] = bf[base + used + at_front];
        at_front = at_front + 1;
    }
    st[p] = st[p] - used;
    if st[p + 3] == 1 && st[p + 2] == 0 {
        shut(tab, core, k);
    } else {
        settle(tab, core, k);
    }
    return 0;
}

// The poller said connection `k` is ready.
fn step[&t, &c](tab: &!t conns.Table, core: &!c Core, k: int, readiness: int) -> [conn_read, conn_write, poll] int {
    let st = contents(core.state);
    let bf = contents(core.bufs);
    let pd = contents(core.pends);
    let size = input_size();
    let p = stride() * k;
    let base = k * size;
    let obase = k * output_size();
    if st[p + 2] > 0 {
        // Waiting to send: the kernel can take more.
        if readiness % 4 >= 2 {
            match conns.write(tab, k, pd[obase..obase + st[p + 2]]) {
                Sent::Wrote(sent) => {
                    var at = 0;
                    while at < st[p + 2] - sent {
                        pd[obase + at] = pd[obase + sent + at];
                        at = at + 1;
                    }
                    st[p + 2] = st[p + 2] - sent;
                    if st[p + 2] == 0 {
                        if st[p + 3] == 1 {
                            shut(tab, core, k);
                        } else {
                            // Held back while the last answers were being sent.
                            process(tab, core, k);
                        }
                    }
                }
                Sent::Again => {
                }
                Sent::Failed(e) => {
                    shut(tab, core, k);
                }
            }
        } else if readiness % 2 == 1 {
            // The peer has gone, or said something while we are not listening.
            shut(tab, core, k);
        }
    } else if st[p] >= size {
        // No room to read into and nothing to send: a command that can never fit was answered already.
        shut(tab, core, k);
    } else {
        match conns.read(tab, k, bf[base + st[p]..base + size]) {
            Received::Data(got) => {
                st[p] = st[p] + got;
                process(tab, core, k);
            }
            Received::End => {
                shut(tab, core, k);
            }
            Received::Again => {
            }
            Received::Failed(e) => {
                shut(tab, core, k);
            }
        }
    }
    return 0;
}

// Take every connection waiting on the listener, up to the limit: each goes in the table, is made
// non-blocking, and is watched for input under the token `slot + 1` (the listener is token 0).
fn accept_all[&h, &l, &c](heap: &!h Heap, conn: conns.Table, listener: &!l Listener, core: &!c Core) -> [heap, conn_accept, poll] conns.Table {
    let st = contents(core.state);
    var table = conn;
    var more = true;
    while more {
        match tcp_accept(listener) {
            Accepted::Ok(c) => {
                var held = 0;
                borrow table as &tt in {
                    held = conns.live(tt);
                }
                if held >= max_connections() {
                    conn_close(c);
                } else {
                    let (grown, slot) = conns.put(heap, table, c);
                    table = grown;
                    if slot >= 0 {
                        let p = stride() * slot;
                        st[p] = 0;
                        st[p + 1] = 1;
                        st[p + 2] = 0;
                        st[p + 3] = 0;
                        st[p + 4] = 1;
                        borrow mut table as &!ct in {
                            if conns.nonblocking(ct, slot) != 0 || conns.watch(ct, core.poller, slot, slot + 1, 1) != 0 {
                                shut(ct, core, slot);
                            }
                        }
                    }
                }
            }
            Accepted::Again => {
                more = false;
            }
            Accepted::Failed(e) => {
                more = false;
            }
        }
    }
    return table;
}

// ---------------------------------------------------------------------
// The loop
// ---------------------------------------------------------------------

fn run[&h, &l](heap: &!h Heap, listener: &!l Listener, memory: int, max_keys: int) -> [heap, conn_accept, conn_read, conn_write, poll] int {
    match poller_new() {
        Polling::Ok(p) => {
            var poller = p;
            borrow mut poller as &!pw in {
                poller_add_listener(pw, listener, 0);
            }
            let limit = max_connections();
            var core = Core { poller: poller, events: box_slice(heap, 128, 0), state: box_slice(heap, stride() * limit, 0), bufs: box_slice(heap, limit * input_size(), byte_of(0)), pends: box_slice(heap, limit * output_size(), byte_of(0)), table: box_slice(heap, resp.slots(), 0), scratch: box_slice(heap, 4 * input_size(), byte_of(0)), store: store.open(heap, memory, max_keys, 0) };
            var tab = conns.empty(heap, 64);
            while true {
                var ready = 0 - 1;
                borrow mut core as &!cw in {
                    ready = poller_wait(cw.poller, contents(cw.events), 1000);
                }
                var j = 0;
                while j < ready {
                    var token = 0 - 1;
                    var readiness = 0;
                    borrow core as &cr in {
                        token = contents(cr.events)[2 * j];
                        readiness = contents(cr.events)[2 * j + 1];
                    }
                    if token == 0 {
                        borrow mut core as &!cw in {
                            tab = accept_all(heap, tab, listener, cw);
                        }
                    } else {
                        borrow mut tab as &!tw in {
                            borrow mut core as &!cw in {
                                if contents(cw.state)[stride() * (token - 1) + 1] == 1 {
                                    step(tw, cw, token - 1, readiness);
                                }
                            }
                        }
                    }
                    j = j + 1;
                }
            }
            conns.drop(heap, tab);
            let Core { poller, events, state, bufs, pends, table, scratch, store } = core;
            poller_close(poller);
            unbox_slice(heap, events);
            unbox_slice(heap, state);
            unbox_slice(heap, bufs);
            unbox_slice(heap, pends);
            unbox_slice(heap, table);
            unbox_slice(heap, scratch);
            store.close(heap, store);
            return 0;
        }
        Polling::Failed(e) => {
            return 4;
        }
    }
}

fn number_of[&t](text: &t [byte]) -> [] int {
    if len(text) == 0 || len(text) > 9 {
        return 0 - 1;
    }
    var n = 0;
    var i = 0;
    while i < len(text) {
        let c = int_of(text[i]);
        if c < '0' || c > '9' {
            return 0 - 1;
        }
        n = n * 10 + (c - '0');
        i = i + 1;
    }
    return n;
}

fn main(world: World) -> [] int {
    let Split { io, ffi, fs, heap, args, net, clock } = split(world);
    release(fs);
    release(ffi);
    release(clock);
    var port = 0 - 1;
    borrow args as &g in {
        if arg_count(g) > 1 {
            port = number_of(arg(g, 1));
        }
    }
    var megabytes = 64;
    var max_keys = 1000000;
    borrow args as &g in {
        if arg_count(g) > 2 {
            megabytes = number_of(arg(g, 2));
        }
        if arg_count(g) > 3 {
            max_keys = number_of(arg(g, 3));
        }
    }
    var status = 2;
    if port > 0 && port < 65536 && megabytes > 0 && megabytes <= 4096 && max_keys > 0 && max_keys <= 100000000 {
        status = 3;
        borrow net as &nn in {
            match tcp_listen(nn, port, 1024, 0) {
                Listening::Ok(l) => {
                    var listener = l;
                    borrow mut listener as &!lh in {
                        listener_nonblocking(lh);
                        borrow mut heap as &!h in {
                            borrow mut io as &!i in {
                                var line = buffer.append(h, buffer.empty(h, 64), "listening on ");
                                line = buffer.push_nat(h, line, port);
                                line = buffer.push(h, line, byte_of(10));
                                borrow line as &lb in {
                                    io.error_all(i, buffer.bytes(lb));
                                }
                                buffer.drop(h, line);
                            }
                            status = run(h, lh, megabytes * 1048576, max_keys);
                        }
                    }
                    listener_close(listener);
                }
                Listening::Failed(e) => {
                }
            }
        }
    }
    release(net);
    release(args);
    release(io);
    release(heap);
    return status;
}
