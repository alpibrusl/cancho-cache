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
        answer = store.set(st, key, store.hash_of(st, key), vb[0..length]);
    }
    return answer;
}

// -1 if absent, otherwise the value's length * 1000 + its first byte (0 for an empty value).
fn look[&b](st: &b store.Store, n: int) -> [] int {
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
    var st = store.open(heap, 4096, 16, 0);
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
    var st = store.open(heap, 4096, 4, 0);
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
    var st = store.open(heap, 64, 100, 0);
    borrow mut st as &!w in {
        // 64 bytes of arena: each key `kN` (2 bytes) with a 14-byte value takes 2 + 16.
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
    var st = store.open(heap, 400000, n, 0);
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
    var st = store.open(heap, 40000000, 64, 0);
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
    var st = store.open(heap, 1024, 4, 0);
    borrow mut st as &!w in {
        let ha = store.hash_of(w, "c80067");
        let hb = store.hash_of(w, "c99133");
        test.assert_eq(ha >> 33 & 0x7fffffff, hb >> 33 & 0x7fffffff);
        test.assert_eq(ha & 7, hb & 7);
        test.assert_eq(store.set(w, "c80067", ha, "aaa"), 0);
        test.assert_eq(store.find(w, "c99133", hb), 0 - 1);
        test.assert_eq(store.set(w, "c99133", hb, "bb"), 0);
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
