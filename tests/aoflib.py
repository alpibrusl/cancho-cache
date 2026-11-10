"""What the tests of the append-only log share: the CRC-64/XZ, a reader of the file, and the keyspace its records replay to (`docs/design.md` sections 38 to 40)."""
import struct

_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0xC96C5795D7870F42 if _c & 1 else _c >> 1
    _TABLE.append(_c)


def crc64(data):
    crc = 0xFFFFFFFFFFFFFFFF
    for b in data:
        crc = _TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFFFFFFFFFF


def parse(path):
    """([(unix ms of the batch, [(kind, key, value, ends)])], bytes after the last whole batch, [what was wrong]). Only batches that verify are returned."""
    d = open(path, "rb").read()
    problems = []
    if d[:8] != b"CANCHOLG" or len(d) < 32 or struct.unpack_from("<Q", d, 8)[0] != 1:
        return [], len(d), ["%s: not a log (%r)" % (path, d[:16])]
    at = 32
    batches, records, start_at = [], [], 32
    while at + 32 <= len(d):
        a, b, kind, c = struct.unpack_from("<QQQQ", d, at)
        if kind == 9:
            body = d[start_at:at]
            if a != len(body) or c != crc64(body):
                problems.append("%s: a batch at %d does not check out (length %d vs %d)" % (path, start_at, a, len(body)))
                return batches, len(d) - start_at, problems
            batches.append((b, records))
            records = []
            at += 32
            start_at = at
            continue
        key = d[at + 32:at + 32 + a]
        value = d[at + 32 + a:at + 32 + a + b]
        if len(key) != a or len(value) != b:
            break
        records.append((kind, key, value, c))
        at += 32 + a + b
    return batches, len(d) - start_at, problems


def flat(batches):
    return [r for _, rs in batches for r in rs]


def replay(batches):
    """{key: (kind, value, ends)}: what the records, applied in order, leave."""
    state = {}
    for k, key, v, e in flat(batches):
        if k in (0, 1):
            state[key] = (k, v, e)
        elif k == 2:
            state.pop(key, None)
        elif k == 3:
            if key in state:
                state[key] = (state[key][0], state[key][1], e)
        elif k == 4:
            state.clear()
    return state
