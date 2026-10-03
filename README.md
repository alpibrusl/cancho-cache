# lexsys-cache

[![ci](https://github.com/alpibrusl/lexsys-cache/actions/workflows/ci.yml/badge.svg)](https://github.com/alpibrusl/lexsys-cache/actions/workflows/ci.yml)

A Redis-compatible cache, written in [lex-sys](https://github.com/alpibrusl/lex-sys): no `Ffi`, no `unsafe`, and a checkable
authority report that says it never touches the filesystem or foreign code.

It speaks RESP2 and RESP3 over TCP on one thread with one poller, holds its data in a fixed arena (sized at start, nothing
allocated afterwards), and answers 38 commands of Redis 7.0's 240: strings, expiry and an LRU eviction policy. `redis-cli`,
`redis-py` and `ioredis` connect and work.

It is **not** a Redis replacement: no data structures beyond strings, no persistence, replication or TLS, one database, no
transactions (`MULTI`/`EXEC`, so a `redis-py` `pipeline()` in its default, transactional mode does not work;
`pipeline(transaction=False)` does).

## Status

**Step C2, and real clients connect.** On one core against Redis 7.0.15 it is at 1.07 to 2.00 times Redis's throughput on the
pre-registered cells, with the same hit rate at the same resident memory, doing much less than Redis does. Its worst-case
pause (a compaction) is bounded by a slice of work, not by the arena: 3 to 8 ms measured on a noisy VM, against 115 ms before.
Every measurement, its caveats and the gate fixed before the code are in [`docs/design.md`](docs/design.md).

## Requirements

- The **lex-sys** compiler at the revision this repository's CI builds with (below); the revision is part of the contract.
- Rust, to build that compiler (its `rust-toolchain.toml` pins the toolchain).
- To run the tests: `redis-server` and `redis-cli` (the oracle of the differential test), `python3` with `pip install redis`,
  and `node`/`npm` for the `ioredis` check.

## Quick start

```sh
git clone https://github.com/alpibrusl/lex-sys
git clone https://github.com/alpibrusl/lexsys-cache && cd lexsys-cache

REV=$(sed -n 's/^ *LEX_SYS_REV: *//p' .github/workflows/ci.yml)    # the revision CI uses
(cd ../lex-sys && git checkout "$REV" && cargo build --release -p lex-sys)
export LEX_SYS=$PWD/../lex-sys/target/release/lex-sys

mkdir -p build
$LEX_SYS build --std src/cache.ls src/resp.ls src/store.ls src/commands.ls src/reply.ls src/session.ls -o build/cache

build/cache 6379 &                     # port; optional: arena MiB, most keys, noeviction | allkeys-lru
redis-cli -p 6379 PING                 # PONG
```

The arguments are `cache <port> [<arena MiB> [<most keys> [noeviction | allkeys-lru]]]`, with 64 MiB, 1,000,000 keys and
`noeviction` by default. Sizes are fixed at start.

## Examples

**From `redis-cli`:**

```
$ redis-cli -p 6379 SET greeting hello EX 60
OK
$ redis-cli -p 6379 GET greeting
"hello"
$ redis-cli -p 6379 TTL greeting
(integer) 60
$ redis-cli -p 6379 INCR visits ; redis-cli -p 6379 INCRBY visits 41
(integer) 1
(integer) 42
$ redis-cli -p 6379 MSET a 1 b 2 ; redis-cli -p 6379 MGET a b nope
OK
1) "1"
2) "2"
3) (nil)
```

**From a client library:**

```python
import redis
r = redis.Redis(port=6379)          # RESP2 or RESP3: both work
r.set("k", "v", ex=60)
r.get("k")                          # b'v'
```

**See what it is allowed to do** (this is checked in CI, not assumed):

```sh
$LEX_SYS authority src/cache.ls src/resp.ls src/store.ls src/commands.ls src/reply.ls src/session.ls --std
# performs: args, clock, conn_accept, conn_read, conn_write, err_write, heap, net_in(""), poll
# never touches: the filesystem, foreign code
```

## Commands

`GET` and `SET` (with `NX XX GET EX PX KEEPTTL`), `SETNX`, `SETEX`, `GETSET`, `GETDEL`, `MGET`, `MSET`, `INCR`, `DECR`,
`INCRBY`, `DECRBY`, `DEL`, `EXISTS`, `TYPE`, `STRLEN`, `DBSIZE`, `FLUSHALL`; `EXPIRE` and `PEXPIRE` (with `NX XX GT LT`),
`TTL`, `PTTL`, `PERSIST`; and what a client library says on connecting: `HELLO` (RESP2 and RESP3), `AUTH`, `CLIENT`, `INFO`,
`CONFIG GET`, `COMMAND`, `QUIT`, `RESET`. The eviction policy is `allkeys-lru`. Every reply is byte-identical to Redis 7.0.15
for the cases in `tests/differential.py`; the deliberate divergences (inline commands refused, `CONFIG SET` refused, a
`COMMAND` that keeps no table of flags) are listed in [`docs/design.md`](docs/design.md) section 12.

## Tests

```sh
$LEX_SYS test tests/resp_test.ls src/resp.ls --std        # the parser, over every string up to six bytes
$LEX_SYS test tests/store_test.ls src/store.ls --std      # the store: collisions, deletion, a random run against a model
python3 tests/differential.py build/cache                 # byte-for-byte against redis-server, four framings each
python3 tests/limits.py build/cache                       # a full arena and a full key table
python3 tests/expiry.py build/cache                       # expiry, the sweep, eviction under allkeys-lru
python3 tests/session.py build/cache                      # INFO/HELLO/CLIENT/CONFIG/QUIT; redis-py and ioredis connecting
python3 tests/fuzz_server.py build/cache                  # hostile bytes
```

Benchmarks (reported, with the gate in the design doc):

```sh
python3 bench/memory.py build/cache                       # bytes per key against Redis
python3 bench/hitrate.py build/cache                      # hit rate on a Zipf workload at equal resident memory
python3 bench/stall.py build/cache 64                     # the worst-case pause under eviction
bench/vs_redis.sh -t set,get -P "1 16"                    # against redis-server, one core each
```

## Documentation

- [`docs/design.md`](docs/design.md): the claim, the pre-registered gate, the command set, the design, and one section for each
  step built (C0 to C2, the compaction pause, connecting real clients, incremental compaction) with its measurements.

## Layout

```
src/cache.ls      the loop: one poller, one slab of connections
src/resp.ls       the RESP parser
src/commands.ls   the commands
src/session.ls    HELLO, AUTH, CLIENT, INFO, CONFIG, COMMAND, QUIT
src/reply.ls      reply helpers shared by the two
src/store.ls      the memory: arena, key table, expiry, eviction, compaction
tests/            unit tests (lex-sys) and harnesses (Python, with Redis as the oracle)
bench/            memory, hit rate, stall, and the comparison against Redis
```

## Limitations

Strings only. No persistence, replication, TLS, pub/sub, scripting or `MULTI`/`EXEC`. One database. Up to 256 connections and
a 16 KiB command (both fixed). `CONFIG SET` is refused: the settings are fixed at start.

## Contributing

Every change goes through what CI runs: `$LEX_SYS fmt --check src tests`, the unit tests, the authority check, and the
differential, limits, expiry, session and fuzz harnesses above. Design before code, in `docs/`, with claims measured; a claim
that turns out false is corrected in place.

## Licence

[EUPL-1.2](LICENSE).
