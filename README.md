<p align="center"><img src="docs/assets/cancho-cache-logo.png" alt="cancho-cache" width="220"></p>

# cancho-cache

[![ci](https://github.com/alpibrusl/cancho-cache/actions/workflows/ci.yml/badge.svg)](https://github.com/alpibrusl/cancho-cache/actions/workflows/ci.yml)

**A Redis-compatible cache that says what it can do.** Strings, expiry and an LRU eviction policy over RESP2 and RESP3, written in [cancho](https://github.com/alpibrusl/cancho): no `Ffi`, no `unsafe`, and an authority report, checked in CI, that names the one file it reads (`/dev/urandom`, for the hash seed) and no other, and no foreign code. One thread, one poller, a fixed arena sized at start with nothing allocated afterwards. `redis-cli`, `redis-py`, `ioredis` and `node-redis` connect and work. The [project page](https://alpibrusl.github.io/cancho-cache/) has the summary.

**Status: alpha, a string cache and not a Redis replacement.** It answers 54 of Redis 7.0's 240 commands; there are no data structures beyond strings, no persistence, replication or TLS, and one database. Transactions (`MULTI`/`EXEC`/`DISCARD`/`WATCH`/`UNWATCH`) work, so a default `redis-py` `pipeline()` does, with three deliberate differences (below). The gaps to pairing with Redis, each with its design question and gate, are tracked in the [epic](https://github.com/alpibrusl/cancho-cache/issues/8).

## What you get

* **The same answers as Redis.** Every reply is byte-identical to Redis 7.0.15 for the cases in `tests/differential.py` (334 cases and 8 small-index cases, four framings each). The deliberate divergences are listed in [`docs/design.md`](docs/design.md) section 12.
* **Speed, measured.** On one core against Redis 7.0.15 it is at 1.07 to 2.00 times Redis's throughput on the pre-registered cells, with the same hit rate at the same resident memory, while doing much less than Redis does.
* **A bounded pause.** Compaction is incremental: the worst-case pause is a slice of work, not the arena. 3 to 8 ms measured on a noisy VM, against 115 ms before.
* **A fixed arena.** Keys and values live in one arena sized at start; expiry (lazy and swept) and an approximate LRU (`allkeys-lru`) or `noeviction`, which refuses with Redis's own error.
* **An authority you can read.** `cancho authority` lists what the program may do, and CI checks it (below).
* **Real clients.** RESP2 and RESP3, `HELLO`, `AUTH`, `CLIENT`, `INFO`, `CONFIG GET`, `COMMAND`: what a client library sends on connecting; and transactions, so `redis-py`'s default `pipeline()`, its `WATCH`-based `transaction()` and `ioredis`'s `multi().exec()` work.

Every measurement, its caveats and the gate fixed before the code are in [`docs/design.md`](docs/design.md).

## Requirements

- The **cancho** compiler at the revision this repository's CI builds with (below); the revision is part of the contract.
- Rust, to build that compiler (its `rust-toolchain.toml` pins the toolchain).
- To run the tests: `redis-server` and `redis-cli` (the oracle of the differential test), `python3` with `pip install redis`, and `node`/`npm` for the `ioredis` and `node-redis` checks.

## Quick start

```sh
git clone https://github.com/alpibrusl/cancho
git clone https://github.com/alpibrusl/cancho-cache && cd cancho-cache

REV=$(sed -n 's/^ *CANCHO_REV: *//p' .github/workflows/ci.yml)    # the revision CI uses
(cd ../cancho && git checkout "$REV" && cargo build --release -p cancho)
export CANCHO=$PWD/../cancho/target/release/cancho

mkdir -p build
$CANCHO build --std src/cache.cho src/resp.cho src/store.cho src/commands.cho src/reply.cho src/session.cho src/txn.cho src/glob.cho -o build/cache

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
$CANCHO authority src/cache.cho src/resp.cho src/store.cho src/commands.cho src/reply.cho src/session.cho src/txn.cho src/glob.cho --std
# performs: args, clock, conn_accept, conn_read, conn_write, err_write, fs_read("/dev/urandom"), heap, net_in(""), poll
# never touches: foreign code

(the filesystem row widened from "never" to one named file, /dev/urandom, when the hash seed stopped
being a constant: a client that can choose keys must not be able to pick ones that collide. `--seed N`
fixes the seed for reproducible tests and benchmarks; the sixth argument of `cache`.)
```

## Commands

`GET` and `SET` (with `NX XX GET EX PX KEEPTTL`), `SETNX`, `SETEX`, `GETSET`, `GETDEL`, `GETEX`, `MGET`, `MSET`, `MSETNX`, `APPEND`, `SETRANGE`, `GETRANGE` (and `SUBSTR`), `INCR`, `DECR`, `KEYS`, `SCAN` (`MATCH COUNT TYPE`), `RANDOMKEY`, `RENAME`, `RENAMENX`,
`INCRBY`, `DECRBY`, `DEL`, `EXISTS`, `TYPE`, `STRLEN`, `DBSIZE`, `FLUSHALL`; `EXPIRE` and `PEXPIRE` (with `NX XX GT LT`),
`TTL`, `PTTL`, `PERSIST`; and what a client library says on connecting: `HELLO` (RESP2 and RESP3), `AUTH`, `CLIENT`, `INFO`,
`CONFIG GET`, `COMMAND`, `QUIT`, `RESET`; and transactions: `MULTI`, `EXEC`, `DISCARD`, `WATCH`, `UNWATCH`. The eviction policy is `allkeys-lru`. Every reply is byte-identical to Redis 7.0.15
for the cases in `tests/differential.py`; the deliberate divergences (inline commands refused, `CONFIG SET` refused, a
`COMMAND` that keeps no table of flags) are listed in [`docs/design.md`](docs/design.md) section 12.

**Keys** (section 16 of the design): `KEYS` and `SCAN` walk the keys in the order their entries were numbered, not in Redis's hash order, and a `SCAN` cursor is that number, so only 0 and the cursors the server gave mean anything (and `COUNT` is the number of entries looked at). Every key there from the first call to the last is answered once. `KEYS` refuses a result of more than about 1 MiB (`-ERR the keys do not fit one reply: use SCAN`), and `RENAME` needs room for a second copy of the value while it moves.

**Transactions** (section 14 of the design): `EXEC` is atomic because the loop runs one command at a time. A queued command is stored as the bytes it arrived as, in a 16 KiB queue for each connection (a command that does not fit is refused, and `EXEC` then answers `-EXECABORT`); `WATCH` follows up to eight keys, by a version for each of 4,096 buckets of keys, so `EXEC` can abort when an *unrelated* key shares a bucket with a watched one (a client retries, as it must after a real abort), and it never fails to abort when a watched key changed. Redis has none of these three limits.

## Tests

```sh
$CANCHO test tests/resp_test.cho src/resp.cho --std        # the parser, over every string up to six bytes
$CANCHO test tests/store_test.cho src/store.cho --std      # the store: collisions, deletion, a random run against a model
python3 tests/differential.py build/cache                 # byte-for-byte against redis-server, four framings each
python3 tests/limits.py build/cache                       # a full arena and a full key table
python3 tests/expiry.py build/cache                       # expiry, the sweep, eviction under allkeys-lru
python3 tests/session.py build/cache                      # INFO/HELLO/CLIENT/CONFIG/QUIT; redis-py, ioredis and node-redis connecting
python3 tests/fuzz_server.py build/cache                  # hostile bytes
python3 tests/transactions.py build/cache                 # WATCH and EXEC across two connections; redis-py, ioredis and node-redis transactions
CANCHO=$CANCHO python3 tests/txn_mutants.py               # 16 deliberately wrong caches, each of which must be caught
CANCHO=$CANCHO python3 tests/string_mutants.py            # 16 more, for APPEND, SETRANGE, GETRANGE, GETEX and MSETNX
python3 tests/keyspace.py build/cache                     # KEYS patterns and SCAN against Redis, SCAN while keys change, RENAME against a model
CANCHO=$CANCHO python3 tests/keyspace_mutants.py          # 17 more, for the matcher, KEYS, SCAN and RENAME
$CANCHO test tests/glob_test.cho src/glob.cho --std       # the pattern matcher
```

Benchmarks (reported, with the gate in the design doc):

```sh
python3 bench/memory.py build/cache                       # bytes per key against Redis
python3 bench/hitrate.py build/cache                      # hit rate on a Zipf workload at equal resident memory
python3 bench/stall.py build/cache 64                     # the worst-case pause under eviction
bench/vs_redis.sh -t set,get -P "1 16"                    # against redis-server, one core each
```

## Client matrix

Each client's battery runs in CI and the table below is what it produced; `tests/clients_matrix.py`
prints the same table from a fresh run, and CI checks the two agree, so the README cannot claim a
client the run did not pass. A client whose runtime is missing is reported as skipped by
`tests/session.py`, never as passing.

| Client | Result | Notes |
|---|---|---|
| redis-cli | pass | the oracle's own client; the differential and keyspace harnesses |
| redis-py (pip install redis; RESP2, RESP3, default) | pass | |
| ioredis 5, node (npm install ioredis in tests/clients) | pass | |
| node-redis 4, node (npm install redis in tests/clients) | pass | |

Go, Java, Rust and .NET clients (go-redis, Jedis, Lettuce, redis-rs, StackExchange.Redis, hiredis)
and framework smoke tests (Django, Rails, Spring, Laravel) are not run yet: issue
[#13](https://github.com/alpibrusl/cancho-cache/issues/13) tracks adding each, pinned, to this table.

## Learn more

| | |
|---|---|
| [docs/design.md](docs/design.md) | the claim, the pre-registered gate, the command set, the design, and one section for each step built (C0 to C2, the compaction pause, connecting real clients, incremental compaction) with its measurements |
| [The epic](https://github.com/alpibrusl/cancho-cache/issues/8) | what is left to pair with Redis, one issue per gap |
| [The project page](https://alpibrusl.github.io/cancho-cache/) | the summary, with the numbers |

## Layout

```
src/cache.cho      the loop: one poller, one slab of connections
src/resp.cho       the RESP parser
src/commands.cho   the commands
src/session.cho    HELLO, AUTH, CLIENT, INFO, CONFIG, COMMAND, QUIT
src/txn.cho        MULTI, EXEC, DISCARD, WATCH, UNWATCH
src/glob.cho       the pattern matcher of KEYS and SCAN MATCH
src/reply.cho      reply helpers shared by the two
src/store.cho      the memory: arena, key table, expiry, eviction, compaction
tests/            unit tests (cancho) and harnesses (Python, with Redis as the oracle)
bench/            memory, hit rate, stall, and the comparison against Redis
```

## Limitations

Strings only. No persistence, replication, TLS, pub/sub or scripting. One database. A value is at most 16 KiB, whether it is `SET` or built by `APPEND` and `SETRANGE`. A transaction queues up to 16 KiB and watches up to eight keys, and `EXEC` may abort when an unrelated key shares a bucket with a watched one. Up to 256 connections and a 16 KiB command (both fixed). `CONFIG SET` is refused: the settings are fixed at start. Each is an issue in the [epic](https://github.com/alpibrusl/cancho-cache/issues/8).

## Contributing

Every change goes through what CI runs: `$CANCHO fmt --check src tests`, the unit tests, the authority check, and the differential, limits, expiry, session and fuzz harnesses above. Design before code, in `docs/`, with claims measured; a claim that turns out false is corrected in place.

## Licence

[EUPL-1.2](LICENSE).
