<p align="center"><img src="docs/assets/cancho-cache-logo.png" alt="cancho-cache" width="220"></p>

# cancho-cache

[![ci](https://github.com/alpibrusl/cancho-cache/actions/workflows/ci.yml/badge.svg)](https://github.com/alpibrusl/cancho-cache/actions/workflows/ci.yml)

**A Redis-compatible cache that says what it can do.** Strings and hashes, expiry and six eviction policies over RESP2 and RESP3, a snapshot that survives a restart, and the observability an exporter reads, written in [cancho](https://github.com/alpibrusl/cancho): no `Ffi`, no `unsafe`, and an authority report, checked in CI, that shows no foreign code and no `fs_write` (it can write only inside the one directory `--dir=` gives it, one name at a time; its filesystem row is the unnarrowed `fs_read("")` that directory needs, see "See what it is allowed to do" below). One thread, one poller, a fixed arena sized at start with nothing allocated afterwards. `redis-cli`, `redis-py`, `ioredis`, `node-redis` and `redis-rs` connect and work. The [project page](https://alpibrusl.github.io/cancho-cache/) has the summary.

**Status: alpha. A string-and-hash cache, not a Redis replacement.** It answers 86 of Redis 7.0's 240 commands. Built: strings and hashes (15 `H*` commands), expiry, transactions (`MULTI`/`EXEC`/`WATCH`), six eviction policies, `CONFIG GET` of 14 settings and `CONFIG SET` of the six that can change while it runs, `INFO` with all its sections and `commandstats`/`errorstats`, `SLOWLOG`, `CLIENT LIST`/`INFO`/`KILL`, `LATENCY`, `MEMORY`, `MONITOR`, a static-map cluster-lite (`MOVED`, `CLUSTER SLOTS`), and a snapshot (`SAVE`, `BGSAVE`, `LASTSAVE`, the `save <seconds> <changes>` policy, `SHUTDOWN [SAVE|NOSAVE]`, loaded at start). Not built: lists, sets, sorted sets, streams, pub/sub, scripting, replication, TLS, ACL, more than one database, an append-only log, and the LFU policies. The gaps, each with its design question and gate, are tracked in the [epic](https://github.com/alpibrusl/cancho-cache/issues/8); the open ones are listed under "What is next".

## What you get

* **The same answers as Redis.** Every reply is byte-identical to Redis 7.0.15 for the cases in `tests/differential.py` (959 cases and 8 small-index cases, four framings each), and the replies of `SLOWLOG`, `CLIENT`, `LATENCY`, `MEMORY` and `MONITOR` are compared with Redis by their own harnesses. The deliberate divergences are listed in [`docs/design.md`](docs/design.md) section 12 and in the section of each feature.
* **Speed, measured.** On one core against Redis 7.0.15, about 1.1 times Redis's throughput at pipeline depth 1 and 1.0 to 2.0 times at depth 16 (the cell where either server may run in a faster or a slower mode), with the same hit rate at the same resident memory, while doing much less than Redis does. The ratios are medians of interleaved rounds; Redis itself moves by up to 15% from run to run on a shared VM, and at depth 16 either server may run in a faster or a slower mode, so each re-run in the design says which cells it could and could not separate from noise. The gate was fixed before the code (`docs/design.md` section 2).
* **A bounded pause.** Compaction is incremental: the worst-case pause is a slice of work, not the arena. 3 to 8 ms measured on a noisy VM, against 115 ms before. A background save keeps it: while `BGSAVE` writes about 100 MiB the longest wait of a `PING` was 12 ms (a blocking `SAVE` of the same data stalled everyone for 727 ms), measured in `docs/design.md` section 36.
* **A fixed arena.** Keys and values live in one arena sized at start; expiry (lazy and swept) and six eviction policies (`noeviction`, which refuses with Redis's own error; `allkeys-lru`, `allkeys-random`, `volatile-lru`, `volatile-random`, `volatile-ttl`). `CONFIG SET` changes the policy, `maxmemory-samples`, the slow log and the latency threshold at run time; the arena and the key table are fixed, so `maxmemory` is not settable. The LFU policies are not built.
* **A snapshot.** `--dir=<directory>` keeps `dump.cho` there: `SAVE` writes it crash-safely and blocking; `BGSAVE` writes it a slice a turn without stopping the cache, and every key in that file is whole and was true at some moment of the save, though the file is not one instant (there is no `fork` to copy from; section 36 says exactly what that means). `--save=900:1,300:10` (or `CONFIG SET save`) makes the cache start a `BGSAVE` itself when that many writes and seconds have passed, and `SHUTDOWN` saves before it stops when a policy is set. The next start loads the file before accepting connections, keeping each key's expiry as an absolute time. A file that is truncated, altered or from another version stops the start with a rule tag and loads nothing (`docs/design.md` sections 35 to 37).
* **What an exporter reads.** `INFO` with the sections an exporter reads (a field Redis has and this cache cannot tell is absent, not invented), `commandstats` and `errorstats`, `SLOWLOG`, `CLIENT LIST`/`INFO`/`KILL`, `LATENCY`, `MEMORY`, `MONITOR`, `COMMAND INFO`/`DOCS`/`GETKEYS` from tables generated from Redis itself.
* **An authority you can read.** `cancho authority` lists what the program may do, and CI checks it (below).
* **Real clients.** RESP2 and RESP3, `HELLO`, `AUTH`, `CLIENT`, `INFO`, `CONFIG GET`, `COMMAND`: what a client library sends on connecting; and transactions, so `redis-py`'s default `pipeline()`, its `WATCH`-based `transaction()` and `ioredis`'s `multi().exec()` work.

Every measurement, its caveats and the gate fixed before the code are in [`docs/design.md`](docs/design.md).

## Requirements

- The **cancho** compiler at the revision this repository's CI builds with (below); the revision is part of the contract.
- `clang`, to build that compiler from its seed (`scripts/build-standalone.sh --seed`; no Rust is needed, and the result must reproduce itself byte for byte).
- To run the tests: `redis-server` and `redis-cli` (the oracle of the differential test), `python3` with `pip install redis`, and `node`/`npm` for the `ioredis` and `node-redis` checks.

## Quick start

```sh
git clone https://github.com/alpibrusl/cancho
git clone https://github.com/alpibrusl/cancho-cache && cd cancho-cache

REV=$(sed -n 's/^ *CANCHO_REV: *//p' .github/workflows/ci.yml)    # the revision CI uses
# the compiler written in cancho, built from its seed by clang (no Rust); it must reproduce itself byte for byte
(cd ../cancho && git checkout "$REV" && sh scripts/build-standalone.sh --out "$PWD/standalone" --rev "$REV" --seed)
export CANCHO=$PWD/../cancho/standalone/cancho

mkdir -p build
$CANCHO build --std src/cache.cho src/resp.cho src/store.cho src/commands.cho src/reply.cho src/session.cho src/txn.cho src/glob.cho src/pool.cho src/stream.cho src/cmdinfo.cho src/cmddocs.cho src/cmdkeys.cho src/cluster.cho src/cmdnames.cho src/stats.cho src/slowlog.cho src/clients.cho src/latency.cho src/memory.cho src/persist.cho -o build/cache

build/cache 6379 &                     # port; optional: arena MiB, most keys, eviction policy (noeviction | allkeys-lru | allkeys-random | volatile-lru | volatile-random | volatile-ttl)
redis-cli -p 6379 PING                 # PONG
```

The arguments are `cache <port> [<arena MiB> [<most keys> [<policy> [<seed> [--dir=<directory>] [--cluster=<slot map>]]]]]`, a policy being any of the six above, with 64 MiB, 1,000,000 keys and
`noeviction` by default. Sizes are fixed at start. Without a seed argument the hash seed is read from `/dev/urandom`; a number fixes it, for reproducible tests. The two options are read after the sixth argument, so they need a seed (any number) before them; they are for the snapshot (below) and for cluster-lite (`docs/design.md` section 29).

To keep the data across restarts, add `--dir=/path/to/directory` after the seed: the cache then loads `dump.cho` from it before accepting connections (a file that is not a valid snapshot stops the start, status 4, with a rule tag on standard error), and `SAVE` writes it, whole and crash-safely. Without `--dir`, `SAVE` is an error that says so. `--save=<seconds>:<changes>[,...]` (after the seed, like `--dir`) sets the policy: up to eight pairs, none by default (Redis's default saves; this one does not until told). A command counts as one change however many keys it wrote (Redis counts keys). See `docs/design.md` section 35.

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
$CANCHO authority src/cache.cho src/resp.cho src/store.cho src/commands.cho src/reply.cho src/session.cho src/txn.cho src/glob.cho src/pool.cho src/stream.cho src/cmdinfo.cho src/cmddocs.cho src/cmdkeys.cho src/cluster.cho src/cmdnames.cho src/stats.cho src/slowlog.cho src/clients.cho src/latency.cho src/memory.cho src/persist.cho --std
# performs: args, clock, conn_accept, conn_read, conn_write, dir_read, dir_write, err_write, file_read, file_write, fs_read(""), heap, net_in(""), poll
# never touches: foreign code

(The filesystem row widened twice. First from "never" to one named file, `/dev/urandom`, when the hash seed stopped being a constant: a client that
can choose keys must not be able to pick ones that collide; `--seed N` fixes the seed for reproducible tests and benchmarks, the sixth argument of
`cache`. Then, with the snapshot, to `fs_read("")`: the directory is chosen by an argument (`--dir=`), and `narrow` takes only a literal, so the
report can no longer name it (cancho#330). What the program does is open that one directory with `open_dir`, once, and then work beneath the handle
one name at a time (`dump.cho`, `dump.tmp`), following no link; the report shows `dir_write` and `file_write` and no `fs_write`, because no path is
ever spent to write. It cannot prove which directory that is, and this README no longer says it can. Without `--dir`, nothing is opened but
`/dev/urandom`.)
```

## Commands

86 commands, in groups (`COMMAND LIST` answers the same list):

* **Strings:** `GET`, `SET` (with `NX XX GET EX PX EXAT PXAT KEEPTTL`), `SETNX`, `SETEX`, `PSETEX`, `GETSET`, `GETDEL`, `GETEX`, `MGET`, `MSET`, `MSETNX`, `APPEND`, `SETRANGE`, `GETRANGE` (and `SUBSTR`), `STRLEN`, `INCR`, `DECR`, `INCRBY`, `DECRBY`.
* **Hashes:** `HSET`, `HMSET`, `HSETNX`, `HGET`, `HMGET`, `HDEL`, `HLEN`, `HEXISTS`, `HSTRLEN`, `HGETALL`, `HKEYS`, `HVALS`, `HINCRBY`, `HRANDFIELD`, `HSCAN`.
* **Keys:** `DEL`, `UNLINK`, `EXISTS`, `TOUCH`, `TYPE`, `KEYS`, `SCAN` (`MATCH COUNT TYPE`), `RANDOMKEY`, `RENAME`, `RENAMENX`, `COPY`, `OBJECT` (`ENCODING IDLETIME FREQ`), `DBSIZE`, `FLUSHALL`, `FLUSHDB`, `SELECT` (database 0 only).
* **Expiry:** `EXPIRE`, `PEXPIRE` (with `NX XX GT LT`), `EXPIREAT`, `PEXPIREAT`, `EXPIRETIME`, `PEXPIRETIME`, `TTL`, `PTTL`, `PERSIST`.
* **Transactions:** `MULTI`, `EXEC`, `DISCARD`, `WATCH`, `UNWATCH`.
* **Connection:** `HELLO` (RESP2 and RESP3), `AUTH`, `CLIENT` (`ID GETNAME SETNAME LIST INFO KILL NO-EVICT`), `PING`, `ECHO`, `QUIT`, `RESET`.
* **Server and observability:** `INFO`, `CONFIG` (`GET SET RESETSTAT`), `COMMAND` (`COUNT LIST INFO DOCS GETKEYS GETKEYSANDFLAGS`), `SLOWLOG` (`GET LEN RESET`), `LATENCY` (`LATEST HISTORY RESET DOCTOR`), `MEMORY` (`USAGE STATS DOCTOR PURGE`), `MONITOR`, `SAVE`, `BGSAVE`, `LASTSAVE`, `SHUTDOWN`.
* **Cluster-lite:** `CLUSTER` (`INFO SLOTS SHARDS NODES MYID KEYSLOT`), `READONLY`, `READWRITE`: a static slot map given with `--cluster=`, `MOVED` redirects and `CROSSSLOT`, no gossip and no failover (`docs/design.md` section 29).

The eviction policy is `noeviction` unless the command line or `CONFIG SET maxmemory-policy` says otherwise. Every reply is byte-identical to Redis 7.0.15 for the cases in `tests/differential.py`; the deliberate divergences (inline commands refused, `CONFIG SET` of the settings that are fixed at start refused, a `MEMORY USAGE` in this cache's own bytes, the commands that are not supported refused with an error that names the section that says why) are listed in [`docs/design.md`](docs/design.md) section 12 and in each feature's section.

**Keys** (section 16 of the design): `KEYS` and `SCAN` walk the keys in the order their entries were numbered, not in Redis's hash order, and a `SCAN` cursor is that number, so only 0 and the cursors the server gave mean anything (and `COUNT` is the number of entries looked at). Every key there from the first call to the last is answered once. `KEYS` refuses a result of more than about 1 MiB (`-ERR the keys do not fit one reply: use SCAN`), and `RENAME` needs room for a second copy of the value while it moves.

**Transactions** (section 14 of the design): `EXEC` is atomic because the loop runs one command at a time. A queued command is stored as the bytes it arrived as, in a 16 KiB queue for each connection (a command that does not fit is refused, and `EXEC` then answers `-EXECABORT`); `WATCH` follows up to eight keys, by a version for each of 4,096 buckets of keys, so `EXEC` can abort when an *unrelated* key shares a bucket with a watched one (a client retries, as it must after a real abort), and it never fails to abort when a watched key changed. Redis has none of these three limits.

## Tests

```sh
# unit tests (cancho)
$CANCHO test tests/resp_test.cho src/resp.cho --std        # the parser, over every string up to six bytes
$CANCHO test tests/store_test.cho src/store.cho --std      # the store: collisions, deletion, a random run against a model
$CANCHO test tests/glob_test.cho src/glob.cho --std        # the pattern matcher
$CANCHO test tests/pool_test.cho src/pool.cho --std        # the lent-buffer pools

# against redis-server, the oracle
python3 tests/differential.py build/cache                 # byte for byte, four framings each (959 + 8 cases)
python3 tests/keyspace.py build/cache                     # KEYS patterns, SCAN while keys change, RENAME against a model, COMMAND tables
python3 tests/info.py build/cache                         # INFO sections and fields, the counters the commands decide
python3 tests/slowlog.py build/cache                      # SLOWLOG entries, counts and errors
python3 tests/client_commands.py build/cache              # CLIENT LIST, INFO, KILL
python3 tests/latency.py build/cache                      # LATENCY and MEMORY
python3 tests/monitor.py build/cache                      # MONITOR: what is shown, in what order, to whom
python3 tests/cluster.py build/cache                      # cluster-lite against a real three-node Redis Cluster
python3 tests/transactions.py build/cache                 # WATCH and EXEC across two connections; redis-py, ioredis and node-redis transactions
python3 tests/session.py build/cache                      # INFO/HELLO/CLIENT/CONFIG/QUIT; redis-py, ioredis, node-redis and redis-rs connecting

# on the cache alone
python3 tests/persist.py build/cache                      # the snapshot: round trip, every damaged file, BGSAVE under changes and a kill -9, the order of the calls (strace)
python3 tests/policy.py build/cache                       # the save policy, the changes counter, SHUTDOWN
python3 tests/expiry.py build/cache                       # expiry, the sweep, eviction under every policy
python3 tests/limits.py build/cache                       # a full arena and a full key table
python3 tests/connections.py build/cache                  # 10,000 connections; pools of lent buffers running out
python3 tests/large.py build/cache                        # values of 1 MiB to the whole arena, in and out in pieces
python3 tests/fuzz_server.py build/cache                  # hostile bytes

# deliberately wrong caches, each of which a harness must catch (CANCHO=$CANCHO)
python3 tests/txn_mutants.py                              # 16, transactions
python3 tests/string_mutants.py                           # 17, APPEND, SETRANGE, GETRANGE, GETEX, MSETNX
python3 tests/keyspace_mutants.py                         # 51, the matcher, KEYS, SCAN, RENAME, COPY, expiry, eviction, cluster, INFO counters
python3 tests/pieces_mutants.py                           # 11, answers in pieces and the lent buffers
python3 tests/slowlog_mutants.py                          # 21
python3 tests/clients_mutants.py                          # 22
python3 tests/latency_mutants.py                          # 16
python3 tests/monitor_mutants.py                          # 18
python3 tests/persist_mutants.py                          # 42
python3 tests/policy_mutants.py                           # 33
```

Benchmarks (reported, with the gate in the design doc):

```sh
python3 bench/memory.py build/cache                       # bytes per key against Redis
python3 bench/hitrate.py build/cache                      # hit rate on a Zipf workload at equal resident memory
python3 bench/stall.py build/cache 64                     # the worst-case pause under eviction
python3 bench/bgsave_stall.py build/cache                 # the wait of a client while BGSAVE, and while a blocking SAVE, writes
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
| redis-rs 1.7, rust (cargo build --release in tests/clients/redis-rs) | pass | |

Go, Java and .NET clients (go-redis, Jedis, Lettuce, StackExchange.Redis, hiredis)
and framework smoke tests (Django, Rails, Spring, Laravel) are not run yet: issue
[#13](https://github.com/alpibrusl/cancho-cache/issues/13) tracks adding each, pinned, to this table.

## Learn more

| | |
|---|---|
| [docs/design.md](docs/design.md) | the claim, the pre-registered gate, the command set, the design, and one section for each step built (37 sections, from C0 to the save policy), each with its measurements and what it did not measure |
| [The epic](https://github.com/alpibrusl/cancho-cache/issues/8) | what is left to pair with Redis, one issue per gap |
| [The project page](https://alpibrusl.github.io/cancho-cache/) | the summary, with the numbers |

## What is next

In the order the work is going: an append-only log with `appendfsync` (the last of #21), then a TLS listener (#16). Open and not started: pub/sub (#15), lists and sets (#25), sorted sets (#26), scripting (#27), replication (#23), a tiered near-cache mode (#22), multiple databases (#19), a unix-domain socket (#20), ACL (#45), key-prefix namespaces (#46), compare-and-set (#47), a test clock (#48), and a cancho client package (#44). The fixed limits (#11) are partly lifted (10,000 connections, values up to the arena) and partly not (a 16 KiB command).

## Layout

```
src/cache.cho      the loop: one poller, one slab of connections; MONITOR, SAVE and the commands that look at other connections
src/resp.cho       the RESP parser
src/commands.cho   the commands
src/session.cho    HELLO, AUTH, CLIENT, INFO, CONFIG, COMMAND, QUIT
src/txn.cho        MULTI, EXEC, DISCARD, WATCH, UNWATCH
src/glob.cho       the pattern matcher of KEYS and SCAN MATCH
src/pool.cho       pools of fixed buffer blocks lent to connections
src/stream.cho     the commands that carry a value too long for a block in pieces (SET, SETEX, PSETEX in; GET out)
src/cmdinfo.cho    GENERATED: what COMMAND INFO answers for each command, copied from Redis 7.0 (tests/gen_cmdinfo.py)
src/cmddocs.cho    GENERATED: what COMMAND DOCS answers (the same generator)
src/cmdkeys.cho    GENERATED: the key positions and key specs COMMAND GETKEYS and GETKEYSANDFLAGS use (the same generator)
src/cmdnames.cho   GENERATED: the number of each command, for INFO commandstats, and which commands MONITOR leaves out (the same generator)
src/cluster.cho    cluster-lite: the slot of a key, the static slot map, redirects, the CLUSTER commands
src/stats.cho      the counters of INFO: errors by code, calls by command, the ops-per-second window
src/slowlog.cho    the slow log: what is kept of a slow command, and SLOWLOG GET/LEN/RESET
src/clients.cho    CLIENT LIST, INFO and KILL: the commands that look at other connections
src/latency.cho    the latency monitor: LATENCY LATEST, HISTORY, RESET, DOCTOR
src/memory.cho     MEMORY USAGE, STATS, DOCTOR, PURGE
src/persist.cho    the snapshot: the file format, the save and the load
src/reply.cho      reply helpers shared by the modules
src/store.cho      the memory: arena, key table, expiry, eviction, compaction
tests/            unit tests (cancho) and harnesses (Python, with Redis as the oracle)
bench/            memory, hit rate, stall, and the comparison against Redis
```

## Limitations

Strings and hashes. No lists, sets, sorted sets or streams, no replication, TLS, pub/sub, ACL or scripting; persistence is a snapshot only, no append-only log (`docs/design.md` sections 35 to 37): `SAVE` stalls every client while it writes the arena, `BGSAVE` does not but is not a point in time, and a single value is written whole in one slice, so a value of 100 MiB is one slice that long. One database. A value may be as large as the arena (up to Redis's 512 MiB): `SET`, `SETEX` and `PSETEX` take it in pieces and `GET` sends it in pieces, but only a `SET` whose value is the last argument (so not `SET key value EX 10` with a value over 16 KiB, which is refused as a command too large), and a big `SET` cannot be queued in `MULTI` or shown by `MONITOR`. Every other command refuses a value over 16 KiB with `-ERR value too large for this command` (`GETSET`, `GETDEL`, `GETEX`, `SET … GET`, a `GETRANGE` of more than 16 KiB, an element of `MGET`, the replies of `EXEC`), and `APPEND` and `SETRANGE` cannot build one; Redis has none of these refusals. While a value arrives its old one stays, so replacing a value needs room for both until the last byte is here, and a `GET` of a value that is written, deleted or flushed while it is being sent closes that connection. A transaction queues up to 16 KiB and watches up to eight keys, and `EXEC` may abort when an unrelated key shares a bucket with a watched one. Up to 10,000 connections, served from pools of lent buffers (1,024 partial commands held at once, 256 connections with output waiting, 128 open transactions; past a pool the connection is closed or, for a queue, the command refused), and a 16 KiB command (fixed), which holds up to 2,048 arguments (`MGET` and `DEL` of a thousand keys work); `MGET` and `EXEC` answer in pieces of 64 KiB, so a client that reads slowly is waited for, not dropped, and an `EXEC` that has to wait for its client is no longer atomic with other connections' commands. `CONFIG SET` refuses the settings that are fixed at start (`maxmemory`, the port, ...) with Redis's error shape. `CLIENT LIST` has no `fd` or `laddr` (the standard library offers neither), `CLIENT KILL LADDR` is refused, `LATENCY` has two events and no `GRAPH`/`HISTOGRAM`, and a `MONITOR` watcher that does not read is closed when its 64 KiB of queue fills. Each is an issue in the [epic](https://github.com/alpibrusl/cancho-cache/issues/8) or a note in the section of the design that built it.

## Contributing

Every change goes through what CI runs: `$CANCHO fmt --check src tests`, the unit tests, the authority check, and the differential, limits, expiry, session and fuzz harnesses above. Design before code, in `docs/`, with claims measured; a claim that turns out false is corrected in place.

## Licence

[EUPL-1.2](LICENSE).
