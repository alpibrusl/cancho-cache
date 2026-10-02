edition 5;

module store;

import std.map;

// `store` -- the cache's memory: keys and values in one arena, found through one index (`docs/design.md` §5).
//
// Nothing is allocated after `open`. There are three fixed-size pieces:
//
//   * `data`, the arena: every key is followed by its value, written from the front. Overwriting a value that fits
//     the room it has is done in place; one that does not is appended anew and the old room becomes garbage
//     (`dead`), which compaction (step C2) takes back.
//   * `meta`, eight ints a key, indexed by an *entry number*: where its bytes start, the key's length, the value's
//     length and its room, the expiry (0: none), its full hash, and -- for an entry that is free -- the next free
//     one. Entries are numbered from 0 as they are first needed, and a deleted key's number is reused.
//   * `index`, open addressing with linear probing and **backward-shift deletion** (no tombstones, so a cache that
//     deletes forever does not get slower): a slot is 0 for empty, or the top bits of the key's hash above the entry
//     number plus one. Comparing the 31-bit tag first means a probe past another key's slot rarely touches `meta`.
//     It has at least twice as many slots as there can be keys, so it is never more than half full.
//
// What `set` refuses, it refuses by saying so (1: the arena is full, 2: no room for another key); it never grows.

// Ints a key takes in `meta`: 0 start of the key's bytes, 1 key length, 2 value length, 3 value room, 4 expiry,
// 5 hash, 6 next free entry (while free), 7 unused.
fn stride() -> [] int {
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
}

// A store with an arena of `memory` bytes and room for `max_keys` keys. `seed` is mixed into every hash; it is a
// constant until the cache reads one at start (a hostile client that can choose keys can make a known seed
// collide).
pub fn open[&h](heap: &!h Heap, memory: int, max_keys: int, seed: int) -> [heap] Store {
    var slots = 8;
    while slots < 2 * max_keys {
        slots = slots * 2;
    }
    return Store { index: box_slice(heap, slots, 0), meta: box_slice(heap, stride() * max_keys, 0), data: box_slice(heap, memory, byte_of(0)), mask: slots - 1, max_keys: max_keys, live: 0, fresh: 0, free_head: 0 - 1, top: 0, dead: 0, seed: seed };
}

pub fn close[&h](heap: &!h Heap, st: Store) -> [heap] int {
    let Store { index, meta, data, mask, max_keys, live, fresh, free_head, top, dead, seed } = st;
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

pub fn live[&s](st: &s Store) -> [] int {
    return st.live;
}

// Bytes of the arena in use, and how many of them are garbage.
pub fn used[&s](st: &s Store) -> [] int {
    return st.top;
}

pub fn dead[&s](st: &s Store) -> [] int {
    return st.dead;
}

fn tag_of(h: int) -> [] int {
    return h >> 33 & 0x7fffffff;
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

// The entry holding `key`, or -1.
pub fn find[&s, &k](st: &s Store, key: &k [byte], h: int) -> [] int {
    let i = find_slot(st, key, h);
    if i < 0 {
        return 0 - 1;
    }
    return (contents(st.index)[i] & 0xffffffff) - 1;
}

// The value of entry `e` (one `find` answered), as a view of the arena.
pub fn value[&s](st: &s Store, e: int) -> [] &s [byte] {
    let meta = contents(st.meta);
    let at = meta[stride() * e] + meta[stride() * e + 1];
    return contents(st.data)[at..at + meta[stride() * e + 2]];
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

// Store `value` under `key` (`h` is `hash_of`). 0: stored. 1: the arena has no room for it. 2: no room for another
// key. Overwriting never needs a new key.
pub fn set[&s, &k, &v](st: &!s Store, key: &k [byte], h: int, value: &v [byte]) -> [] int {
    let index = contents(st.index);
    let meta = contents(st.meta);
    let data = contents(st.data);
    let slot = find_slot(st, key, h);
    if slot >= 0 {
        let m = stride() * ((index[slot] & 0xffffffff) - 1);
        if len(value) <= meta[m + 3] {
            copy(data, meta[m] + meta[m + 1], value);
            meta[m + 2] = len(value);
            return 0;
        }
        let need = len(key) + room(len(value));
        if st.top + need > len(data) {
            return 1;
        }
        let at = st.top;
        copy(data, copy(data, at, key), value);
        st.top = st.top + need;
        st.dead = st.dead + meta[m + 1] + meta[m + 3];
        meta[m] = at;
        meta[m + 2] = len(value);
        meta[m + 3] = room(len(value));
        return 0;
    }
    if st.live >= st.max_keys {
        return 2;
    }
    let need = len(key) + room(len(value));
    if st.top + need > len(data) {
        return 1;
    }
    var e = st.fresh;
    if st.free_head >= 0 {
        e = st.free_head;
        st.free_head = meta[stride() * e + 6];
    } else {
        st.fresh = st.fresh + 1;
    }
    let at = st.top;
    copy(data, copy(data, at, key), value);
    st.top = st.top + need;
    let m = stride() * e;
    meta[m] = at;
    meta[m + 1] = len(key);
    meta[m + 2] = len(value);
    meta[m + 3] = room(len(value));
    meta[m + 4] = 0;
    meta[m + 5] = h;
    meta[m + 6] = 0;
    var i = h & st.mask;
    while index[i] != 0 {
        i = i + 1 & st.mask;
    }
    index[i] = tag_of(h) << 32 | e + 1;
    st.live = st.live + 1;
    return 0;
}

// Empty slot `hole`, and move back any later entry of the run that is allowed to sit there, so that no entry is ever
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

// Remove `key`. 1 if it was there, 0 if not.
pub fn delete[&s, &k](st: &!s Store, key: &k [byte], h: int) -> [] int {
    let slot = find_slot(st, key, h);
    if slot < 0 {
        return 0;
    }
    let meta = contents(st.meta);
    let e = (contents(st.index)[slot] & 0xffffffff) - 1;
    let m = stride() * e;
    st.dead = st.dead + meta[m + 1] + meta[m + 3];
    meta[m + 6] = st.free_head;
    st.free_head = e;
    st.live = st.live - 1;
    close_hole(st, slot);
    return 1;
}
