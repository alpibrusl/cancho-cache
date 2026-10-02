import std.test;
import store;

// What `store` keeps and gives back, with no server: overwriting in place and by moving, deletion that leaves every
// other key reachable (the backward shift), the two ways it refuses, and a randomised run checked against a model
// kept in plain arrays.

// `k<n>` into `buf`; answers its length.
fn key_into[&b](buf: &!b [byte], n: int) -> [] int {
    buf[0] = byte_of('k');
    var digits = 1;
    var rest = n / 10;
    while rest > 0 {
        digits = digits + 1;
        rest = rest / 10;
    }
    var value = n;
    var i = digits;
    while i > 0 {
        buf[i] = byte_of('0' + value % 10);
        value = value / 10;
        i = i - 1;
    }
    return digits + 1;
}

// `length` copies of the letter `c`.
fn fill_with[&b](buf: &!b [byte], c: int, length: int) -> [] int {
    var i = 0;
    while i < length {
        buf[i] = byte_of(c);
        i = i + 1;
    }
    return length;
}

fn put[&b](st: &!b store.Store, n: int, c: int, length: int) -> [] int {
    var answer = 0;
    region a {
        let kb = alloc_slice[a](24, byte_of(0));
        let vb = alloc_slice[a](length + 1, byte_of(0));
        let k = key_into(kb, n);
        fill_with(vb, c, length);
        let key = kb[0..k];
        answer = store.set(st, key, store.hash_of(st, key), vb[0..length], 0);
    }
    return answer;
}

// -1 if absent, otherwise the value's length * 1000 + its first byte (0 for an empty value).
fn look[&b](st: &!b store.Store, n: int) -> [] int {
    var answer = 0 - 1;
    region a {
        let kb = alloc_slice[a](24, byte_of(0));
        let k = key_into(kb, n);
        let key = kb[0..k];
        let e = store.find(st, key, store.hash_of(st, key));
        if e >= 0 {
            let v = store.value(st, e);
            answer = len(v) * 1000;
            if len(v) > 0 {
                answer = answer + int_of(v[0]);
                // Every byte is the same letter.
                var i = 0;
                while i < len(v) {
                    test.assert_eq(int_of(v[i]), int_of(v[0]));
                    i = i + 1;
                }
            }
        }
    }
    return answer;
}

fn drop_key[&b](st: &!b store.Store, n: int) -> [] int {
    var answer = 0;
    region a {
        let kb = alloc_slice[a](24, byte_of(0));
        let k = key_into(kb, n);
        let key = kb[0..k];
        answer = store.delete(st, key, store.hash_of(st, key));
    }
    return answer;
}

fn test_set_get_and_overwrite[&h](heap: &!h Heap) -> [heap] int {
    var st = store.open(heap, 4096, 16, 0, store.evict_none());
    borrow mut st as &!w in {
        test.assert_eq(look(w, 1), 0 - 1);
        test.assert_eq(put(w, 1, 'a', 3), 0);
        test.assert_eq(look(w, 1), 3 * 1000 + 'a');
        // Smaller, same size, and as large as the room (eight): all in place, so the arena does not grow.
        let before = store.used(w);
        test.assert_eq(put(w, 1, 'b', 2), 0);
        test.assert_eq(put(w, 1, 'c', 8), 0);
        test.assert_eq(put(w, 1, 'd', 0), 0);
        test.assert_eq(store.used(w), before);
        test.assert_eq(look(w, 1), 0);
        // Larger than the room: a new record, and the old one is garbage.
        test.assert_eq(put(w, 1, 'e', 9), 0);
        test.assert_eq(look(w, 1), 9 * 1000 + 'e');
        test.assert(store.used(w) > before);
        test.assert(store.dead(w) > 0);
        test.assert_eq(store.live(w), 1);
    }
    store.close(heap, st);
    return 0;
}

fn test_delete_and_reuse_of_an_entry[&h](heap: &!h Heap) -> [heap] int {
    var st = store.open(heap, 4096, 4, 0, store.evict_none());
    borrow mut st as &!w in {
        test.assert_eq(put(w, 1, 'a', 1), 0);
        test.assert_eq(put(w, 2, 'b', 1), 0);
        test.assert_eq(drop_key(w, 1), 1);
        test.assert_eq(drop_key(w, 1), 0);
        test.assert_eq(look(w, 1), 0 - 1);
        test.assert_eq(look(w, 2), 1000 + 'b');
        test.assert_eq(store.live(w), 1);
        // Four keys fit in all; the deleted key's entry is reused, so deleting makes room.
        test.assert_eq(put(w, 3, 'c', 1), 0);
        test.assert_eq(put(w, 4, 'd', 1), 0);
        test.assert_eq(put(w, 5, 'e', 1), 0);
        test.assert_eq(put(w, 6, 'f', 1), 2);
        test.assert_eq(drop_key(w, 3), 1);
        test.assert_eq(put(w, 6, 'f', 1), 0);
    }
    store.close(heap, st);
    return 0;
}

fn test_the_two_ways_of_being_full[&h](heap: &!h Heap) -> [heap] int {
    var st = store.open(heap, 80, 100, 0, store.evict_none());
    borrow mut st as &!w in {
        // 80 bytes of arena: each record is an 8-byte header, the key `kN` (2 bytes) and a 14-byte value rounded up to 16: 26.
        test.assert_eq(put(w, 1, 'a', 14), 0);
        test.assert_eq(put(w, 2, 'a', 14), 0);
        test.assert_eq(put(w, 3, 'a', 14), 0);
        test.assert_eq(put(w, 4, 'a', 14), 1);
        // Refused means unchanged.
        test.assert_eq(look(w, 4), 0 - 1);
        test.assert_eq(look(w, 3), 14 * 1000 + 'a');
        test.assert_eq(store.live(w), 3);
        // An overwrite that does not fit is refused the same way and leaves the old value.
        test.assert_eq(put(w, 1, 'z', 40), 1);
        test.assert_eq(look(w, 1), 14 * 1000 + 'a');
    }
    store.close(heap, st);
    return 0;
}

fn test_many_keys_deleted_in_three_orders[&h](heap: &!h Heap) -> [heap] int {
    let n = 3000;
    var st = store.open(heap, 400000, n, 0, store.evict_none());
    borrow mut st as &!w in {
        var i = 0;
        while i < n {
            test.assert_eq(put(w, i, 'a' + i % 26, 1 + i % 20), 0);
            i = i + 1;
        }
        test.assert_eq(store.live(w), n);
        // Every third, from the front.
        i = 0;
        while i < n {
            test.assert_eq(drop_key(w, i), 1);
            i = i + 3;
        }
        i = 0;
        while i < n {
            if i % 3 == 0 {
                test.assert_eq(look(w, i), 0 - 1);
            } else {
                test.assert_eq(look(w, i), (1 + i % 20) * 1000 + 'a' + i % 26);
            }
            i = i + 1;
        }
        // Every other of the rest, from the back.
        i = n - 1;
        while i >= 0 {
            if i % 3 != 0 && i % 2 == 0 {
                test.assert_eq(drop_key(w, i), 1);
            }
            i = i - 1;
        }
        i = 0;
        while i < n {
            if i % 3 == 0 || i % 2 == 0 {
                test.assert_eq(look(w, i), 0 - 1);
            } else {
                test.assert_eq(look(w, i), (1 + i % 20) * 1000 + 'a' + i % 26);
            }
            i = i + 1;
        }
        // The deleted ones come back, and now everything is there.
        i = 0;
        while i < n {
            if i % 3 == 0 || i % 2 == 0 {
                test.assert_eq(put(w, i, 'q', 5), 0);
            }
            i = i + 1;
        }
        test.assert_eq(store.live(w), n);
        i = 0;
        while i < n {
            if i % 3 == 0 || i % 2 == 0 {
                test.assert_eq(look(w, i), 5000 + 'q');
            } else {
                test.assert_eq(look(w, i), (1 + i % 20) * 1000 + 'a' + i % 26);
            }
            i = i + 1;
        }
        // Delete them all.
        i = 0;
        while i < n {
            test.assert_eq(drop_key(w, i), 1);
            i = i + 1;
        }
        test.assert_eq(store.live(w), 0);
        test.assert_eq(look(w, 7), 0 - 1);
    }
    store.close(heap, st);
    return 0;
}

// A linear congruential step that cannot overflow: 31-bit state.
fn next(x: int) -> [] int {
    return (x * 1103515245 + 12345) % 2147483648;
}

// 200,000 random operations over 64 keys against a model of plain arrays: set (a value of a random length,
// sometimes growing past its room), get, delete. The arena is large enough that no set is refused.
fn test_a_random_run_agrees_with_a_model[&h](heap: &!h Heap) -> [heap] int {
    var st = store.open(heap, 40000000, 64, 0, store.evict_none());
    var seed = 12345;
    var round = 0;
    borrow mut st as &!w in {
        region a {
            // For each of the 64 keys: -1 absent, else length * 1000 + letter.
            let model = alloc_slice[a](64, 0 - 1);
            while round < 200000 {
                seed = next(seed);
                let key = seed / 8 % 64;
                seed = next(seed);
                let op = seed / 8 % 10;
                seed = next(seed);
                let length = seed / 8 % 40;
                let letter = 'a' + seed / 4096 % 26;
                if op < 5 {
                    test.assert_eq(put(w, key, letter, length), 0);
                    model[key] = length * 1000;
                    if length > 0 {
                        model[key] = model[key] + letter;
                    }
                } else if op < 8 {
                    test.assert_eq(look(w, key), model[key]);
                } else {
                    var existed = 0;
                    if model[key] >= 0 {
                        existed = 1;
                    }
                    test.assert_eq(drop_key(w, key), existed);
                    model[key] = 0 - 1;
                }
                round = round + 1;
            }
            // And at the end, every key agrees.
            var k = 0;
            while k < 64 {
                test.assert_eq(look(w, k), model[k]);
                k = k + 1;
            }
        }
    }
    store.close(heap, st);
    return 0;
}

// Two keys of the same length that the hash puts in the same home slot with the same 31-bit tag (found by search:
// 8 slots, `c80067` and `c99133`), so only the comparison of the bytes tells them apart. Random keys never collide
// this way in a test, which is why this one is pinned.
fn test_keys_that_collide_in_tag_and_slot_are_told_apart[&h](heap: &!h Heap) -> [heap] int {
    var st = store.open(heap, 1024, 4, 0, store.evict_none());
    borrow mut st as &!w in {
        let ha = store.hash_of(w, "c80067");
        let hb = store.hash_of(w, "c99133");
        test.assert_eq(ha >> 33 & 0x7fffffff, hb >> 33 & 0x7fffffff);
        test.assert_eq(ha & 7, hb & 7);
        test.assert_eq(store.set(w, "c80067", ha, "aaa", 0), 0);
        test.assert_eq(store.find(w, "c99133", hb), 0 - 1);
        test.assert_eq(store.set(w, "c99133", hb, "bb", 0), 0);
        test.assert_eq(store.live(w), 2);
        let ea = store.find(w, "c80067", ha);
        let eb = store.find(w, "c99133", hb);
        test.assert(ea >= 0);
        test.assert(eb >= 0);
        test.assert(ea != eb);
        test.assert_eq(len(store.value(w, ea)), 3);
        test.assert_eq(len(store.value(w, eb)), 2);
        test.assert_eq(store.delete(w, "c80067", ha), 1);
        test.assert_eq(store.find(w, "c80067", ha), 0 - 1);
        test.assert_eq(len(store.value(w, store.find(w, "c99133", hb))), 2);
    }
    store.close(heap, st);
    return 0;
}

// How many bytes a record of a key of `klen` bytes and a value of `vlen` takes: the 8-byte header, the key, and the value
// rounded up to a multiple of eight. (What `store` does, written out so that the model below does not use `store`.)
fn record(klen: int, vlen: int) -> [] int {
    return 8 + klen + (vlen + 7) / 8 * 8;
}

fn key_length(n: int) -> [] int {
    var digits = 1;
    var rest = n / 10;
    while rest > 0 {
        digits = digits + 1;
        rest = rest / 10;
    }
    return digits + 1;
}

// A small arena that is often full, no eviction: 100,000 random operations against a model that knows exactly when a
// set must be refused -- when the records of the keys that exist (the key's own old record included, since compaction
// keeps it until the new one is written) plus the new one do not fit -- and checks every value it reads back. This is
// what compaction has to get exactly right: a value that moved must still be the value.
fn test_compaction_keeps_every_value_and_refuses_only_when_it_must[&h](heap: &!h Heap) -> [heap] int {
    let cap = 1500;
    var st = store.open(heap, cap, 40, 0, store.evict_none());
    var seed = 777;
    var round = 0;
    var refused = 0;
    borrow mut st as &!w in {
        region a {
            let model = alloc_slice[a](40, 0 - 1);
            // The room each live key holds: what it was given when its record was last written. A shorter value
            // written later keeps the room, and a record's size is its room, not its length.
            let held = alloc_slice[a](40, 0);
            while round < 100000 {
                seed = next(seed);
                let key = seed / 8 % 40;
                seed = next(seed);
                let op = seed / 8 % 10;
                seed = next(seed);
                let length = seed / 8 % 60;
                let letter = 'a' + seed / 4096 % 26;
                if op < 6 {
                    // What is live now, in bytes.
                    var live_bytes = 0;
                    var k = 0;
                    while k < 40 {
                        if model[k] >= 0 {
                            live_bytes = live_bytes + 8 + key_length(k) + held[k];
                        }
                        k = k + 1;
                    }
                    var fits = true;
                    var in_place = false;
                    if model[key] >= 0 && length <= held[key] {
                        // In place: needs nothing.
                        in_place = true;
                    } else if live_bytes + record(key_length(key), length) > cap {
                        fits = false;
                    }
                    let answer = put(w, key, letter, length);
                    if fits {
                        test.assert_eq(answer, 0);
                        if !in_place {
                            held[key] = (length + 7) / 8 * 8;
                        }
                        model[key] = length * 1000;
                        if length > 0 {
                            model[key] = model[key] + letter;
                        }
                    } else {
                        test.assert_eq(answer, 1);
                        refused = refused + 1;
                    }
                } else if op < 9 {
                    test.assert_eq(look(w, key), model[key]);
                } else {
                    var existed = 0;
                    if model[key] >= 0 {
                        existed = 1;
                    }
                    test.assert_eq(drop_key(w, key), existed);
                    model[key] = 0 - 1;
                }
                round = round + 1;
            }
            var k = 0;
            while k < 40 {
                test.assert_eq(look(w, k), model[k]);
                k = k + 1;
            }
        }
        // It really was full, and really did compact.
        test.assert(refused > 100);
        test.assert(store.compactions(w) > 100);
    }
    store.close(heap, st);
    return 0;
}

fn test_lru_eviction_never_refuses_and_keeps_what_is_used[&h](heap: &!h Heap) -> [heap] int {
    // A record is 8 + 4 or 5 (the key) + 16 (the value) = 28 or 29 bytes: this arena holds about two hundred.
    let cap = 6000;
    var st = store.open(heap, cap, 1000, 0, store.evict_lru());
    borrow mut st as &!w in {
        var i = 0;
        while i < 1000 {
            store.tick(w, 1000 + i);
            // Five keys are used all the time; the rest are written once.
            if i >= 5 {
                test.assert_eq(put(w, i, 'x', 16), 0);
            } else {
                test.assert_eq(put(w, i, 'h', 16), 0);
            }
            var hot = 0;
            while hot < 5 && i >= 5 {
                test.assert(look(w, hot) >= 0);
                hot = hot + 1;
            }
            i = i + 1;
        }
        test.assert(store.evicted(w) > 700);
        test.assert(store.live(w) < 250);
        // The five that were used all the time are all still there.
        var hot = 0;
        while hot < 5 {
            test.assert_eq(look(w, hot), 16 * 1000 + 'h');
            hot = hot + 1;
        }
        // And every key that is there holds what was written to it.
        var k = 5;
        var found = 0;
        while k < 1000 {
            let seen = look(w, k);
            if seen >= 0 {
                test.assert_eq(seen, 16 * 1000 + 'x');
                found = found + 1;
            }
            k = k + 1;
        }
        test.assert_eq(found + 5, store.live(w));
        // The most recent writes are the likeliest to be there.
        test.assert(look(w, 999) >= 0);
    }
    store.close(heap, st);
    return 0;
}

fn test_a_key_table_that_is_full_evicts_under_lru_and_refuses_otherwise[&h](heap: &!h Heap) -> [heap] int {
    var st = store.open(heap, 100000, 8, 0, store.evict_lru());
    borrow mut st as &!w in {
        var i = 0;
        while i < 100 {
            store.tick(w, i);
            test.assert_eq(put(w, i, 'a', 4), 0);
            i = i + 1;
        }
        test.assert_eq(store.live(w), 8);
        test.assert_eq(store.evicted(w), 92);
    }
    store.close(heap, st);
    return 0;
}

fn test_expiry_lazy_and_swept[&h](heap: &!h Heap) -> [heap] int {
    var st = store.open(heap, 10000, 100, 0, store.evict_none());
    borrow mut st as &!w in {
        store.tick(w, 1000);
        // `a` lives until 1500, `b` for ever, `c` until 1200.
        test.assert_eq(store.set(w, "a", store.hash_of(w, "a"), "1", 1500), 0);
        test.assert_eq(store.set(w, "b", store.hash_of(w, "b"), "2", 0), 0);
        test.assert_eq(store.set(w, "c", store.hash_of(w, "c"), "3", 1200), 0);
        test.assert_eq(store.keys_with_expiry(w), 2);
        test.assert(store.find(w, "a", store.hash_of(w, "a")) >= 0);
        store.tick(w, 1199);
        test.assert(store.find(w, "c", store.hash_of(w, "c")) >= 0);
        // At its time it is gone (the time is the first moment it is not there).
        store.tick(w, 1200);
        test.assert_eq(store.find(w, "c", store.hash_of(w, "c")), 0 - 1);
        test.assert_eq(store.expired(w), 1);
        test.assert_eq(store.live(w), 2);
        test.assert_eq(store.keys_with_expiry(w), 1);
        // Nobody asks for `a` again; the sweep takes it.
        store.tick(w, 2000);
        test.assert_eq(store.sweep(w, 10), 1);
        test.assert_eq(store.live(w), 1);
        test.assert_eq(store.keys_with_expiry(w), 0);
        test.assert(store.find(w, "b", store.hash_of(w, "b")) >= 0);
        // A set clears an expiry; -1 keeps it; a new one replaces it.
        test.assert_eq(store.set(w, "b", store.hash_of(w, "b"), "22", 3000), 0);
        test.assert_eq(store.set(w, "b", store.hash_of(w, "b"), "222", 0 - 1), 0);
        test.assert_eq(store.expiry_of(w, store.find(w, "b", store.hash_of(w, "b"))), 3000);
        test.assert_eq(store.set(w, "b", store.hash_of(w, "b"), "2222", 0), 0);
        test.assert_eq(store.expiry_of(w, store.find(w, "b", store.hash_of(w, "b"))), 0);
        test.assert_eq(store.keys_with_expiry(w), 0);
        // A key that is set again after it ran out is a new key.
        test.assert_eq(store.set(w, "a", store.hash_of(w, "a"), "x", 2500), 0);
        store.tick(w, 2600);
        test.assert_eq(store.set(w, "a", store.hash_of(w, "a"), "y", 0 - 1), 0);
        test.assert_eq(store.expiry_of(w, store.find(w, "a", store.hash_of(w, "a"))), 0);
        // clear empties everything.
        store.clear(w);
        test.assert_eq(store.live(w), 0);
        test.assert_eq(store.find(w, "b", store.hash_of(w, "b")), 0 - 1);
        test.assert_eq(store.set(w, "b", store.hash_of(w, "b"), "again", 0), 0);
    }
    store.close(heap, st);
    return 0;
}

// Under LRU in a small arena with values that grow, shrink and come back: a set that says it succeeded leaves its key
// there with exactly the value written (an eviction made to give a key room must never take that key), and any key that
// is there holds the last value written to it, never an older one or another key's.
fn test_a_set_under_eviction_never_loses_its_own_key[&h](heap: &!h Heap) -> [heap] int {
    var st = store.open(heap, 700, 30, 0, store.evict_lru());
    var seed = 4242;
    var round = 0;
    borrow mut st as &!w in {
        region a {
            // The last (length * 1000 + letter) written to each key, or -1 if never written.
            let last = alloc_slice[a](30, 0 - 1);
            while round < 50000 {
                store.tick(w, round);
                seed = next(seed);
                let key = seed / 8 % 30;
                seed = next(seed);
                let length = seed / 8 % 90;
                let letter = 'a' + seed / 4096 % 26;
                if round % 4 != 3 {
                    let answer = put(w, key, letter, length);
                    // A value of 90 bytes in a 700-byte arena always fits once the rest is evicted.
                    test.assert_eq(answer, 0);
                    last[key] = length * 1000;
                    if length > 0 {
                        last[key] = last[key] + letter;
                    }
                    test.assert_eq(look(w, key), last[key]);
                } else {
                    let seen = look(w, key);
                    if seen >= 0 {
                        test.assert_eq(seen, last[key]);
                    }
                }
                round = round + 1;
            }
        }
        test.assert(store.evicted(w) > 1000);
    }
    store.close(heap, st);
    return 0;
}

// A key that grows when it is the only key and the old and the new record do not both fit: there is nothing else to
// evict, and the key being grown must not be taken. The set is refused and the old value stays (a conservative answer:
// the new value alone would fit, but the old one is not given up before the new one is written).
fn test_growing_the_only_key_is_refused_not_self_evicted[&h](heap: &!h Heap) -> [heap] int {
    var st = store.open(heap, 100, 10, 0, store.evict_lru());
    borrow mut st as &!w in {
        // 8 + 2 + 16 = 26 bytes.
        test.assert_eq(put(w, 1, 'a', 10), 0);
        // 8 + 2 + 80 = 90 more: 116 > 100, and the only key is the one being grown.
        test.assert_eq(put(w, 1, 'b', 80), 1);
        test.assert_eq(look(w, 1), 10 * 1000 + 'a');
        test.assert_eq(store.live(w), 1);
        test.assert_eq(store.evicted(w), 0);
        // 8 + 2 + 40 = 50 more: 76 <= 100, so it fits beside the old record, which is garbage from then on.
        test.assert_eq(put(w, 1, 'c', 40), 0);
        test.assert_eq(look(w, 1), 40 * 1000 + 'c');
        test.assert_eq(store.dead(w), 26);
    }
    store.close(heap, st);
    return 0;
}
