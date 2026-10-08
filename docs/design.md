# cancho-cache: a Redis-compatible cache in cancho

Status: **C0, C1 and C2 built** (sections 8, 9, 10); compaction and the client handshake improved in sections 11 and 12. The numbers in section 2 are measurements of Redis; section 8's are of this project.

## 1. What this is for, and the claim it must survive

A byte-string key/value cache that speaks enough of RESP2 for `redis-benchmark` and a normal client to use it, built on
cancho with no `Ffi`, no `unsafe`, and an authority report (`cancho authority`) that a reader can check. The performance
claim is deliberately modest: **at one core, not meaningfully slower than Redis.** If it is slower, this document says so
and the project stops being "a faster Redis" and becomes, at most, "a Redis with a checkable authority report".

Why it should be possible at all (measured elsewhere, `cancho/docs/parallelism.md`): cancho's `http.server` loop (epoll,
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
store across threads needs the communication primitive that cancho does not have (`parallelism.md`, T5). One core against
one core is the only fair claim today.

## 3. Correctness gates (these come first)

1. **Differential test.** One command sequence, replayed against Redis and against this cache, replies compared byte for byte
   for the supported subset, in four framings: whole commands, one byte at a time, random splits, and pipelined batches.
2. **No input reaches a trap.** A malformed or hostile frame is answered with `-ERR` and the connection continues or closes;
   it never traps the process. (cancho's own rule: no input may reach a panic.) A mutation-style fuzz over the parser, with
   the corpus kept in the repo.
3. **Memory is bounded** by `maxmemory`, counted by the cache itself, and never exceeded under a SET flood larger than it.
4. **Authority**: `cancho authority` shows `net_in`, `conn_*`, `poll`, `clock`, `heap`, `args` and no `ffi`; CI diffs it.

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

`src/resp.cho` is the parser, `src/cache.cho` the loop (`PING`, `ECHO`, and the errors Redis gives for everything else),
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
2. **No input reaches a trap**: `tests/resp_test.cho` runs the parser over **every byte string of 0 to 6 bytes over eight
   symbols (299,593 of them)** and checks it never traps, never claims more than it was given, and that whatever it accepts
   is self-contained (no shorter prefix is a whole command). `tests/fuzz_server.py` sends 3,000 hostile connections
   (random bytes, flipped bytes in valid commands, valid commands cut and continued) at the running server: it stays up and
   still answers `PING`.
3. **Memory**: nothing is allocated after start (all slabs are sized at start); `maxmemory` has no meaning until there is a
   store (C2).
4. **Authority** (`cancho authority`): `args`, `conn_accept`, `conn_read`, `conn_write`, `err_write`, `heap`, `net_in("")`,
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

`src/store.cho` is the memory: one arena of keys and values and one open-addressing index with backward-shift deletion (no
tombstones), all sized at start, nothing allocated afterwards. `GET`, `SET` (plain: no options yet), `DEL` and `EXISTS` use it. An
overwrite that fits the room a value already has is done in place; one that does not is appended and the old room counted as
garbage, which compaction (C2) will take back. A refusal is `-OOM command not allowed when used memory > 'maxmemory'.` (Redis's
words under `noeviction`), changes nothing, and the server carries on.

**Correctness.** `tests/store_test.cho`: six tests, including 3,000 keys deleted in three orders and 200,000 random operations
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

## 10. C2, built and measured: expiry, eviction, and thirty commands

**What exists.** `src/store.cho` now writes records with an 8-byte header (entry number and size), which lets the arena be walked from the
front: **compaction** slides the live records down in place, and **eviction** under `allkeys-lru` samples five keys and removes the one used
longest ago, reclaiming a sixteenth of the arena at a time so that a compaction is paid for once per sixteenth written. Expiry is lazy (a key
whose time has passed is gone when asked for) and swept (up to a hundred times a second, 256 keys a look, repeating while more than a quarter of
what it looks at has run out: Redis's rule). `src/commands.cho` is the commands: `PING ECHO GET SET` (with `NX XX GET EX PX KEEPTTL`)
`SETNX SETEX PSETEX GETSET GETDEL MGET MSET DEL UNLINK EXISTS TOUCH STRLEN TYPE INCR DECR INCRBY DECRBY EXPIRE PEXPIRE` (with `NX XX GT LT`)
`TTL PTTL PERSIST DBSIZE FLUSHALL FLUSHDB SELECT`: **30 of Redis 7.0's 240**, with Redis's argument checks and error texts, including every
overflow case of 64-bit integers.

**Correctness.**
* `tests/differential.py`: **111 cases + 8 small-index cases**, four framings each, byte-identical to Redis. Five divergences are deliberate and asserted
  as divergent (inline commands, a bulk not followed by CRLF, more than 64 arguments, `EXAT`/`PXAT`/`EXPIREAT`, and `SELECT 1`: one database).
* Mutation: **nine broken command layers** were each caught (a `TTL` that rounds down, an overflow check off by one, `INCR` accepting `+7`, `GT` on a key
  with no expiry, `NX` ignored, `SET ... GET` replying before the write, `KEEPTTL` ignored, `MSET` with an odd argument count, `PERSIST` that does not
  persist); **twelve broken stores** (compaction without the offset check, without re-pointing, never compacting, evicting the newest, the expiry boundary
  off by one, no use stamp, and others) were each caught by `tests/store_test.cho` (12 tests, including a model that knows exactly when a `SET` must be
  refused and a randomised run that checks every value after every compaction).
* `tests/expiry.py`: lazy and swept expiry against Redis in real time, `PTTL` ranges, and a 1 MiB cache under `allkeys-lru` written to 20 times its size
  that never refuses a write, stays about a MiB, keeps a key that is used all the time, and drops the oldest. `tests/fuzz_server.py` now sends 6,000 hostile
  connections including every new command with hostile arguments.

**What the tests found in the code** (kept here because it is why they exist): the sweep, as first written, scanned 64 keys a turn and took **about 40% of
2,000 expired keys in 1.5 seconds** (the timed test said so; it is now time-based and adaptive and takes them all); and growing the *only* key in a full
arena is refused rather than evicting itself (a test pins it, and the mutant without the guard is caught).

**Throughput, the gate again with all of it in** (`bench/vs_redis.sh`, one core each, five interleaved rounds, medians, default one hot key):

| | Redis | cache | ratio |
|---|---|---|---|
| SET, pipeline 1 | 153,610 | 181,719 | **1.18** |
| SET, pipeline 16 | 999,001 | 1,992,032 | **1.99** |
| GET, pipeline 1 | 147,951 | 181,752 | **1.23** |
| GET, pipeline 16 | 998,004 | 1,996,008 | **2.00** |

With a 100,000-key keyspace: SET 1.07 (pipeline 1) and 1.25 (16), GET 1.07 and 1.25. **One cell of that run was discarded**: the first `SET` pipeline-1 cell ran at half speed
for both servers (75k against 72k) because something else was loading the machine, and the script's own headroom check flagged it; rerun alone it is 133,156
against 142,796, 1.07. Expiry, the clock read each turn and eviction cost nothing visible against Redis; the cache still does much less (no persistence,
replication, statistics).

**Memory** (`bench/memory.py`, 500,000 keys of 16+100 bytes, sizes configured to the data): Redis `used_memory` 200.5 bytes a key, resident 111.6 MiB loaded
and 12 MiB idle; the cache **103.1 MiB loaded** (216 bytes a key at this size) and **between 69 and 96 MiB idle**: the arena is committed when it starts, Redis
grows as it is filled. At full utilisation the cache is about 8% smaller; at half full it is larger.

**Hit rate** (`bench/hitrate.py`, Zipf 0.99 over 2,000,000 keys, 100-byte values, cache-aside, three million requests counted): Redis `maxmemory 128mb allkeys-lru`
**0.8583** at 138.4 MiB resident; the cache with a 76 MiB arena **0.8577** at 136.3 MiB. At equal resident memory they are indistinguishable. Their eviction is not the same algorithm
(both sample five; Redis keeps a candidate pool and a coarser clock) and the comparison is at equal resident size precisely because the two count memory differently.

**The weakness, measured** (`bench/stall.py`): compaction is a pass over the whole arena. On a 64 MiB arena, 200,000 `SET`s past full give a median of 0.035 ms and a 99.9th
percentile of 0.231 ms, **but a maximum of 114.7 ms**, and 18 commands over 2 ms; on 16 MiB the maximum is 54.2 ms. Redis has no such pause. It is dominated by a byte-at-a-time
copy (about 0.5 GB/s) because cancho has no memmove primitive; two ways out, neither built: a `copy_within` builtin (a language change, one small, and measurable), or compaction
that moves a bounded amount per turn (a design change).

**Still not Redis, stated.** 30 commands of 240; no lists, hashes, sets, sorted sets, streams, pub/sub, scripting, transactions; no persistence, replication, cluster, `AUTH`, TLS, RESP3 or
`HELLO`/`CLIENT`/`INFO`/`CONFIG` (so a client library that insists on a handshake may not connect); no `EXAT`-style absolute times; one database; at most 64 arguments and a value
that fits a 16 KiB input buffer; a constant hash seed; one core. Compaction pauses of up to ~115 ms at 64 MiB.

## 11. The compaction pause, after `copy_within`

Section 10 measured the weakness: a worst-case pause of 114.7 ms on a 64 MiB arena, from a byte-at-a-time copy (cancho had no block move). cancho now has
`copy_within(buf, dst, src, n)`, a bounds-checked `memmove` inside one slice (`cancho/docs/memory-moves.md`, cancho #186), and compaction uses it.

| arena | before | after | SETs over 2 ms, of 200,000 (before / after) |
|---|---|---|---|
| 64 MiB | max **114.7 ms** | max **42.4 ms** | 18 / 25 |
| 16 MiB | max 54.2 ms | max 32.2 ms | 32 / 40 |

The median (0.035 ms) and 99.9th percentile (0.23-0.40 ms) did not change; the count over 2 ms moved the wrong way by amounts within the run-to-run noise of this
loopback, one-at-a-time measurement. **The maximum fell by 2.7x and is still tens of milliseconds**, because the copy was never all of it: the compaction walks every record, reads its header,
and looks its entry up in `meta`, in an order (the arena's) that is unrelated to the entries' numbers, so each of about 630,000 records is a cache miss. What would fix that is not a faster copy:
compaction that does a bounded amount per turn, or eviction that frees whole segments so that nothing needs to move. Section 13 builds the first.

## 12. Connecting real clients

C2 spoke to `redis-cli`. A client *library* says more when it connects, and testing three of them showed the cache did not work with the ones people use:

* **ioredis** sends `INFO` as a "ready check" and waits for the answer: against the C2 cache it never became ready (`ERR unknown command 'info'`).
* **redis-py 8** speaks RESP3 by default and sends `HELLO 3` on connect: the cache refused it, so a default `redis.Redis(...)` could not connect.
* Both also call `CLIENT SETNAME`/`SETINFO` (`connectionName`, library name) and sometimes `CONFIG GET`, `AUTH`, `COMMAND`.

What was built (`src/session.cho`, with `src/reply.cho` holding the helpers `src/commands.cho` shares with it): `HELLO` (2 and 3, `AUTH`, `SETNAME`), `AUTH`, `CLIENT ID|GETNAME|SETNAME`, `INFO`
(five sections; `redis_version:7.0.15` is what clients read to decide which commands to try, and the command set mirrors 7.0, so that is what it says; `server_name:cancho-cache` says what this is), `CONFIG GET|SET|RESETSTAT`,
`COMMAND|COUNT|LIST`, `QUIT`, `RESET`: **38 commands**. **RESP3** is per connection and covers what this server produces: the null (`_`), and a map for `HELLO` and `CONFIG GET`; integers, errors and strings are the same in both.

**Checks.** `tests/differential.py` is at **131 cases + 8 small-index cases**, four framings each, byte-identical to Redis, now including every `AUTH`/`HELLO`/`CLIENT`/`CONFIG GET` error text and answer and six RESP3 cases (the connection's id, which no two servers share, is normalised out
of `HELLO`'s answer). Its first run found three texts I had guessed wrongly (`NOPROTO unsupported protocol version`, `AUTH` with too many arguments is a syntax error, and a `CONFIG GET` pattern with no wildcard is echoed as typed). `tests/session.py`
checks what Redis cannot arbitrate: `COMMAND COUNT` equals `COMMAND LIST` and every listed command is answered; `INFO`'s shape and that its numbers move; ids unique and increasing; names per connection; `QUIT` closes; replies that follow a long
in-place-written one are intact (with a client that checks the CRLF after every bulk string: **an earlier version skipped those two bytes blindly, and a mutant that dropped the last one survived**); and **redis-py (RESP2, RESP3 and its default) and ioredis** connect and get through a session. Nine mutants of the session layer
were each caught. The gate benchmark is unchanged (1.23, 1.99, 1.18, 1.50 against Redis on the four cells).

**Deliberate divergences, in the harness's known list:** `CONFIG SET` is refused (the settings are fixed at start); `CONFIG GET` knows ten settings with this server's own values (`maxmemory` is the arena, `databases` is 1);
`COMMAND` and `COMMAND COUNT`/`LIST` describe 38 commands and keep no table of flags and key positions; `CONFIG GET` with several patterns answers in pattern order where Redis uses its hash table's (and its order differs between runs, so it
cannot be asserted either way); `AUTH` accepts the `default` user with any password and refuses any other, as a Redis with no password does.

**Found, then built in section 14: `MULTI`/`EXEC`.** redis-py's `pipeline()` is a transaction by default and sends `MULTI`, so `r.pipeline()` fails against this server (`pipeline(transaction=False)` works, and is what the test uses). Transactions were declared a non-goal; this is the case for
reconsidering a minimal `MULTI`/`EXEC` (queue per connection, run at `EXEC`), which is a design question (what is queued, what a syntax error inside it does) and not built.

## 13. Incremental compaction

The pause of section 11 is a pass over the whole arena in one command. It is now a pass in slices.

**How.** `compact_step(st, budget)` looks at up to `budget` records and stops; `cfrom` and `cto` say where it is. Below `cto` are the compacted records, `cto..cfrom` is a
gap nothing points into, and from `cfrom` on are the records not yet looked at. Commands in between see every key where its entry says it is (a record that moves has its entry
re-pointed in the same step), appends still go at `top` and the walk reaches them in the end, and `dead` is the garbage in the compacted part and the part not yet looked at, so a
dropped record is taken off it as the walk passes. `reclaim(st, budget)` is what spreads it out: a compaction in progress carries on; with the arena three quarters used and a
sixteenth of it garbage, one starts; under `allkeys-lru`, with less than an eighth left and not yet a sixteenth of garbage, up to 32 keys are evicted (never the one just stored). It runs
after every successful `set` (64 records) and once per turn of the loop (1,024), and the loop does not sleep while a compaction is part way through. When writes outrun it, the arena fills and
`make_room` finishes what is left in one go, as before; `INFO` counts those (`compactions_forced`) and the longest turn of the loop (`max_turn_ms`).

**Checked.** `store.audit` states the books' invariant: every byte below `top` is a live record, a dead one or the gap, every live entry's record lies inside the arena and outside the gap,
and the number of entries in use is `live`. It runs after every operation of the randomised model test, repeated with 64, 3 and 1 record paid per set (so commands land between the steps of a
compaction: in the one-record run, a good part of the 100,000 operations land mid-compaction), and of a 6,000-set run under `allkeys-lru` that also checks the key just written is there. Six mutants of the new
code were run against the tests (no dead-bytes subtraction, restarting every step from the front, no protection of the key just stored, `make_room` ignoring a compaction in progress,
`clear` leaving one in progress, a gap not counted) and each is killed; the first version of the tests let two survive (the protection, and `clear`), and a test was added for each. A seventh, a
mutation that changes nothing (`cto + 0`), was kept as a control and survives, as it should.

**Measured** (`bench/stall.py`, 64 MiB, `allkeys-lru`, 100-byte values, 630,000 records filled and 200,000 more `SET`s, one outstanding at a time over loopback; this VM, which is noisy):

| | before (section 11) | after |
|---|---|---|
| compactions forced by a full arena | all of them | **0** in every run (10 compactions per run, all incremental) |
| the server's longest turn (`max_turn_ms`, 1 ms clock), 6 runs | not measured | 3, 4, 5, 3, 3, 8 ms |
| client-side maximum, 6 runs | 42.4 ms | 4.6, 7.5, 16.2, 17.9, 27.6, 5.4 ms |
| client-side `SET`s over 2 ms, of 200,000 | 25 | 8 to 25 |

Read the client-side rows with the noise floor in mind: a loop of 200,000 `PING`s to the same server, which does no work at all, has a maximum of 5 to 7 ms and 9 to 17 commands over 2 ms on
this machine. Where the client saw 17.9, 27.6 ms the server's longest turn was 3 and 5 ms: those are the machine (scheduling, the client), not the cache. What the server did spend is the
8 ms at most (and the 1 ms clock does not resolve less), against 42 ms before; the median (0.035 ms) and 99.9th percentile (0.23 to 0.40 ms) did not move. **So the worst pause is no longer
a function of the arena: it is what one slice costs.** What this does not show is the cost on a different arena, value size or write rate, or on a quiet machine (the 16 MiB arena was run once: no forced
compaction, the same pattern). An earlier set of runs of this benchmark filled in batches of 2,000 pipelined `SET`s, so the server's own longest turn was the fill's, not the pause's (14 and 31 ms in two of six); the benchmark now fills
in batches of 20, and those figures are not used above.

**Throughput after the change** (`bench/vs_redis.sh`, the section 2 gate, medians of 5 interleaved rounds, pinned cores; no cell client-bound). The gate's single key: SET and GET at pipeline 1 and 16 were
**1.23, 2.00, 1.18, 1.99** times Redis (section 11's run: 1.23, 1.99, 1.18, 1.50; the 1.50 had been a quantised cell, see below). With a 100,000-key keyspace (`-k`): **1.10, 1.25, 1.07, 1.00**, against 1.08, 1.25, 1.07, 1.25 before. The last cell moved
from 1.25 to 1.00 and the raw rounds say why that is not a regression to read into: at pipeline 16 `redis-benchmark` reports a million requests in 0.75, 1.0 or 1.25 seconds, so the rates come out as 1,331,558, 998,004 or 798,722 and nothing between; here Redis's five rounds were 999k, 998k,
997k, 799k, 798k and the cache's 998k, 997k, 999k, 997k, 1,332k, medians 997k and 998k. A step of that size is a quarter of the cell, so the pipeline-16 cells cannot resolve a difference below about 25%: they show that neither server is below 0.9, not how much faster the cache is.
The per-`SET` cost of `reclaim` when nothing needs reclaiming is two comparisons.

Not built: the slice sizes (64 and 1,024 records) and thresholds (three quarters used, a sixteenth garbage, an eighth left) are the first values tried, not tuned; and `CONFIG RESETSTAT` is still an
acknowledgement that resets nothing, so `max_turn_ms` covers the whole run.

## 14. `MULTI`, `EXEC`, `DISCARD`, `WATCH`, `UNWATCH` (epic #8, issue #9)

Status: **built**; sections 14.1 to 14.4 are the design as it stood before the code, and 14.5 says what building it found.

**The claim.** A client library's transaction (`redis-py` `pipeline()` in its default mode, `ioredis` `multi().exec()`, a `WATCH`-based check-and-set loop) works, with every reply byte-identical to Redis 7.0.15, at no cost to `GET`/`SET` outside a transaction.

**Why it is cheap here.** The loop is one thread and every command runs to completion, so `EXEC` is atomic by construction: no other connection's command can run between two queued ones. What is left is bookkeeping per connection, in fixed memory.

### 14.1 What is queued, and where

- Per connection, a `txn` row of 24 ints (in `MULTI`, dirty, bytes queued, commands queued, watched count, the flush epoch at `WATCH`, and up to eight watched `(bucket, version)` pairs) and a queue of `input_size()` (16 KiB) bytes in a slab of its own: 256 connections cost 4 MiB, fixed at start, nothing allocated afterwards.
- A queued command is stored as the **bytes it arrived as** (the whole RESP array) and parsed again at `EXEC`. No second representation of a command exists to disagree with the first.
- If a command does not fit in what is left of the queue, the reply is `-ERR transaction queue is full` and the transaction is marked dirty, so `EXEC` answers `-EXECABORT`. **Deliberate divergence:** Redis has no such bound (it is bounded by memory).

### 14.2 What is checked when a command is queued, and what at `EXEC`

Redis refuses at queue time an unknown command and a wrong argument count, answers the usual error (not `+QUEUED`), and marks the transaction dirty, so that `EXEC` replies `-EXECABORT Transaction discarded because of previous errors.` and runs nothing. Everything else (a syntax error in `SET k v EX`, `INCR` on a non-integer, a value out of range) is found by the command itself at `EXEC`, answered **as that element of the reply array**, and does not stop the others.

The queue-time check needs the arity of every command without running it. `commands.arity(name)` is a table (positive: exactly; negative: at least), covering the 38 commands and the transaction commands. It is a second place that knows an arity; issue #10 replaces it with the `COMMAND` table that the dispatcher and `COMMAND INFO` will both read, and until then a test (14.4) compares it with what the dispatcher answers for every command at every count from 0 to 4.

Executed at once, never queued: `MULTI` (answers `-ERR MULTI calls can not be nested`), `EXEC`, `DISCARD`, `WATCH` (`-ERR WATCH inside MULTI is not allowed`), `QUIT`, `RESET` (which also discards the transaction and the watches). Every other command is queued, `HELLO`, `AUTH` and `CLIENT` included.

### 14.3 `WATCH`: a version for each bucket of keys

`EXEC` must abort if a watched key was modified since `WATCH`. The store gains a table of 4,096 version counters indexed by the low bits of a key's hash; every modification of a key bumps the counter of its bucket: a `SET` (even of the same value: Redis signals it too), a deletion, an expiry change, an eviction, and the removal of an expired key; `FLUSHALL`/`FLUSHDB` bump an epoch that every watch also records. `WATCH` records `(bucket, version)`; `EXEC` compares.

- **Cost.** One increment on the write paths, none on `GET`. Memory: 32 KiB, fixed.
- **It can abort when it should not**: two keys sharing a bucket (1 in 4,096 for a pair), and a client that retries is the correct reaction, as it must be to a real abort. It never fails to abort when it should. **Deliberate divergence**, stated in the README.
- **A key that expires after `WATCH` aborts `EXEC`.** Redis 7 does not abort for a key that was already past its time when it was watched; this server cannot tell the two apart and aborts. Not asserted either way by the harness.
- **At most eight watched keys** per connection, then `-ERR too many watched keys`. **Deliberate divergence** (Redis has no limit); `WATCH` of the same bucket twice counts once.
- An aborted `EXEC` answers a null array (`*-1` in RESP2, `_` in RESP3) and clears the watches; so does a successful one, `DISCARD`, `UNWATCH`, `RESET` and a disconnect.

### 14.4 Gate, fixed before building

1. `tests/differential.py`: every transaction case that fits one connection, four framings each, byte-identical to Redis: `MULTI`/`EXEC` of several commands, an empty transaction, nesting, `EXEC`/`DISCARD` without `MULTI`, an unknown command and a wrong arity inside (`EXECABORT`), a runtime error in the middle (the others still run), a syntax error inside a command, `WATCH` with no change, `UNWATCH`, `WATCH` inside `MULTI`, `RESET` inside `MULTI`, RESP3 replies, and a `SET`/`GET`/`DEL`/`INCR` mix inside one transaction.
2. `tests/transactions.py` (new): what one connection cannot show, with a second: a write to a watched key from another connection aborts `EXEC`; a write to another key does not; `FLUSHALL` from another connection aborts; an expiry of the watched key aborts; `redis-py` default `pipeline()` and a `WATCH` check-and-set loop; `ioredis` `multi().exec()`; and the arity table against the dispatcher.
3. **Mutants, each caught:** commands run as they arrive; `WATCH` ignored; `EXEC` continues after a dirty queue; a runtime error aborts the rest; the queue is not cleared by `DISCARD`; a queue that leaks into the next `MULTI`; watches not cleared by `EXEC`.
4. The throughput gate of section 2 re-run: outside a transaction a command pays one comparison (`in_multi`), which must not move a cell.
5. README: remove `MULTI`/`EXEC` from the limitations, state the three divergences above, update the command count.

### 14.5 Built, and what building it found

`src/txn.cho` (the five commands and the queue), the `EXEC` loop in `src/cache.cho`, `commands.arity`, and in `src/store.cho` the version table and epoch (bumped in `put_key`, `remove_entry`, `set_expiry` and `clear`). `COMMAND COUNT` is 43.

**The gate of 14.4, as run (Redis 7.0.15):**

1. `tests/differential.py`: **334 cases + 8 small-index cases**, four framings each, byte-identical (131 before this section; the 203 new ones are the 48 transaction cases above and a sweep of the arity of 30 commands at 0 to 4 words, queued, plus five commands with no words). The sweep compares the arity table against Redis's for every command, which is the check 14.2 promised.
2. `tests/transactions.py`: 12 two-connection scenarios compared with Redis step by step (a write by another connection to the watched key, the same value, a `DEL`, an `INCR`, an `EXPIRE`, `FLUSHALL` and a key expiring all abort `EXEC`; a write to another key and a read do not; a queued command and another connection's command do not interleave), `EXEC` sending 2.4 MB of answers (more than the 2.2 MB scratch buffer) in pieces and in order, and `redis-py` (default `pipeline()`, a bounded `WATCH` loop that retries after a competing write, `WatchError`, an unknown command in the queue, a RESP3 transaction) and `ioredis` (`multi().exec()` and a watch aborted by a competing writer).
3. `tests/txn_mutants.py`: **16 deliberately wrong caches, every one caught**: the seven of 14.4, and nine more (no mark on an unknown command or a wrong arity, `RESET` leaving the watches, a `DEL`, `EXPIRE`, `SET` or `FLUSHALL` that does not touch the version, an array where Redis answers a null, a watch compared against the wrong bucket, a queued command cut one byte short). It runs in its own workflow, `mutants.yml`, when the layer changes.
4. **Throughput**, `bench/vs_redis.sh`, the section 2 cells, medians of five interleaved rounds, this machine (4 vCPUs), the cache built from `main` (before) and from this change (after), run twice each. The cache's own medians, requests a second, before / after: `SET` at pipeline 1: 181,752 and 190,222 / 181,587 and 181,587; `GET` at pipeline 1: 190,295 and 181,587 / 190,259 and 181,818; `SET` at pipeline 16: 1,329,787 and 1,988,072 / 1,333,333 and 1,329,787; `GET` at pipeline 16: 2,000,000 and 1,992,032 / 1,996,008 and 1,992,032. The ratios against Redis in the four cells, after: 1.23, 1.34, 1.24 and 1.50 (first run); 1.23, 1.33, 1.18 and 1.50 (second). **No cell is below the gate's 0.9, and no change in the cache's own figures is outside the run-to-run spread of the same binary** (the baseline's two runs differ from each other by as much). The pipeline-16 cells are quantised, as sections 9 and 13 say (a million requests in 0.5, 0.75 or 1.0 s), which is why `SET` at pipeline 16 reads 1.33M in one run and 1.99M in the next of the *same* build, and why the `GET` ratio at pipeline 16 reads 1.50 here and 2.00 in the first baseline run: Redis's median fell on a step.

**What building it found, each fixed and each now a case:**

* **A trap on `*0\\r\\n`.** The first version of `txn.handle` read the command's name before checking that there was one; an empty array, which the older `commands.execute` guards against, killed the process. The differential case "empty array *0" (it was there before) found it at once. It is the reason the harness has hostile-input cases, and the check now comes first.
* **`EXEC` with an argument** is not `-ERR wrong number of arguments`: Redis answers `-EXECABORT Transaction discarded because of: wrong number of arguments for 'exec' command` and discards a transaction in progress, in or out of `MULTI`.
* **`RESET` outside a transaction also forgets the watches**; the first version cleared them only inside `MULTI`.
* **A queued `UNWATCH` is run at `EXEC`**, where the dispatcher does not know it: the loop answers it `+OK` itself, and `clear` forgets the watches.
* **redis-py's `transaction()` retries without end**, so a server that always aborts hangs a test that uses it. The test writes the loop out, bounded, and the mutant runner counts a harness that times out as having caught the mutant.

**Not built, stated:** queue-time checks of a command's *arguments* (Redis does none either); `WATCH` of more than eight keys (`-ERR too many watched keys`); a queue larger than 16 KiB; the exact Redis 7 rule for a watched key that was already past its time when watched (this server aborts; neither answer is asserted); `MULTI` inside a `MULTI` marking nothing dirty is as Redis has it. Commands are still not described by a `COMMAND` table with flags: `commands.arity` is a second place that knows an arity, to be replaced by the table of issue #10.

## 15. String commands, slice 1 of issue #10: `APPEND`, `SETRANGE`, `GETRANGE`/`SUBSTR`, `GETEX`, `MSETNX`

Status: **built** (49 commands). The design, then what building it found.

**The claim.** The six commands answer as Redis 7.0.15 does, byte for byte, including every error text, at no cost to `GET`/`SET`, and without a second way for a value to change.

**One write path.** `APPEND` and `SETRANGE` are both `store.splice(key, hash, offset, bytes)`: write `bytes` at `offset` of the key's value, in place if the room the value already has holds the result, and otherwise in a new record at the end of the arena with the old one counted as garbage (as `put_key` does when a value grows). A gap between the old end and `offset` is zeros, the value never gets shorter, an existing key keeps its expiry, and a key that is not there is created (and removed again if the arena cannot hold the result, so a refused write leaves nothing behind). `APPEND` is a splice at the old length. The new record copies the old key and value with `copy_within`, so a value is never copied through a buffer and its size is not bounded by the scratch buffer.

**Semantics taken from Redis's source, each with a differential case:** `SETRANGE` with no bytes to write answers the length and creates nothing; the string is limited, with Redis's error text, before the arena is asked (to 512 MiB (`proto-max-bulk-len`) in Redis; **here to 16 KiB, see 15.1**); `GETRANGE` converts negative indexes, answers an empty string when both are negative and the start is past the end (without that check a pair that both reach before the start would answer the first byte, which a mutant showed the first set of cases did not distinguish), and clamps; `GETEX` parses its options first (`EX`, `PX`, `PERSIST`, no combination of them), then looks the key up (a missing key answers null whatever the expiry argument is), then validates the number, and deletes the key if the new time has already passed; `MSETNX` looks at every key before writing any.

**Not built, and why:** `GETEX ... EXAT|PXAT` and `SET ... EXAT|PXAT` name a time of day, and the cache has a monotonic clock and no calendar (the same known divergence as `EXPIREAT`); they answer `-ERR syntax error`.

**Differences from Redis, stated:**

* **A string is at most 16 KiB** (section 15.1), where Redis allows 512 MiB; a longer result is refused in Redis's words (`string exceeds maximum allowed size (proto-max-bulk-len)`).
* ~~A large offset is one long command~~ (measured: `SETRANGE key 60000000 x` on a 64 MiB arena took 34 ms). **No longer true: the limit above means an offset is at most 16 KiB.** Written first as a stated cost, then removed by the fix of 15.1; the measurement stays here because the next limit to be lifted (issue #11) brings it back, and then it has to be sliced.

**Gate, as run (Redis 7.0.15 as the oracle):**

1. `tests/differential.py`: **426 cases + 8 small-index cases, 0 differ** (92 new: 62 cases of the six commands, three of them the ones that put the zero-fill over old garbage, and the arity sweep of the six commands at 0 to 4 words). The first set of cases passed on the first run and two mutants (below) showed they were too kind: the harness is what is judged, so each survivor became a case.
2. `tests/store_test.cho`: 5 new unit tests of `splice` (in place and by moving; zero gap; a refused write changes nothing and leaves no new key; the expiry is kept; 400 appends to 17 keys in a 2 KiB arena under `allkeys-lru` with the arena's books audited after each).
3. `tests/string_mutants.py`: **16 deliberately wrong caches, every one caught** (one survived at first, which is the case added for the `GETRANGE` pair above). They run in `mutants.yml` with the transaction mutants.
4. Throughput, `bench/vs_redis.sh`, the section 2 cells, medians of five interleaved rounds, two runs, this machine: the cache's own medians were 190,223 and 190,259 (`SET`, pipeline 1), 181,587 and 190,223 (`GET`, pipeline 1), 1,329,787 and 1,329,787 (`SET`, pipeline 16) and 1,996,008 and 1,992,032 (`GET`, pipeline 16): the same as section 14.5's, within the spread between runs of one binary. Ratios against Redis: 1.24, 1.33, 1.18, 1.50 and 1.24, 1.33, 1.19, 1.50. No cell is below the gate's 0.9; the pipeline-16 cells are quantised as sections 9 and 13 say. The new commands sit after `GET`/`SET` in the dispatch, so the hot path gains no comparison.

## 16. The keyspace, slice 2 of issue #10: `KEYS`, `SCAN`, `RANDOMKEY`, `RENAME`, `RENAMENX`, and the matcher they share

Status: **built** (54 commands). The design, then what building it found.

**The claim.** Frameworks that clear a cache by pattern (`KEYS prefix:*`, `SCAN MATCH`) or rename a key into place work, with Redis's own pattern language, and a `SCAN` that never loses a key that is there the whole time.

**The matcher** (`src/glob.cho`) is Redis's `stringmatchlen` for bytes: `*`, `?`, `[abc]`, `[^abc]`, `[a-c]` (either way round), `\x`, a set that is not closed running to the end, a `\` at the end being itself. It uses no recursion: a `*` remembers where it was and a mismatch tries it one byte further on. Redis's code was the specification; the first set of unit tests was checked against a real Redis case by case, and two of my expectations (`[a-]` and `[]a]`) were wrong, which is why 4,000 random patterns over random keys are also compared with Redis (`tests/keyspace.py`).

**The cursor is an entry number.** An entry keeps its number from the `set` that made its key until the key is removed, through every compaction and every move of its record (`store.cho`, the `meta` table is indexed by it). So `SCAN cursor COUNT n` looks at the entries `cursor..cursor+n`, answers the visible ones that match, and answers the entry to go on from, 0 at the end. A key there from the first call to the last is therefore answered **exactly once** (Redis promises at least once); a key added or removed in between may or may not be. A key whose time has passed is not visible even if nothing has swept it. No allocation, no per-scan state.

**`RENAME` re-keys the entry** instead of copying it through a buffer: a new record at the end of the arena with the new name, the value copied with `copy_within`, the old record counted as garbage, the old name taken out of the index (found by the old hash, so before the hash is overwritten) and the new one put in. The expiry travels with the key, whatever was at the new name goes with its expiry, and both names count as modified for `WATCH`.

**Differences from Redis, stated:**

* **Cursors are not Redis's.** A cursor means something only if it is 0 or the server gave it; Redis accepts any number (it masks it), this answers the end for one past the keys. `COUNT` is a number of entries looked at, so `SCAN 0 COUNT 1` on one key answers cursor 0 where Redis answers 2 (its table has four buckets). Neither is a thing a client can rely on, and the byte-for-byte cases avoid them.
* **Order.** Keys come in entry order, not hash order.
* **`KEYS` answers in one piece.** A result of more than about 1 MiB is refused with `-ERR the keys do not fit one reply: use SCAN`, where Redis sends all of it.
* **A scan is one long command.** Measured (this machine, 200,000 keys): `KEYS` with a pattern 7 to 14 ms (Redis 21 to 34 ms), a whole `SCAN COUNT 1000` read to the end 182 ms in 200 calls (Redis 302 ms). `KEYS` over a million keys would pause for about 70 ms: outside the sliced-pause claim of section 13, as it is in Redis, and the reason `SCAN` exists.
* **`RENAME` needs room for a second copy of the value** while it moves (`-OOM` otherwise, and nothing is changed). An arena that is more than half full of one value cannot rename it.
* **`SCAN ... TYPE x`** for a type other than `string` matches nothing, as all keys are strings; unknown type names are not an error here.

**Gate, as run (Redis 7.0.15 as the oracle):**

1. `tests/differential.py`: **486 cases + 8 small-index cases, 0 differ** (60 new: 35 cases of the five commands, which stay with what one connection can show (one key or none, every error text, the effect on `WATCH` and in `MULTI`), and the arity sweep of the five at 0 to 4 words, queued).
2. `tests/keyspace.py`: `KEYS` with 4,000 random patterns over 400 random keys of the special bytes, the sets compared with Redis; `SCAN` read to the end with `COUNT` 1, 7, 100 and 100,000, with `MATCH` and `TYPE`, exactly once for each key, and the same sets as Redis; `SCAN` while keys are deleted and added between the pages (1,500 stable keys, each answered once); a key that has expired is left out by `KEYS`, `SCAN` and `RANDOMKEY`; and 3,000 random `SET`/`DEL`/`APPEND`/`RENAME`/`RENAMENX` against a model on a default cache and on a 1 MiB one (one compaction in it, which is fewer than I wanted; the unit test below is what presses on it).
3. `tests/glob_test.cho` (6 tests), and 5 new in `tests/store_test.cho`: a rename moves the value and the expiry, over an existing key, with no room (nothing changes), 600 renames in a 2 KiB arena under `allkeys-lru` with the arena's books audited after each, and the walk that `KEYS` and `SCAN` make.
4. `tests/keyspace_mutants.py`: **17 deliberately wrong caches, every one caught** (one survived at first: a key that has run out can only be arranged in a unit test, since a live server sweeps it, so that mutant is judged there).
5. Throughput, `bench/vs_redis.sh`, the section 2 cells, medians of five interleaved rounds, two runs (the machine ran about 5% faster than in section 15's runs, Redis included, so the ratios are what compares): the cache's own medians were 199,960 and 199,880 (`SET`, pipeline 1), 199,840 and 199,760 (`GET`, pipeline 1), 1,996,008 and 1,992,032 (`SET`, pipeline 16) and 1,992,032 and 1,992,032 (`GET`, pipeline 16). Ratios against Redis: 1.20, 2.00, 1.20, 1.50 and 1.20, 2.00, 1.15, 1.50. No cell is below the gate's 0.9; the pipeline-16 cells are quantised as sections 9 and 13 say. The new commands sit after `GET`/`SET` in the dispatch, so the hot path gains no comparison.

**What building it found.** `SCAN` cursors: the first version took any 20-digit number, and Redis refuses one past 18446744073709551615 (`invalid cursor`), found by the differential case; the other differences it showed (above) are Redis's cursor semantics and not bugs, and the cases that compared them were replaced by ones that can be compared.

**Not built:** `COPY`, `OBJECT`, `MEMORY USAGE` and `INCRBYFLOAT`, and the `COMMAND` table with flags (issue #10 stays open for them).

### 15.1 A crash found after the merge, and the limit that fixes it

**What happened.** Section 15 (and PR #31) let `APPEND` and `SETRANGE` build strings up to 512 MiB, "limited by the arena". But every answer is built in one scratch buffer whose size, `3 * reserve()`, rests on a value never being longer than a command can carry (16 KiB, the input buffer): `reserve()` is `MGET` of the most keys with such values. A `GET` of a 4 MB value (made with `SETRANGE key 3999999 x`) wrote past the scratch buffer, the process trapped, and every connection was lost. It was on `main` from the merge of #31 until this fix. The differential harness could not see it (Redis allows the value and answers it), and nothing else asked for a reply that large: the measurement I made of `SETRANGE` pauses at 60 MB never read the value back.

**The fix** is the smallest that makes the invariant true again: `max_string()` is 16,384, so `APPEND` and `SETRANGE` refuse a longer result with Redis's own words, and no value is longer than `reserve()` assumes. `tests/limits.py` now builds the longest value, checks that one byte more is refused and changes nothing, that a `SETRANGE` at an offset of 4 million is refused and makes no key (the reproducer), and that the largest answers (`MGET` of 63 longest values, `EXEC` of 100 `GET`s of one) come back whole. It fails on the binary of `main` (`SETRANGE one past it is refused: got :16385`) and passes on this one.

**What it does not fix, and why issue #11 matters:** `MGET` takes at most 63 keys (the table of arguments holds 64), a command at most 16 KiB. Both are what #11 is for; this section is the reason that lifting them comes with answers that are written in pieces and not built whole.

## 17. More arguments, and answers in pieces (epic #8, issue #11, slice 1 of 3)

Issue #11 is three limits: arguments (a command took 64, so `MGET` of a hundred keys failed), connections (256) and the size of a value (16 KiB). They are three changes, in this order; this is the first.

**Arguments.** `resp.max_args()` is 2,048 (was 64). A command must still fit the 16 KiB input buffer, and the shortest argument takes seven bytes (`$1\r\nk\r\n`), so about 2,300 is all that can arrive; 2,048 is the power of two below it. It is not 4,096 because the unit tests allocate the table in a small region, which 4,096 (a 64 KiB table) overran: the first run of `tests/resp_test.cho` trapped in all seven tests, and the limit was lowered to what the test regions hold. Redis takes a million arguments; a command here is still at most 16 KiB.

**Answers in pieces.** With 2,048 keys, an `MGET` answer can be 2,048 × 16 KiB, far more than the scratch buffer (`3 * reserve()`). So `MGET` is resumable: `commands.piece()` (64 KiB, the size of the per-connection queue for a slow client) is how much one call may write; it writes the header on its first call, then as many values as fit, and keeps in `link[4]` (state index 9) the key it is at, or 0 when done. The server sends what was written, leaves the command at the same place in the input, and parses it again when the answers have gone; the second call carries on from `link[4]`. `flush_at()` (32 KiB) is the point at which the main loop sends and stops reading further commands, so that what the kernel does not take always fits the queue: this is the back-pressure, and a client that does not read is waited for rather than dropped (the connection is closed only if the queue overflows, as before).

**`EXEC`** runs its queue the same way. `tx[22]` says an `EXEC` is waiting and `tx[23]` is the byte of the queue it has reached; `txn.handle` answers "carry on" for an `EXEC` that is waiting, before the arity check, and the queue is cleared only when it has run to its end. A queued `MGET` can itself stop part way. **A difference from Redis, stated:** an `EXEC` that has to wait for a client that is not reading is not atomic with other connections' commands, because the loop serves them while it waits; an `EXEC` whose answers go out at once still is.

**What building it found.**

* An `EXEC` whose queued `MGET` left `st[p + 9]` non-zero was taken by the generic "this command is part way" test as being part way itself: it ran again and produced a stray `-ERR EXEC without MULTI` (found by `tests/limits.py`, the slow-reader `EXEC` of 20 `MGET`s). Fixed by testing `status == 0` first.
* Two transaction mutants stopped applying when the `EXEC` block was rewritten (the lint, `MUTANTS_LINT=1`, said so: "its text is in cache.cho 0 times"); they were re-written against the new text and are caught.
* My first tests used 64-key `MGET`s, which are over the old limit, and a helper that cannot read arrays.

**Gate, as run (Redis 7.0.15):**

1. `tests/differential.py`: **498 cases + 8 small-index cases, 0 differ**. New: 65, 100 and 1,000 arguments; `MGET` of 64, 100 and 1,000 keys, in RESP3 with misses, and of one key 1,000 times; `DEL`, `EXISTS`, `TOUCH` of 1,000 keys; `MSET` and `MSETNX` of 300 pairs; `MGET` of 500 keys in `MULTI`. A known divergence is moved to the list: `WATCH` of nine keys (the cache watches eight).
2. `tests/limits.py`: longest values; `MGET` of 63, 300 (4.9 MB) and 1,000 keys; two `MGET`s and a `PING` pipelined; a client that does not read for a second; 200 pipelined `GET`s of 16 KiB to a slow reader; `EXEC` of 20 `MGET`s (20 MB) to a slow reader and to a prompt one, each followed by a `PING` that must answer.
3. `tests/pieces_mutants.py`: **9 wrong caches, every one caught** (`MGET` all at once, forgetting where it was, never saying more, part way taken as done, `EXEC` resume taken as part way, header written again on resume, `EXEC` not waiting, `flush_at` huge, `max_args` back to 64); and the lint of all four mutant files.
4. Unit tests: `resp` 7, `glob` 6, `store` 25; `session`, `expiry`, `fuzz_server`, `transactions` and `keyspace` harnesses pass.
5. Throughput, `bench/vs_redis.sh`, two runs: ratios against Redis 1.18, 1.33, 1.18, 1.50 and 1.23, 2.00, 1.18, 1.50 (`SET` and `GET`, pipeline 1 and 16); none below the gate's 0.9. These cells never reach the new code.

**Not done here:** more than 256 connections (slice 2) and values over 16 KiB (slice 3); `MGET` replies are still limited by 16 KiB per value.
