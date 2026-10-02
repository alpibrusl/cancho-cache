# lexsys-cache: a Redis-compatible cache in lex-sys

Status: **C0 and C1 built** (sections 8 and 9); expiry, `INCR` and eviction not yet. The numbers in section 2 are measurements of Redis; section 8's are of this project.

## 1. What this is for, and the claim it must survive

A byte-string key/value cache that speaks enough of RESP2 for `redis-benchmark` and a normal client to use it, built on
lex-sys with no `Ffi`, no `unsafe`, and an authority report (`lex-sys authority`) that a reader can check. The performance
claim is deliberately modest: **at one core, not meaningfully slower than Redis.** If it is slower, this document says so
and the project stops being "a faster Redis" and becomes, at most, "a Redis with a checkable authority report".

Why it should be possible at all (measured elsewhere, `lex-sys/docs/parallelism.md`): lex-sys's `http.server` loop (epoll,
non-blocking, one core) served about 95,000 requests a second with a full JSON parse and schema validation per request.
A RESP command is far cheaper to parse than that, and Redis itself is a single-threaded event loop, so the comparison is the
same *shape* of program. It is not a measurement of the cache; it is the reason the experiment is worth its first week.

## 2. The gate, fixed before the code

**Baseline (this machine, Redis 7.0.15, jemalloc, `--save "" --appendonly no`, server pinned to core 0, `redis-benchmark`
`-c 50 -n 1000000 --threads 2` pinned to cores 2-3, default 3-byte value, three runs):**

| | SET | GET |
|---|---|---|
| pipeline 1 | 142,633 147,863 147,951 | 153,563 147,842 142,816 |
| pipeline 16 | 999,001 998,004 999,001 | 1,333,333 999,001 1,331,558 |

Pipeline-16 figures are quantised (they repeat to the digit) and may be limited by the load generator, not by Redis. Before
any pipeline-16 comparison is quoted, the client cores' CPU use is recorded; a client that is saturated reports **"client-
bound"** and no ratio.

**Criterion (pre-registered): median of at least five runs of the cache, interleaved with Redis in the same session, at least
0.9 times Redis in each of the four cells (SET and GET, pipeline 1 and 16, 3-byte values).** 256-byte values (`-d 256`) are
measured and reported, not gated. If any cell misses, the result says which and by how much, and the README does not say
"as fast as Redis".

**Not compared:** multiple cores. Redis scales by running more processes and clients that shard; a cache that shares one
store across threads needs the communication primitive that lex-sys does not have (`parallelism.md`, T5). One core against
one core is the only fair claim today.

## 3. Correctness gates (these come first)

1. **Differential test.** One command sequence, replayed against Redis and against this cache, replies compared byte for byte
   for the supported subset, in four framings: whole commands, one byte at a time, random splits, and pipelined batches.
2. **No input reaches a trap.** A malformed or hostile frame is answered with `-ERR` and the connection continues or closes;
   it never traps the process. (lex-sys's own rule: no input may reach a panic.) A mutation-style fuzz over the parser, with
   the corpus kept in the repo.
3. **Memory is bounded** by `maxmemory`, counted by the cache itself, and never exceeded under a SET flood larger than it.
4. **Authority**: `lex-sys authority` shows `net_in`, `conn_*`, `poll`, `clock`, `heap`, `args` and no `ffi`; CI diffs it.

## 4. Command set (the first slice)

`PING`, `ECHO`, `GET`, `SET key value [EX s | PX ms] [NX | XX]`, `DEL`, `EXISTS`, `INCR`/`DECR`, `EXPIRE`/`PEXPIRE`, `TTL`/`PTTL`,
`DBSIZE`, `FLUSHALL`, `INFO` (a short fixed reply), and whatever handshake `redis-benchmark` and `redis-cli` send
(`COMMAND`, `CONFIG GET`, `HELLO`: refused with the standard error, as Redis does for a RESP3 request on a RESP2 server).
Everything else answers `-ERR unknown command`. Lists, hashes, sets, sorted sets, streams, pub/sub, scripting, transactions,
persistence and replication are **non-goals** for this project.

## 5. Design

**Protocol: sans-io, like `http.server`.** A RESP2 parser that is a pure function from `(buffer, position)` to
`(command, bytes consumed)`, `need more`, or `error`, with no allocation on the parse of a well-formed array of bulk strings
(it returns offsets into the connection's input buffer). The server loop is `http.server`'s: `wait`, `next`, `respond`, with
the HTTP parser replaced. **Found in C0:** `http.server` itself is not reused (its `Server` and `produce` are HTTP all the way
down); `std.conns` is, as it is, and is generic enough that the loop is about 350 lines of its own. See section 8.

**Storage: a log, not a heap of values.** `std.map` holds `val` values (copyable), and a value here is a byte string of any
length. So the map is an **index** from key to `{offset, length, expires_at}` into a **value log**, one large `Box[[byte]]`
arena written from the head: a SET appends the value (and, for a replaced key, the old record becomes garbage); the map
points at the new one. This is the same idea as a log-structured store, and it buys three things the alternative (one
allocation per value) does not: no per-value allocator metadata, no fragmentation, and **eviction and compaction are the
same operation** (copy the live records of the oldest segment forward, drop the rest). It costs a copy per compaction, which
the benchmark will show or not. `maxmemory` is the arena size plus the index, counted exactly.

**Expiry** is lazy (checked on access) plus a bounded sweep per loop iteration; time comes from `Clock`. No timer thread.

**Eviction** when full: drop the oldest segment (FIFO), or compact it if more than a threshold of it is live. LRU/LFU are
non-goals for the first slice and would be judged against a hit-rate measurement, not assumed.

**One core, one process, one thread.** Scaling is by running several processes on several ports, as for the `users` server.

## 6. Plan, each step with its own gate

| step | what | gate |
|---|---|---|
| C0 | RESP parser + loop answering `PING`/`ECHO`; the differential harness (Redis is the oracle) | parser fuzz clean; differential test green on the subset; `redis-benchmark -t ping` ratio recorded |
| C1 | `GET`/`SET`/`DEL`/`EXISTS` on a fixed-size log and index | the pre-registered cells, measured; each miss reported |
| C2 | expiry, `INCR`, `maxmemory` and eviction | memory gate (3) under a flood; hit-rate on a skewed workload recorded, not gated |
| C3 | whatever the C1 profile says is the cost: only then optimise | each change must move a measured number, or it is reverted |

## 7. What would make this project not worth continuing

* C1 below 0.7 times Redis at pipeline 1, where both sides are syscall-bound and the language should not matter: that would
  mean the loop or the connection table has a cost the HTTP numbers hid.
* A log-structured store that cannot hold the gate under overwrite-heavy load because compaction dominates.
* The parser needing an allocation per command to be correct.

Any of these is written up here, in place, as the result.

## 8. C0, built and measured

`src/resp.ls` is the parser, `src/cache.ls` the loop (`PING`, `ECHO`, and the errors Redis gives for everything else),
over `std.conns` and the poller. One read per wakeup, every whole command in it answered into one scratch buffer, one
write; a client that does not read is no longer read from (its answers queue per connection, and input already buffered is
answered when the queue drains).

**Correctness gates, as met**

1. **Differential**: `tests/differential.py`, 31 cases in four framings each (whole, byte by byte, random splits, twenty
   pipelined copies), replies byte-identical to Redis 7.0.15. **The harness was mutation-tested**: four deliberately wrong
   cache variants (`+PONK`, the 128-byte cut off by one, the byte in `expected '$', got 'x'` replaced, `ECHO` taking more
   arguments) were each caught. Three divergences are *deliberate* and are asserted as divergent so the list cannot go
   stale: inline commands (`PING\r\n` typed into telnet) are refused; a bulk string not followed by CRLF is refused (Redis
   skips two bytes without looking, which is the shape of a request-smuggling ambiguity); and a command has at most 16
   arguments (Redis: a million). One more is known and **not** tested: Redis's unknown-command text stops at a NUL byte inside
   an argument (C `%s`), the cache's does not.
2. **No input reaches a trap**: `tests/resp_test.ls` runs the parser over **every byte string of 0 to 6 bytes over eight
   symbols (299,593 of them)** and checks it never traps, never claims more than it was given, and that whatever it accepts
   is self-contained (no shorter prefix is a whole command). `tests/fuzz_server.py` sends 3,000 hostile connections
   (random bytes, flipped bytes in valid commands, valid commands cut and continued) at the running server: it stays up and
   still answers `PING`.
3. **Memory**: nothing is allocated after start (all slabs are sized at start); `maxmemory` has no meaning until there is a
   store (C2).
4. **Authority** (`lex-sys authority`): `args`, `conn_accept`, `conn_read`, `conn_write`, `err_write`, `heap`, `net_in("")`,
   `poll`; **never touches the filesystem or foreign code**. CI checks the last line.

**Throughput of the loop, against Redis** (`bench/vs_redis.sh`, `PING` as an array of bulk strings, server on core 0, client on
cores 2-3, 50 clients, five rounds interleaved; this is the loop and the parser with no storage, not the gate):

| | Redis 7.0.15 (median) | cache (median) | ratio |
|---|---|---|---|
| pipeline 1 | 152,847 | 190,440 | 1.25 |
| pipeline 16 | 1,332,445 | 1,996,008 | 1.50 |

**What this does and does not say.** It says the loop, the parser and `std.conns` do not cost more than Redis's per-command path
on a command that does nothing. It does **not** say the cache is faster than Redis: `PING` touches no table, and Redis's cost
on `GET`/`SET` is dominated by exactly what `PING` skips. A client that is the limit would make this a statement about
`redis-benchmark`, so the script checks it: the faster server is also run with a wider client (cores 1-3, three threads),
and a cell more than 5% faster that way is reported CLIENT-BOUND with no ratio. None was. (An earlier check, the client's CPU
use, over-reported saturation and was replaced by this.) At pipeline 16 the cache's ceiling on this machine is about 2.4
million `PING` a second with a 3-million-request run, so about 0.4 microseconds a command.

## 9. C1, built and measured: the gate

`src/store.ls` is the memory: one arena of keys and values and one open-addressing index with backward-shift deletion (no
tombstones), all sized at start, nothing allocated afterwards. `GET`, `SET` (plain: no options yet), `DEL` and `EXISTS` use it. An
overwrite that fits the room a value already has is done in place; one that does not is appended and the old room counted as
garbage, which compaction (C2) will take back. A refusal is `-OOM command not allowed when used memory > 'maxmemory'.` (Redis's
words under `noeviction`), changes nothing, and the server carries on.

**Correctness.** `tests/store_test.ls`: six tests, including 3,000 keys deleted in three orders and 200,000 random operations
against a model in plain arrays, and a pair of keys (`c80067`, `c99133`) found by search that collide in length, 31-bit tag *and* home
slot, so that only the byte comparison tells them apart. Mutation-checked: six deliberately broken stores are each caught (the
byte comparison was *not* caught until that pinned pair was added: random keys never collide this way). `tests/differential.py`
now has 56 cases, including seeded random mixes of `SET`/`GET`/`DEL`/`EXISTS` that make values fit, grow past their room and shrink, and
a second cache with a 32-slot index (`cache <port> 64 16`) so that probe runs form and deletion has to shift entries: with the
default million-key index twenty keys never touch each other, and the backward-shift mutant survived the first version of this
harness for exactly that reason. All agree with Redis byte for byte in four framings; five mutants of the storage path are caught.
`tests/limits.py`: a full arena and a full key table refuse in Redis's words, leave old values, and `DEL`/overwrite/`GET`/`PING` still work (three
mutants caught, two of them by the cache trapping and closing the connection).

**The pre-registered gate** (section 2): at least 0.9 times Redis in each of four cells. `bench/vs_redis.sh`, Redis 7.0.15 and the cache
each on core 0, `redis-benchmark` 2 threads and 50 clients on cores 2-3, 3-byte values, five interleaved rounds, medians:

| | Redis | cache | ratio |
|---|---|---|---|
| SET, pipeline 1 | 153,799 | 190,440 | **1.24** |
| SET, pipeline 16 | 998,004 | 1,992,032 | **2.00** |
| GET, pipeline 1 | 153,633 | 181,785 | **1.18** |
| GET, pipeline 16 | 1,329,787 | 1,996,008 | **1.50** |

**All four cells meet the criterion**, and none was flagged client-bound (the faster side was re-run with a wider client, three threads
on cores 1-3, and was not more than 5% faster).

Reported, not gated (the same script; keyspace of 100,000 keys with `-r`, which `redis-benchmark` does not use by default):

| | pipeline 1 | pipeline 16 |
|---|---|---|
| SET, 3-byte values | 1.08 (137,703 vs 148,104) | 1.25 (798,085 vs 999,001) |
| GET, 3-byte values | 1.07 (137,798 vs 148,126) | 1.25 (798,722 vs 999,001) |
| SET, 256-byte values | 1.03 (128,916 vs 133,156) | 1.17 (570,451 vs 665,779) |
| GET, 256-byte values | 1.03 (128,816 vs 133,316) | 1.00 (665,336 vs 665,779) |

**What this does not say.** It does not say the cache is faster than Redis. It says the cache is not slower *while doing much less*:
no expiry, no `SET` options, no memory accounting, no statistics, no keyspace events, no replication or persistence hooks, one
database. Adding expiry and eviction (C2) costs something, and the same criterion will be measured again then. The default benchmark
uses one key, so the table is cache-resident; the 100,000-key runs are the honest ones, and they are 1.00-1.25, not 2.00. At pipeline 16
the cache and `PING` give the same 1.99 million a second, and at 256-byte values several cells read 665,779 on both servers:
**the machine, not either server, is the limit there** (the wider-client check catches the load generator, not the loopback path and the
kernel). Redis was one build on one machine.

**Limits of C1, stated.** A value may be at most what one command fits in the 16 KiB input buffer (a larger one closes the connection
with a protocol error); a command has at most 16 arguments; `SET` has no options; **the arena is never reclaimed** (dead room only grows
until C2's compaction, so an overwrite-heavy workload that grows values eventually answers `-OOM`); the hash seed is a constant, so a hostile
client that can pick keys can make them collide (a per-process seed needs entropy from `Fs`, which the cache does not hold).

