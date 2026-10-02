import std.test;
import resp;

// What `resp.parse` answers, with no server: whole commands, every proper prefix of one, pipelined
// commands, binary arguments, each way of not being the protocol, and -- the property the file is for --
// that **no byte string of any of these lengths makes it trap**, and that whatever it accepts is a
// self-contained command no shorter prefix of which parses.

fn parsed[&d](data: &d [byte]) -> [] int {
    var answer = 0;
    region a {
        let table = alloc_slice[a](resp.slots(), 0);
        answer = resp.parse(data, table);
    }
    return answer;
}

fn test_a_whole_command() -> [] int {
    region a {
        let table = alloc_slice[a](resp.slots(), 0);
        let data = "*3\r\n$3\r\nSET\r\n$3\r\nkey\r\n$5\r\nvalue\r\n";
        test.assert_eq(resp.parse(data, table), len(data));
        test.assert_eq(table[0], 3);
        test.assert_eq(table[1], 8);
        test.assert_eq(table[2], 3);
        test.assert_eq(table[3], 17);
        test.assert_eq(table[4], 3);
        test.assert_eq(table[5], 26);
        test.assert_eq(table[6], 5);
    }
    return 0;
}

fn test_every_proper_prefix_is_not_all_here_yet() -> [] int {
    let data = "*3\r\n$3\r\nSET\r\n$3\r\nkey\r\n$5\r\nvalue\r\n";
    var i = 0;
    while i < len(data) {
        test.assert_eq(parsed(data[0..i]), 0);
        i = i + 1;
    }
    test.assert_eq(parsed(data), len(data));
    return 0;
}

fn test_the_first_of_two_pipelined_commands() -> [] int {
    let data = "*1\r\n$4\r\nPING\r\n*2\r\n$4\r\nECHO\r\n$2\r\nhi\r\n";
    test.assert_eq(parsed(data), 14);
    test.assert_eq(parsed(data[14..len(data)]), len(data) - 14);
    return 0;
}

fn test_an_argument_may_hold_the_protocols_own_bytes() -> [] int {
    // The value is `\r\n*1\r\n`: six bytes that look like the start of another command.
    let data = "*2\r\n$3\r\nGET\r\n$6\r\n\r\n*1\r\n\r\n";
    region a {
        let table = alloc_slice[a](resp.slots(), 0);
        test.assert_eq(resp.parse(data, table), len(data));
        test.assert_eq(table[0], 2);
        test.assert_eq(table[4], 6);
    }
    return 0;
}

fn test_empty_arguments_and_empty_arrays() -> [] int {
    region a {
        let table = alloc_slice[a](resp.slots(), 0);
        test.assert_eq(resp.parse("*0\r\n", table), 4);
        test.assert_eq(table[0], 0);
        test.assert_eq(resp.parse("*1\r\n$0\r\n\r\n", table), 10);
        test.assert_eq(table[0], 1);
        test.assert_eq(table[2], 0);
    }
    return 0;
}

fn test_each_way_of_not_being_the_protocol() -> [] int {
    test.assert_eq(parsed("PING\r\n"), resp.error_expected_array());
    test.assert_eq(parsed("*x\r\n"), resp.error_array_length());
    test.assert_eq(parsed("*-1\r\n"), 5);
    test.assert_eq(parsed("*-x\r\n"), resp.error_array_length());
    test.assert_eq(parsed("*-\r\n"), resp.error_array_length());
    test.assert_eq(parsed("*\r\n"), resp.error_array_length());
    test.assert_eq(parsed("*1\r\nx"), resp.error_expected_bulk());
    test.assert_eq(parsed("*1\r\n$x\r\n"), resp.error_bulk_length());
    test.assert_eq(parsed("*1\r\n$-1\r\n"), resp.error_bulk_length());
    test.assert_eq(parsed("*1\r\n$3\r\nabcde\r\n"), resp.error_missing_terminator());
    test.assert_eq(parsed("*64\r\n"), 0);
    test.assert_eq(parsed("*65\r\n"), resp.error_too_many_arguments());
    test.assert_eq(parsed("*1234567890\r\n"), resp.error_array_length());
    test.assert_eq(parsed("*1\r\n$999999999\r\n"), 0);
    return 0;
}

fn symbol(i: int) -> [] int {
    if i == 0 {
        return '*';
    }
    if i == 1 {
        return '$';
    }
    if i == 2 {
        return '\r';
    }
    if i == 3 {
        return '\n';
    }
    if i == 4 {
        return '0';
    }
    if i == 5 {
        return '1';
    }
    if i == 6 {
        return '3';
    }
    return 'x';
}

// Every string of `length` bytes over the eight symbols above: 8^6 = 262,144 of them at the longest.
fn sweep(length: int) -> [] int {
    var total = 1;
    var k = 0;
    while k < length {
        total = total * 8;
        k = k + 1;
    }
    region a {
        let buf = alloc_slice[a](16, byte_of(0));
        let table = alloc_slice[a](resp.slots(), 0);
        var n = 0;
        while n < total {
            var rest = n;
            var j = 0;
            while j < length {
                buf[j] = byte_of(symbol(rest % 8));
                rest = rest / 8;
                j = j + 1;
            }
            let view = buf[0..length];
            let r = resp.parse(view, table);
            // Never more than it was given, never an unknown error.
            test.assert(r <= length);
            test.assert(r >= 0 - 6);
            if r > 0 {
                // What it accepted: every argument lies inside it...
                var i = 0;
                while i < table[0] {
                    test.assert(table[1 + 2 * i] >= 0);
                    test.assert(table[2 + 2 * i] >= 0);
                    test.assert(table[1 + 2 * i] + table[2 + 2 * i] + 2 <= r);
                    i = i + 1;
                }
                // ...and no shorter prefix of it is a whole command.
                var p = 0;
                while p < r {
                    test.assert_eq(resp.parse(view[0..p], table), 0);
                    p = p + 1;
                }
            }
            n = n + 1;
        }
    }
    return 0;
}

fn test_no_string_up_to_six_bytes_traps_or_lies() -> [] int {
    var length = 0;
    while length <= 6 {
        sweep(length);
        length = length + 1;
    }
    return 0;
}
