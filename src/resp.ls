edition 5;

module resp;

// `resp` -- the Redis wire protocol (RESP2), the part a cache needs: parsing one command, and the
// replies that answer it.
//
// Sans-io, like `std.http`: `parse` is a pure function from the bytes in front of it to "this
// command, this many bytes long", "not all here yet", or "this is not the protocol". It allocates
// nothing and **never traps**, whatever the bytes (`tests/resp_test.ls` runs it over every string up to
// a length, to say so). A command comes back as offsets into the caller's buffer, in a table the
// caller owns, so a well-formed command costs no copy.
//
// Only the array-of-bulk-strings form a client library sends is parsed. The *inline* form a person
// types into `telnet` (`PING\r\n`) is not, and is refused as `error_expected_array`: a deliberate
// gap (`docs/design.md` §4), not an oversight, and the differential test names it.
//
// The table `parse` fills, from index 0:
//
//     [0]            argc
//     [1 + 2i]       where argument i starts
//     [2 + 2i]       how long it is
//
// and `slots()` is how big the table has to be.

// The most arguments one command may have. `SET key value EX 10 NX` has six.
fn max_args() -> [] int {
    return 16;
}

pub fn slots() -> [] int {
    return 1 + 2 * max_args();
}

// What `parse` answers besides a length: 0 is "not all here yet", and these are "not the protocol".
pub fn error_expected_array() -> [] int {
    return 0 - 1;
}

pub fn error_array_length() -> [] int {
    return 0 - 2;
}

pub fn error_expected_bulk() -> [] int {
    return 0 - 3;
}

pub fn error_bulk_length() -> [] int {
    return 0 - 4;
}

pub fn error_missing_terminator() -> [] int {
    return 0 - 5;
}

pub fn error_too_many_arguments() -> [] int {
    return 0 - 6;
}

// A decimal number at `data[at..]` and the CRLF after it. Answers `(value, next)` where `next` is the
// index after the LF; `value` is -1 if the line is not all here, -2 if it is not a number. At most nine
// digits, so nothing here can overflow.
fn number[&d](data: &d [byte], at: int) -> [] (int, int) {
    var i = at;
    var n = 0;
    var digits = 0;
    while i < len(data) {
        let c = int_of(data[i]);
        if c == '\r' {
            if i + 1 >= len(data) {
                return (0 - 1, i);
            }
            if int_of(data[i + 1]) != '\n' {
                return (0 - 2, i);
            }
            if digits == 0 {
                return (0 - 2, i);
            }
            return (n, i + 2);
        }
        if c < '0' || c > '9' {
            return (0 - 2, i);
        }
        digits = digits + 1;
        if digits > 9 {
            return (0 - 2, i);
        }
        n = n * 10 + (c - '0');
        i = i + 1;
    }
    return (0 - 1, i);
}

// Parse the command at the front of `data`. Answers how many bytes it is (and fills `table`), or 0 if
// the rest has not arrived (call again with more), or a negative `error_*`.
//
// An array of no elements (`*0\r\n`) is a command of no arguments, answered with its length and an
// argc of 0: Redis ignores it, and so should the caller.
pub fn parse[&d, &t](data: &d [byte], table: &!t [int]) -> [] int {
    if len(data) == 0 {
        return 0;
    }
    if int_of(data[0]) != '*' {
        return error_expected_array();
    }
    let (argc, first) = number(data, 1);
    if argc == 0 - 1 {
        return 0;
    }
    if argc == 0 - 2 {
        return error_array_length();
    }
    if argc > max_args() {
        return error_too_many_arguments();
    }
    table[0] = argc;
    var at = first;
    var k = 0;
    while k < argc {
        if at >= len(data) {
            return 0;
        }
        if int_of(data[at]) != '$' {
            return error_expected_bulk();
        }
        let (size, start) = number(data, at + 1);
        if size == 0 - 1 {
            return 0;
        }
        if size == 0 - 2 {
            return error_bulk_length();
        }
        if start + size + 2 > len(data) {
            return 0;
        }
        if int_of(data[start + size]) != '\r' || int_of(data[start + size + 1]) != '\n' {
            return error_missing_terminator();
        }
        table[1 + 2 * k] = start;
        table[2 + 2 * k] = size;
        at = start + size + 2;
        k = k + 1;
    }
    return at;
}

// The text Redis puts after `-ERR ` for each of these (and closes the connection after).
pub fn error_text(code: int) -> [] &static [byte] {
    if code == error_expected_array() {
        return "Protocol error: expected '*', got inline command (not supported)";
    }
    if code == error_array_length() {
        return "Protocol error: invalid multibulk length";
    }
    if code == error_expected_bulk() {
        return "Protocol error: expected '$'";
    }
    if code == error_bulk_length() {
        return "Protocol error: invalid bulk length";
    }
    if code == error_missing_terminator() {
        return "Protocol error: expected CRLF after bulk";
    }
    return "Protocol error: too many arguments";
}
