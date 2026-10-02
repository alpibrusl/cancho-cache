edition 5;

import std.buffer;
import std.conns;
import std.io;
import commands;
import resp;
import store;

// `cache` -- a Redis-compatible cache (`docs/design.md`): the loop. The commands are `src/commands.ls`, the memory is
// `src/store.ls`, the protocol `src/resp.ls`.
//
//     cache <port> [<arena MiB> [<most keys> [noeviction | allkeys-lru]]]     (64 MiB, 1,000,000 keys, noeviction)
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
    // A few bytes for a command to build a value in (`INCR`).
    tmp: Box[[byte]],
    store: store.Store,
    // When the sweep for expired keys last ran, in the store's milliseconds.
    swept: int,
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
                    at = commands.put(sc, at, "-ERR Protocol error: command too large\r\n");
                    st[p + 3] = 1;
                }
                going = false;
            } else if n < 0 {
                at = commands.put(sc, at, "-ERR ");
                at = commands.put(sc, at, resp.error_text(n));
                if n == resp.error_expected_bulk() {
                    at = commands.put_byte(sc, at, tb[0]);
                    at = commands.put(sc, at, "'");
                }
                at = commands.put(sc, at, "\r\n");
                st[p + 3] = 1;
                going = false;
            } else {
                at = commands.execute(view, tb, sc, at, core.store, contents(core.tmp));
                used = used + n;
                // Room for the biggest answer one command can make.
                if at + commands.reserve() > len(sc) {
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

fn run[&h, &l, &k](heap: &!h Heap, listener: &!l Listener, clock: &k Clock, memory: int, max_keys: int, policy: int) -> [heap, conn_accept, conn_read, conn_write, poll, clock] int {
    match poller_new() {
        Polling::Ok(p) => {
            var poller = p;
            borrow mut poller as &!pw in {
                poller_add_listener(pw, listener, 0);
            }
            let limit = max_connections();
            var core = Core { poller: poller, events: box_slice(heap, 128, 0), state: box_slice(heap, stride() * limit, 0), bufs: box_slice(heap, limit * input_size(), byte_of(0)), pends: box_slice(heap, limit * output_size(), byte_of(0)), table: box_slice(heap, resp.slots(), 0), scratch: box_slice(heap, 3 * commands.reserve(), byte_of(0)), tmp: box_slice(heap, 64, byte_of(0)), store: store.open(heap, memory, max_keys, 0, policy), swept: 0 };
            var tab = conns.empty(heap, 64);
            while true {
                var ready = 0 - 1;
                borrow mut core as &!cw in {
                    // Wake often enough to take keys whose time has come, but only if there are any.
                    var wait_ms = 1000;
                    if store.keys_with_expiry(cw.store) > 0 {
                        wait_ms = 100;
                    }
                    ready = poller_wait(cw.poller, contents(cw.events), wait_ms);
                    // The time, once for this turn: every use of a key in it is stamped with it.
                    store.tick(cw.store, clock_ms(clock));
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
                borrow mut core as &!cw in {
                    // Take the keys whose time has passed that nobody asked for again: at most a hundred times a second,
                    // 256 keys at a look, and again while more than a quarter of what it looked at had run out (Redis's
                    // rule), at most sixteen looks.
                    if store.now_of(cw.store) - cw.swept >= 10 {
                        cw.swept = store.now_of(cw.store);
                        var looks = 0;
                        var more = true;
                        while more && looks < 16 {
                            more = store.sweep(cw.store, 256) * 4 > 256;
                            looks = looks + 1;
                        }
                    }
                }
            }
            conns.drop(heap, tab);
            let Core { poller, events, state, bufs, pends, table, scratch, tmp, store, swept } = core;
            poller_close(poller);
            unbox_slice(heap, events);
            unbox_slice(heap, state);
            unbox_slice(heap, bufs);
            unbox_slice(heap, pends);
            unbox_slice(heap, table);
            unbox_slice(heap, scratch);
            unbox_slice(heap, tmp);
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
    var port = 0 - 1;
    borrow args as &g in {
        if arg_count(g) > 1 {
            port = number_of(arg(g, 1));
        }
    }
    var megabytes = 64;
    var max_keys = 1000000;
    var policy = store.evict_none();
    borrow args as &g in {
        if arg_count(g) > 4 {
            if arg(g, 4)[0] == byte_of('a') {
                policy = store.evict_lru();
            }
        }
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
                            borrow clock as &c in {
                                status = run(h, lh, c, megabytes * 1048576, max_keys, policy);
                            }
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
    release(clock);
    release(args);
    release(io);
    release(heap);
    return status;
}
