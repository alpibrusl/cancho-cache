edition 5;

module store;

import std.map;

// `store` -- the cache's memory: keys and values in one arena, found through one index (`docs/design.md` §5, §10).
//
// Nothing is allocated after `open`. There are three fixed-size pieces:
//
//   * `data`, the arena, a log of **records** written from the front. A record is an 8-byte header (the entry number,
//     and the size of what follows) and then the key and its value's room. Overwriting a value that fits the room it
//     has is done in place; one that does not is appended anew and the old record becomes garbage (`dead`). The header
//     is what lets the arena be walked from the front, which is what compaction does.
//   * `meta`, eight ints a key, indexed by an *entry number*: 0 where its key starts (after the header), 1 the key's
//     length, 2 the value's length, 3 the value's room, 4 its expiry in milliseconds (0: none), 5 its full hash,
//     6 whether it is in use (-2) or, if it is free, the next free entry (-1: none), 7 when it was last used.
//     Entries are numbered from 0 as they are first needed, and a deleted key's number is reused.
//   * `index`, open addressing with linear probing and **backward-shift deletion** (no tombstones, so a cache that
//     deletes forever does not get slower): a slot is 0 for empty, or the top bits of the key's hash above the entry
//     number plus one. Comparing the 31-bit tag first means a probe past another key's slot rarely touches `meta`.
//     It has at least twice as many slots as there can be keys, so it is never more than half full.
//
// When the arena is full, `set` first **compacts**: it walks the records from the front, keeps those whose entry still
// points at them, and slides them down over the garbage, in place and with no second copy of the data. If that is not
// enough and the policy is `evict_lru`, it evicts keys until a sixteenth of the arena is reclaimable and compacts once,
// so that the cost of a compaction is paid once per sixteenth of the arena written, not once per write. An eviction
// picks five keys at random and removes the one used longest ago (Redis's approximate LRU, which is also why no
// ordering structure is kept). With `evict_none` it refuses instead (1), like Redis's default `noeviction`.
//
// Time is the caller's: `tick` says what it is, once per turn of the loop, and every use of a key stamps that time.

pub fn evict_none() -> [] int {
    return 0;
}

pub fn evict_lru() -> [] int {
    return 1;
}

// Ints a key takes in `meta`.
fn stride() -> [] int {
    return 8;
}

// Bytes of header before each record's key.
fn header() -> [] int {
    return 8;
}

pub res struct Store {
    index: Box[[int]],
    meta: Box[[int]],
    data: Box[[byte]],
    mask: int,
    max_keys: int,
    live: int,
    // Entry numbers handed out so far; every one below this is in use or on the free chain.
    fresh: int,
    free_head: int,
    top: int,
    dead: int,
    seed: int,
    policy: int,
    now: int,
    // The next entry `sweep` looks at, and the state of the generator that picks eviction candidates.
    cursor: int,
    rng: int,
    // The entry a `set` is growing, which an eviction must not take.
    protect: int,
    // How many keys have an expiry, and counts for `INFO`.
    ttl_keys: int,
    evicted: int,
    expired: int,
    compactions: int,
}

// A store with an arena of `memory` bytes and room for `max_keys` keys. `seed` is mixed into every hash; it is a
// constant until the cache reads one at start (a hostile client that can choose keys can make a known seed
// collide).
pub fn open[&h](heap: &!h Heap, memory: int, max_keys: int, seed: int, policy: int) -> [heap] Store {
    var slots = 8;
    while slots < 2 * max_keys {
        slots = slots * 2;
    }
    return Store { index: box_slice(heap, slots, 0), meta: box_slice(heap, stride() * max_keys, 0), data: box_slice(heap, memory, byte_of(0)), mask: slots - 1, max_keys: max_keys, live: 0, fresh: 0, free_head: 0 - 1, top: 0, dead: 0, seed: seed, policy: policy, now: 0, cursor: 0, rng: 88172645463325252, protect: 0 - 1, ttl_keys: 0, evicted: 0, expired: 0, compactions: 0 };
}

pub fn close[&h](heap: &!h Heap, st: Store) -> [heap] int {
    let Store { index, meta, data, mask, max_keys, live, fresh, free_head, top, dead, seed, policy, now, cursor, rng, protect, ttl_keys, evicted, expired, compactions } = st;
    unbox_slice(heap, index);
    unbox_slice(heap, meta);
    unbox_slice(heap, data);
    return live;
}

// The hash `find`, `set` and `delete` want for `key`: computed once by the caller, because a command needs it
// for more than one of them.
pub fn hash_of[&s, &k](st: &s Store, key: &k [byte]) -> [] int {
    return map.hash(key, st.seed);
}

// Say what time it is, in milliseconds from any fixed origin that never goes backwards.
pub fn tick[&s](st: &!s Store, now: int) -> [] int {
    st.now = now;
    return 0;
}

pub fn live[&s](st: &s Store) -> [] int {
    return st.live;
}

// Bytes of the arena in use (records, dead or not), how many of them are garbage, and the size of the arena.
pub fn used[&s](st: &s Store) -> [] int {
    return st.top;
}

pub fn dead[&s](st: &s Store) -> [] int {
    return st.dead;
}

pub fn capacity[&s](st: &s Store) -> [] int {
    return len(contents(st.data));
}

pub fn keys_with_expiry[&s](st: &s Store) -> [] int {
    return st.ttl_keys;
}

pub fn evicted[&s](st: &s Store) -> [] int {
    return st.evicted;
}

pub fn expired[&s](st: &s Store) -> [] int {
    return st.expired;
}

pub fn compactions[&s](st: &s Store) -> [] int {
    return st.compactions;
}

fn tag_of(h: int) -> [] int {
    return h >> 33 & 0x7fffffff;
}

fn put32[&d](data: &!d [byte], at: int, v: int) -> [] int {
    data[at] = byte_of(v & 255);
    data[at + 1] = byte_of(v >> 8 & 255);
    data[at + 2] = byte_of(v >> 16 & 255);
    data[at + 3] = byte_of(v >> 24 & 255);
    return 0;
}

fn get32[&d](data: &d [byte], at: int) -> [] int {
    return int_of(data[at]) | int_of(data[at + 1]) << 8 | int_of(data[at + 2]) << 16 | int_of(data[at + 3]) << 24;
}

fn same[&d, &k](data: &d [byte], at: int, key: &k [byte]) -> [] bool {
    var i = 0;
    while i < len(key) {
        if data[at + i] != key[i] {
            return false;
        }
        i = i + 1;
    }
    return true;
}

// Room for a value of `n` bytes: `n` rounded up to a multiple of eight, so that a value that grows a little (a
// counter) usually still fits where it is.
fn room(n: int) -> [] int {
    return (n + 7) / 8 * 8;
}

fn copy[&d, &x](data: &!d [byte], at: int, src: &x [byte]) -> [] int {
    var i = 0;
    while i < len(src) {
        data[at + i] = src[i];
        i = i + 1;
    }
    return at + len(src);
}

// The index slot holding `key`, or -1.
fn find_slot[&s, &k](st: &s Store, key: &k [byte], h: int) -> [] int {
    let index = contents(st.index);
    let meta = contents(st.meta);
    let data = contents(st.data);
    let tag = tag_of(h);
    var i = h & st.mask;
    var found = 0 - 1;
    var going = true;
    while going {
        let slot = index[i];
        if slot == 0 {
            going = false;
        } else {
            if slot >> 32 == tag {
                let m = stride() * ((slot & 0xffffffff) - 1);
                if meta[m + 1] == len(key) && same(data, meta[m], key) {
                    found = i;
                    going = false;
                }
            }
            if going {
                i = i + 1 & st.mask;
            }
        }
    }
    return found;
}

// The index slot that holds entry `e`: it is there, because `e` is in use.
fn slot_of[&s](st: &s Store, e: int) -> [] int {
    let index = contents(st.index);
    var i = contents(st.meta)[stride() * e + 5] & st.mask;
    while index[i] & 0xffffffff != e + 1 {
        i = i + 1 & st.mask;
    }
    return i;
}

// Empty slot `start`, and move back any later entry of the run that is allowed to sit there, so that no entry is ever
// separated from its ideal slot by an empty one.
fn close_hole[&s](st: &!s Store, start: int) -> [] int {
    let index = contents(st.index);
    let meta = contents(st.meta);
    var hole = start;
    var j = start + 1 & st.mask;
    while index[j] != 0 {
        let ideal = meta[stride() * ((index[j] & 0xffffffff) - 1) + 5] & st.mask;
        // The entry at `j` may move to `hole` unless its ideal slot lies after `hole` and no later than `j`.
        if j - ideal & st.mask >= j - hole & st.mask {
            index[hole] = index[j];
            hole = j;
        }
        j = j + 1 & st.mask;
    }
    index[hole] = 0;
    return 0;
}

// Set entry `m`'s expiry to `at` (0: none) and keep the count of keys that have one.
fn put_expiry[&s](st: &!s Store, m: int, at: int) -> [] int {
    let meta = contents(st.meta);
    if meta[m + 4] == 0 && at != 0 {
        st.ttl_keys = st.ttl_keys + 1;
    }
    if meta[m + 4] != 0 && at == 0 {
        st.ttl_keys = st.ttl_keys - 1;
    }
    meta[m + 4] = at;
    return 0;
}

// Take entry `e` out: its record becomes garbage, its number goes on the free chain, its slot is closed up.
fn remove_entry[&s](st: &!s Store, e: int) -> [] int {
    let meta = contents(st.meta);
    let m = stride() * e;
    let slot = slot_of(st, e);
    st.dead = st.dead + header() + meta[m + 1] + meta[m + 3];
    put_expiry(st, m, 0);
    meta[m + 6] = st.free_head;
    st.free_head = e;
    st.live = st.live - 1;
    close_hole(st, slot);
    return 0;
}

// The entry holding `key`, or -1. A key whose time has passed is removed here and counts as absent; a key that is
// found is stamped as used now.
pub fn find[&s, &k](st: &!s Store, key: &k [byte], h: int) -> [] int {
    let slot = find_slot(st, key, h);
    if slot < 0 {
        return 0 - 1;
    }
    let meta = contents(st.meta);
    let e = (contents(st.index)[slot] & 0xffffffff) - 1;
    let m = stride() * e;
    if meta[m + 4] != 0 && meta[m + 4] <= st.now {
        remove_entry(st, e);
        st.expired = st.expired + 1;
        return 0 - 1;
    }
    meta[m + 7] = st.now;
    return e;
}

// The value of entry `e` (one `find` answered), as a view of the arena.
pub fn value[&s](st: &s Store, e: int) -> [] &s [byte] {
    let meta = contents(st.meta);
    let at = meta[stride() * e] + meta[stride() * e + 1];
    return contents(st.data)[at..at + meta[stride() * e + 2]];
}

// Entry `e`'s expiry in milliseconds on `tick`'s clock, or 0 for none.
pub fn expiry_of[&s](st: &s Store, e: int) -> [] int {
    return contents(st.meta)[stride() * e + 4];
}

// Give entry `e` an expiry (0: none).
pub fn set_expiry[&s](st: &!s Store, e: int, at: int) -> [] int {
    put_expiry(st, stride() * e, at);
    return 0;
}

// ---------------------------------------------------------------------
// Making room
// ---------------------------------------------------------------------

// Walk the records from the front and slide the live ones down over the garbage. A record is live if its entry is in
// use and still points at it; anything else (a value that grew and moved, a deleted key, an evicted one) is dropped.
// In place: a record only ever moves to a lower address, copied front to back, so it never overwrites one not yet moved.
fn compact[&s](st: &!s Store) -> [] int {
    let meta = contents(st.meta);
    let data = contents(st.data);
    var from = 0;
    var to = 0;
    while from < st.top {
        let e = get32(data, from);
        let size = get32(data, from + 4);
        let total = header() + size;
        let m = stride() * e;
        if meta[m + 6] == 0 - 2 && meta[m] == from + header() {
            if to != from {
                var i = 0;
                while i < total {
                    data[to + i] = data[from + i];
                    i = i + 1;
                }
                meta[m] = to + header();
            }
            to = to + total;
        }
        from = from + total;
    }
    st.top = to;
    st.dead = 0;
    st.compactions = st.compactions + 1;
    return 0;
}

// A pseudo-random number in `0..bound` (xorshift).
fn draw[&s](st: &!s Store, bound: int) -> [] int {
    var x = st.rng;
    x = x ^ x << 13;
    x = x ^ x >> 7 & 0x01ffffffffffffff;
    x = x ^ x << 17;
    st.rng = x;
    return (x >> 11 & 0x1fffffffffffff) % bound;
}

// Pick the entry to evict: of five random keys in use, the one used longest ago. -1 if there is no key to take.
fn victim[&s](st: &!s Store) -> [] int {
    if st.live == 0 {
        return 0 - 1;
    }
    let meta = contents(st.meta);
    var best = 0 - 1;
    var seen = 0;
    var tries = 0;
    while seen < 5 && tries < 40 {
        let e = draw(st, st.fresh);
        if meta[stride() * e + 6] == 0 - 2 && e != st.protect {
            if best < 0 || meta[stride() * e + 7] < meta[stride() * best + 7] {
                best = e;
            }
            seen = seen + 1;
        }
        tries = tries + 1;
    }
    if best >= 0 {
        return best;
    }
    // Sparse numbering (most entries free): look from a random start for any key in use.
    var k = 0;
    var e = draw(st, st.fresh);
    while k < st.fresh {
        if meta[stride() * e + 6] == 0 - 2 && e != st.protect {
            return e;
        }
        e = e + 1;
        if e >= st.fresh {
            e = 0;
        }
        k = k + 1;
    }
    return 0 - 1;
}

// Evict one key. 0 if there was none to take.
fn evict_one[&s](st: &!s Store) -> [] int {
    let e = victim(st);
    if e < 0 {
        return 0;
    }
    remove_entry(st, e);
    st.evicted = st.evicted + 1;
    return 1;
}

// Make `need` bytes available at the end of the arena: compact if that is enough, evict if the policy allows and it is
// not. True if there is room now.
fn make_room[&s](st: &!s Store, need: int) -> [] bool {
    let cap = len(contents(st.data));
    if need > cap {
        return false;
    }
    if st.top + need <= cap {
        return true;
    }
    if st.dead > 0 {
        compact(st);
    }
    if st.top + need <= cap {
        return true;
    }
    if st.policy == evict_none() {
        return false;
    }
    // Reclaim a sixteenth of the arena beyond `need`, so the next compaction is a sixteenth of the arena away.
    var want = need + cap / 16;
    if want > cap {
        want = cap;
    }
    var more = true;
    while more && cap - st.top + st.dead < want {
        more = evict_one(st) == 1;
    }
    compact(st);
    return st.top + need <= cap;
}

// ---------------------------------------------------------------------
// Writing and removing
// ---------------------------------------------------------------------

// Append a record for entry `e` (its key and `value`) at the end of the arena, which has room. Answers where the key
// starts.
fn append[&s, &k, &v](st: &!s Store, e: int, key: &k [byte], value: &v [byte]) -> [] int {
    let data = contents(st.data);
    let size = len(key) + room(len(value));
    let at = st.top;
    put32(data, at, e);
    put32(data, at + 4, size);
    copy(data, copy(data, at + header(), key), value);
    st.top = at + header() + size;
    return at + header();
}

// Store `value` under `key` (`h` is `hash_of`). `expiry` is when it ends, in `tick`'s milliseconds; 0 for never, and
// -1 to leave an existing key's expiry as it is (a new key then has none).
//
// 0: stored. 1: the arena has no room for it, even after compacting and whatever the policy lets it evict (and
// nothing was changed). 2: no room for another key.
pub fn set[&s, &k, &v](st: &!s Store, key: &k [byte], h: int, value: &v [byte], expiry: int) -> [] int {
    let e0 = find(st, key, h);
    if e0 >= 0 {
        let meta = contents(st.meta);
        let m = stride() * e0;
        if len(value) <= meta[m + 3] {
            copy(contents(st.data), meta[m] + meta[m + 1], value);
            meta[m + 2] = len(value);
        } else {
            let need = header() + len(key) + room(len(value));
            st.protect = e0;
            let ok = make_room(st, need);
            st.protect = 0 - 1;
            if !ok {
                return 1;
            }
            // Compacting may have moved it; this is where it is now. The old record is garbage from here on.
            st.dead = st.dead + header() + meta[m + 1] + meta[m + 3];
            meta[m] = append(st, e0, key, value);
            meta[m + 2] = len(value);
            meta[m + 3] = room(len(value));
        }
        if expiry >= 0 {
            put_expiry(st, m, expiry);
        }
        return 0;
    }
    if st.live >= st.max_keys {
        if st.policy == evict_none() || evict_one(st) == 0 {
            return 2;
        }
    }
    if !make_room(st, header() + len(key) + room(len(value))) {
        return 1;
    }
    let meta = contents(st.meta);
    let index = contents(st.index);
    var e = st.fresh;
    if st.free_head >= 0 {
        e = st.free_head;
        st.free_head = meta[stride() * e + 6];
    } else {
        st.fresh = st.fresh + 1;
    }
    let m = stride() * e;
    meta[m] = append(st, e, key, value);
    meta[m + 1] = len(key);
    meta[m + 2] = len(value);
    meta[m + 3] = room(len(value));
    meta[m + 4] = 0;
    meta[m + 5] = h;
    meta[m + 6] = 0 - 2;
    meta[m + 7] = st.now;
    if expiry > 0 {
        put_expiry(st, m, expiry);
    }
    var i = h & st.mask;
    while index[i] != 0 {
        i = i + 1 & st.mask;
    }
    index[i] = tag_of(h) << 32 | e + 1;
    st.live = st.live + 1;
    return 0;
}

// Remove `key`. 1 if it was there (and had not run out), 0 if not.
pub fn delete[&s, &k](st: &!s Store, key: &k [byte], h: int) -> [] int {
    let e = find(st, key, h);
    if e < 0 {
        return 0;
    }
    remove_entry(st, e);
    return 1;
}

// Look at up to `budget` entries from where the last call stopped, and remove those whose time has passed. Answers how
// many it removed. Called once a turn of the loop, so keys nobody asks for again still go.
pub fn sweep[&s](st: &!s Store, budget: int) -> [] int {
    let meta = contents(st.meta);
    var removed = 0;
    var looked = 0;
    while looked < budget && st.fresh > 0 {
        if st.cursor >= st.fresh {
            st.cursor = 0;
        }
        let m = stride() * st.cursor;
        if meta[m + 6] == 0 - 2 && meta[m + 4] != 0 && meta[m + 4] <= st.now {
            remove_entry(st, st.cursor);
            st.expired = st.expired + 1;
            removed = removed + 1;
        }
        st.cursor = st.cursor + 1;
        looked = looked + 1;
    }
    return removed;
}

// Remove every key.
pub fn clear[&s](st: &!s Store) -> [] int {
    let index = contents(st.index);
    let meta = contents(st.meta);
    var i = 0;
    while i < len(index) {
        index[i] = 0;
        i = i + 1;
    }
    var e = 0;
    while e < st.fresh {
        meta[stride() * e + 6] = 0 - 1;
        meta[stride() * e + 4] = 0;
        e = e + 1;
    }
    st.live = 0;
    st.fresh = 0;
    st.free_head = 0 - 1;
    st.top = 0;
    st.dead = 0;
    st.ttl_keys = 0;
    return 0;
}

// The time `tick` last set.
pub fn now_of[&s](st: &s Store) -> [] int {
    return st.now;
}
