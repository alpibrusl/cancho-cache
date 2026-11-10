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

**Not built, and why:** `GETEX ... EXAT|PXAT` and `SET ... EXAT|PXAT` name a time of day, and the cache had a monotonic clock and no calendar; they answered `-ERR syntax error`. *Corrected in §25: cancho has `clock_unix_ms`, and they are built.*

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

## 17. The client compatibility matrix (epic #8, issue #13, slice 1)

`tests/session.py` already gates redis-py (RESP2, RESP3, default) and ioredis. Slice 1 adds the
third widely-used client library, `node-redis` 4 (`redis` on npm), a per-client battery under
`tests/clients/`, and a matrix harness (`tests/clients_matrix.py`) whose output the README's table
must agree with.

### 17.1 The battery

`tests/clients/node_redis_session.js`: connect and handshake (node-redis 4 sends `HELLO` and its
`CLIENT SETINFO` on connect; both are answered), string commands (`SET`/`GET`/`INCR`/`INCRBY`/
`MGET`), expiry (`SET EX`, `EXPIRE`, `TTL`, `GETDEL`), a pipeline and a `MULTI`/`EXEC` transaction
in the library's own shapes, an error reply surfaced as an exception with Redis's text
(`value is not an integer or out of range`), `CLIENT SETNAME`, a reconnect on a fresh connection,
and the library idiom `sendCommand`. The battery is idempotent (it clears its keys first), so a warm
server cannot make it fail.

One finding: node-redis returns the RESP integer as a JavaScript number where redis-py and ioredis
return booleans for the same reply (`EXPIRE`). The battery pins the library's actual behaviour, not
Redis's, which is the point of a per-client matrix.

### 17.2 The matrix

`tests/clients_matrix.py` starts a fresh server per client and prints one Markdown row per client
that ran, `pass`/`fail`/`not run` with the failure's last line as the note. It then checks the
README's matrix section: a client that passed this run must be recorded as passing there, and the
README may not claim a pass the run did not produce. The harness is the record, the batteries inside
`tests/session.py` (and `tests/transactions.py`) are the gate, so a missing runtime can only skip,
never widen, a claim.

### 17.3 Slice 2: redis-rs

`tests/clients/redis-rs/` is a pinned (`redis = "=1.7.1"`) Rust binary, built by CI before the session
test, that runs the same shape of battery as the node clients: handshake, strings, expiry, a pipeline
and a `MULTI`/`EXEC` transaction in redis-rs's own shape (`pipe().atomic()`), hashes (`HSET`/`HGET`/
`HGETALL`, the first client battery to exercise issue #14's commands), an error reply surfacing as an
`Err` with Redis's text, `CLIENT SETNAME`, and a reconnect on a fresh connection. It is idempotent
against a warm server (it clears its keys first), and it joins `session.py` and the matrix.

Two findings for the matrix's record: redis-rs sends nothing on connect (its handshake is lazy, so
the battery's first command is the handshake), and its pipeline replies must be asked for as tuples
(one value per pipelined command, `ignore()` on the ones not wanted) where redis-py answers a list —
the battery pins the library's actual shapes, as the node batteries do.

Not run yet, tracked in #13's later slices: go-redis, Jedis, Lettuce,
StackExchange.Redis, hiredis, framework smoke tests (Django, Rails, Spring, Laravel), Redis's own
test-suite subset, and the wire-level corpus.

## 18. A random hash seed (epic #8, issue #12)

### 18.1 The gap and the choice of source

The seed is a constant today (`store.open(heap, memory, max_keys, 0, policy)` in `src/cache.cho`), so a
client that can choose its keys can pick ones that collide on length, tag and home slot
(`c80067`/`c99133`, pinned in `tests/store_test.cho`) and degrade the open-addressing index to a
linear scan. Redis randomises its hash seed at start for this reason.

cancho has no randomness builtin, by design (`docs/tls-pure.md` §2.2): the established pattern is
that the caller reads `/dev/urandom` through `Fs` and seeds a generator; cancho-hooks does exactly
this in three places. That is what this server now does at start: it holds `Fs("/dev/urandom")`
(borrowed from the world's `fs`, which it used to release unopened), reads eight bytes, and uses them
as the seed. A sixth argument, `--seed N`, names the seed instead, for reproducible tests and
benchmarks; if the read fails (no `/dev/urandom`), the seed is 0, the old constant, and the server
still starts.

**The authority report, before and after.** Before: `never touches the filesystem`. After: the report
names `fs_read("/dev/urandom")` and nothing else under `performs`; the CI step is widened to assert
exactly that (`fs_read("/dev/urandom")` present, `fs_write` absent), so the claim stays readable and
the widening is to one named file, not to a filesystem. This is the issue's stated trade-off: the
alternative, a randomness builtin, would add a different label for the same reach.

### 18.2 The sampler's PRNG

`draw` (the eviction and `RANDOMKEY` sampler, xorshift in `store.cho`) keeps its own state and stays
deterministic given the seed: it is derived from the seed, not from `/dev/urandom` per draw, so a
`--seed` run is reproducible end to end, and two servers with different seeds evict differently
without reading the OS again. Sharing the entropy per draw would make eviction depend on a resource
the report names and the benchmarks could not fix; deriving it once does not.

### 18.3 Gate

- The pinned collision pair collides at seed 0 (as before) and is checked, by probing the store's
  index, not to collide at the seed this process drew (a regression test that measures the probe
  length, not one that asserts it).
- A `--seed N` run is byte-identical to another `--seed N` run.
- The authority golden changes from `never touches the filesystem` to `fs_read("/dev/urandom")` and
  no `fs_write`; CI asserts the exact shape.
- The throughput gate is unchanged (the seed changes hashes, not the hash's cost; the differential
  harness is seed-independent because both servers see the same commands).

## 19. Hashes (epic #8, issue #14)

### 19.1 Representation: a packed list of pairs, one arena record

A hash is one arena record whose value is a packed sequence of `(field, value)` pairs, in insertion
order (Redis's listpack keeps insertion order too, and `HGETALL`'s order is not promised but is
observed):

    [4-byte field length][4-byte value length][field bytes][value bytes] ...

Lengths are 4-byte so a pair's header is fixed and a field scan is a walk. A field is found by a
linear scan; a hash of `n` fields costs `O(n)` per `HSET`/`HGET`, which is Redis's own listpack
behaviour for small hashes. **No indexed form in slice 1**: the conversion threshold (Redis turns a
listpack into a hashtable at 128 entries by default) needs a second in-arena structure whose
compaction story is its own slice. Slice 1 bounds the cost where the arena already bounds it: a hash
lives in one record, so it can be as large as the arena, and one `HSET` on an `n`-field hash costs
one scan (find) plus one bounded shift (§19.3). The threshold, measured against Redis at equal
resident memory, decides slice 2 (`bench/memory.py` extended with a hash workload).

### 19.2 The type tag: a ninth int in `meta`

`meta` was eight ints a key. It becomes nine: slot 8 is the kind, 0 for a string and 1 for a hash.
`stride()` is the one place the size lives, so `meta`'s allocation and every loop that walks it scale
without other edits. Nothing else encodes a kind: slot 6 stays exactly "in use / free chain" (its -2
is compared in the eviction sampler, the sweep, compaction and `visible`), and the hash's bits stay
the hash's (the index's tag and home slot depend on them). `TYPE` answers `string`, `hash` or
`none`; there is no `OBJECT` command yet, so `OBJECT ENCODING` is not answered at all — stated here
rather than left to look like an oversight. A string command that meets a hash key
(`GET`, `SET`, `INCR`, `APPEND`, `SETRANGE`, `GETRANGE`, `STRLEN`, `GETEX`, `GETDEL`, `GETSET`,
`SETEX`, `SETNX`, `GETRANGE`/`SUBSTR`, and `MGET`'s per-key hole) answers Redis's own
`-WRONGTYPE Operation against a key holding the wrong kind of value` byte for byte, and each hash
command answers it for a string key. Expiry and keyspace commands (`EXPIRE`, `TTL`, `PERSIST`,
`DEL`, `EXISTS`, `TOUCH`, `RENAME`, `KEYS`, `SCAN`, `RANDOMKEY`, `FLUSHALL`) work on any kind, as in
Redis.

### 19.3 Mutation: one primitive, `value_splice`, and what each command costs

The store gains one primitive, `value_splice(st, e, cut_from, cut_to, bytes)`: replace the value's
`[cut_from, cut_to)` with `bytes`, shifting what follows inside the record's room when the lengths
differ, or appending a fresh record when they do not fit — the same three cases `put_key` and
`splice` already have (in place / shifted in place / appended), so compaction, eviction and
backward-shift deletion need no new knowledge of hashes. On it:

* `HSET`/`HSETNX`/`HMSET`: find the field; absent → `value_splice` at the end (one append); present
  → `value_splice` over the old value (one shift of the tail, or a plain copy when the lengths
  match).
* `HDEL`: `value_splice` the pair away (one shift).
* `HINCRBY`/`HINCRBYFLOAT`: read, parse, format (into the 64-byte `tmp`), and splice back.

So a 10,000-field hash does not make one `HSET` a 10,000-field copy: the shift moves only the bytes
after the field, and only when the value's length changed; a compaction that happens to run pays the
arena-slice bound it already has. What slice 1 does not have is the `O(1)` field find of an indexed
form — that is the measured threshold of slice 2.

### 19.4 Field expiry is out of scope

`HEXPIRE` and the rest of the 7.4 field-expiry family are a non-goal for this issue, as the issue
says: per-field expiry doubles the expiry bookkeeping (`meta` slot 4 is per key) and no client the
matrix covers asks for it.

### 19.5 Commands, in the order they land

Slice 1: `HSET`, `HGET`, `HDEL`, `HLEN`, `HEXISTS`, `HGETALL`, `HKEYS`, `HVALS`, `HMGET`, `HMSET`,
`HSETNX`, `HSTRLEN`, `HINCRBY`. Slice 2: `HINCRBYFLOAT`, `HRANDFIELD`, `HSCAN` (the scan needs the
keyspace cursor's `TYPE` filter widened, which is why it is not first), and the indexed form with
its threshold measurement. `RESP3` map replies (`HGETALL`, `HENTER` none; `HGETALL` is a flat array
in RESP2 and a map in RESP3) are answered in both protocols, byte-identical to Redis 7.0.15.

### 19.6 Gate

Differential against Redis 7.0.15 for every command (error texts, arity, `WRONGTYPE` both ways,
RESP2 flat and RESP3 map); a model-based store test as `tests/store_test.cho` has for strings
(insertion order, overwrite, delete, grow past room, compaction moving a hash); mutants of the hash
path caught by the differential and store tests; `bench/memory.py` extended with a hash workload at
equal resident memory; the string-path throughput gate unchanged (`bench/vs_redis.sh`).


### 19.7 Built (slice 1), and what building it found

`HSET`, `HMSET`, `HSETNX`, `HGET`, `HMGET`, `HDEL`, `HLEN`, `HEXISTS`, `HSTRLEN`, `HGETALL`, `HKEYS`,
`HVALS`, `HINCRBY` answer, over the packed record of section 19.1 and the `value_splice` primitive of
19.3, with a ninth `meta` int for the kind (19.2) and `TYPE` answering `hash`. `HEXPIRE` stays out of
scope (19.4); `HINCRBYFLOAT`, `HRANDFIELD` and `HSCAN` are slice 2 (19.5), and the string commands
that meet a hash key answer `WRONGTYPE` (`GET` in slice 1; the rest as they are touched, tracked by
the differential cases).

**Found, and fixed here.**

* The tail shift in `value_splice` had one direction for both cases: copying back to front is right
  when the value grows (the tail moves forward) and wrong when it shrinks (the tail moves back and
  runs over bytes it has not copied yet). The store test's model caught it; the direction is now
  chosen by which way the tail moves.
* `HINCRBY` first spliced the new number's bytes over the old value without rewriting the pair
  header's length (a shorter number left the tail of the old one in the value), then, rewritten to
  delete-and-append, aliased the number's buffer with the splice scratch. The pair is now built whole
  in one region buffer and spliced in once.
* `HSETNX` answered 0 for a field that was absent (its "skip when present" check skipped everything);
  it now sets absent fields and answers 1, and leaves present ones alone.
* `HLEN` shared the merged arity check with `HEXISTS`/`HSTRLEN` and demanded three arguments.

**Not measured yet** (the oracle is CI's, and the gates named in 19.6 run there): the differential
cases for every command above are in `tests/differential.py` (23 new cases), the store test has a
model-based `value_splice` test, and the memory/throughput benches with a hash workload follow when
`bench/memory.py` is extended in slice 2.


### 19.8 Slice 2: `HSCAN`, `HRANDFIELD`, the `WRONGTYPE` net, and `SCAN TYPE`

`HSCAN cursor [MATCH] [COUNT] [NOVALUES]` answers, with the cursor a pair number of the packed record
(the same shape as the keyspace `SCAN`'s entry-number cursor, and the same buffer bound). `HRANDFIELD
key [count [WITHVALUES]]` answers one bulk without a count, distinct fields for a positive count
(clamped to the hash), repeats for a negative one, `WITHVALUES` interleaving and doubling the reply;
its randomness is the store's sampler (`random_below`, the eviction generator, so a `--seed` run is
reproducible). `SCAN ... TYPE` now compares the key's kind, so `TYPE hash` matches hashes and
`TYPE string` strings (a type this server has no key of matches none, exactly as before).

The `WRONGTYPE` net is closed over the string commands that read or scan a value: `GET` (slice 1) and
now `APPEND`, `SETRANGE`, `GETRANGE`/`SUBSTR`, `STRLEN`, `GETEX`, `GETDEL`, `GETSET`, `INCR`, `DECR`,
`INCRBY`, `DECRBY` and `MGET`. `SETNX` and `SETEX`/`PSETEX` need no guard: they never read the old
value (a `SETEX` over a hash replaces it, as in Redis). Seven new differential cases pin all of it,
including `HRANDFIELD`'s clamping, repetition and error texts and `HSCAN`'s cursors.

**Not in this slice, and why:** `HINCRBYFLOAT` needs a float formatter whose output is byte-compared
with Redis's (17 significant digits, trailing zeros trimmed) — a piece of work the string path has
not needed either (there is no `INCRBYFLOAT`), and not something to slip in with the rest. The
indexed form with its threshold measurement, and the hash-workload memory bench, remain as §19.6
named them.

## 20. More arguments, and answers in pieces (epic #8, issue #11, slice 1 of 3)

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

## 21. Many connections: buffers that are lent, not owned (epic #8, issue #11, slice 2 of 3)

**The limit.** The cache owned, for each of its 256 possible connections, a 16 KiB input buffer, a 64 KiB queue for output the kernel has not taken and a 16 KiB queue for a transaction: 96 KiB each, 24 MiB for 256, and **960 MiB for 10,000**, which is Redis's default `maxclients`. Almost all of it is idle: a connection that is waiting for its next command holds no input, no output and no transaction.

**The design.** A connection owns only what it always has: its row of `state` (13 ints, 104 bytes), its client name (64), its transaction row (24 ints, 192) and its entry in `std.conns`. The three buffers are *lent*, from three pools of fixed blocks made at start (nothing is allocated afterwards, which is the claim of the project):

| pool | block | blocks | held by a connection | so the cache holds |
|---|---:|---:|---|---:|
| input | 16 KiB | 1,024 (+1 shared) | while a command has arrived only in part | 16 MiB |
| output | 64 KiB | 256 | while the kernel has not taken all its answers | 16 MiB |
| transaction | 16 KiB | 128 | from the first command queued after `MULTI` to `EXEC` or `DISCARD` | 2 MiB |

* **Input.** A read goes into the one *shared* block (the last of the input pool) when the connection holds none, whole commands are answered from there, and only if a part of one is left over is it copied into a block lent to the connection. When nothing is left over the block goes back. A pipeline of whole commands (the gate's cells) never takes a block, and one copy that a read used to avoid is now made only for the partial command at the end of a read.
* **Output.** A block is lent at the start of a turn and returned at its end if the kernel took everything (nearly always). If the pool is empty the answers are still tried straight at the socket, and the connection is closed only if the kernel does not take them all: what a client that is not reading already suffers when its queue overflows.
* **Transaction.** Lent when the first command is queued. If the pool is empty the queue is "full" (`-ERR transaction queue is full`, `EXECABORT`), as when a queue overflows.
* **Close.** Every block a connection holds goes back when it closes.

**Measured** (`tests/connections.py`, this machine): 10,000 idle connections raise the cache's RSS by 3.1 MiB, **about 330 bytes each** (the prediction of ~5 MB counted pages the cache never touches; the kernel's socket memory is not in the cache's RSS and is the same for Redis), against ~20 KiB for each of Redis's. All 10,000 answer `PING`; the 10,001st is closed; closing 5,000 makes room again. The fixed buffers are 34 MiB, against 24 MiB before, and are virtual until used. Throughput gate (`-P 1 16`, set and get): 1.04, 1.33, 1.07 and 1.00 times Redis, so the copy of a leftover partial command into a lent block costs nothing measurable. Exhaustion, each tried: 1,400 half-commands leave exactly 1,024 connections and close the rest; 160 `MULTI` queues leave 128 and refuse 32; stalled readers (300 clients that never read) leave the server answering others, and a second wave after they close behaves the same, which shows the blocks come back. **What this costs:** a limit that was a number of connections is now a number of connections *doing something at once* (1,024 with half a command, 256 with unsent answers, 128 in a transaction), and the way it fails is the way an overflowing queue failed before. Redis has no such limit; its buffers are allocated as clients need them.

## 22. Large values: streamed in, streamed out (epic #8, issue #11, slice 3 of 3)

**The limit.** A value is at most 16 KiB, because a command must arrive whole in one 16 KiB input block and an answer is built whole in the scratch buffer (`3 * reserve()`). Redis takes 512 MiB (`proto-max-bulk-len`). This slice lifts the limit for the two commands that carry a value across the wire, `SET` and `GET`, and makes every other command *refuse* what it cannot hold, rather than overflow.

**Streamed `SET`.** `resp.parse` learns a second answer for a command whose *last* bulk header says more than the block can hold: "the command so far, and N bytes of value still to come". For `SET key <big>` and `SETEX`/`PSETEX key amount <big>`, the commands whose value is the last argument (`SET key <big> EX 10` has its option after the value, and a command is parsed in order, so it is *not* streamed: it is refused as "command too large" and the connection closed, as every command over a block was before; the first draft of this section claimed otherwise and was wrong) the cache then:

1. checks the key, options and the size exactly as `SET` does (the same errors, in the same order), and that N is at most 512 MiB (`resp.max_bulk`; above it Redis's `-ERR Protocol error: invalid bulk length` and the connection is closed; making it configurable is the next PR);
2. asks the store to *reserve* a staging record of N bytes in the arena (`store.reserve`): an entry number in state `staging` (kept by compaction, never evicted, not in the index, so no reader, `SCAN` or `DBSIZE` sees it), with the key kept inside the record. If the arena cannot hold it, the value is read and discarded and the answer is Redis's `-OOM`;
3. copies each read straight into the record (`store.fill`), counting down N; the connection holds no input block while it streams, only its state (`st[p+13..]`: the entry, bytes remaining, mode);
4. at the closing CRLF `store.commit` links the record into the index, or, if the key exists, makes the staged record the key's (the header's entry number is rewritten; nothing is copied) and the old record garbage; or `store.abort` if the connection closed first or the bytes after the value are not CRLF. A `FLUSHALL` while a value arrives makes the staged entry mean nothing (the store's epoch changed): the rest is thrown away and the client is told so. Arena compaction may move a staging record, so the connection names it by entry number each time and the store keeps entry numbers stable (as it already does).

**Streamed `GET`.** A value over `piece()` is answered by the header, then pieces of `piece()` bytes copied from the arena into the connection's output block as the kernel takes them, by entry number and offset, the way `MGET` is resumed (`link`). A write to the key between pieces would change the answer part-way, so the connection keeps a *stamp* of the value (slot 9 of `meta`, from a clock that every write of a value advances: 8 bytes more for each possible key); if the entry's stamp has changed or the key is gone when the next piece is due, the connection is closed. (A per-bucket version, which `WATCH` has, would close it whenever any key sharing one of its 4,096 buckets was written, which for a long send is always.) At most 256 KiB are sent in one turn, then the connection is watched for room to write, so that one 512 MiB value does not hold up the others (a torn reply cannot be repaired). Redis never does this, and the cost is one client that reads a 100 MiB value slowly while another writes the same key.

**Everything else.** `GETSET`, `GETDEL`, `GETEX`, `GETRANGE`, `SET … GET`, `MGET`'s elements, `EXEC`'s replies, `APPEND` and `SETRANGE`'s result stay buffered: they refuse a value over `max_string()` (16 KiB) with `-ERR value too large for this command (see docs/design.md §22)`, which Redis does not. `APPEND`/`SETRANGE` cannot build a value above `max_string()` either. A trap here is a denial of service, so each is a mutant and a limits case. `DEL`, `EXISTS`, `STRLEN`, `TYPE`, `EXPIRE`… never touch the bytes and work on any size.

**What it costs, said first.** A big `SET` cannot be part of `MULTI`/`EXEC` (a queued command must fit the queue block): refused with the queue-full error. A big `SET` is not atomic with respect to *time* (it takes as many turns as the client takes to send it) but is with respect to *visibility*: nothing sees the key change until the commit. Other connections run between its reads, which is what stops one 512 MiB `SET` from stalling the cache; while it streams, the arena it holds is not available to others, so it counts against `maxmemory` at once.

**Measured** (`tests/large.py`, `tests/connections.py`, `bench/vs_redis.sh`, this machine). Values of 16,385 bytes, 40,000, 64 KiB, 1 MiB and 16 MiB round-trip byte for byte, as does one that is nearly the whole of an 8 MiB arena; a value that does not fit is refused with `-OOM` once its last byte has been read (a 100 MiB one into a 64 MiB arena), the old value is intact and the connection carries on; bulk headers of 2^40, 536,870,913 and 999,999,999 are `invalid bulk length` and the connection is closed; heads cut at every byte, values cut at random sizes, CRLFs split from the value and the next command, and 20 connections sending at once all store the value; a half-arrived value is invisible to `GET`, `EXISTS`, `DBSIZE` and `KEYS`; ten abandoned 5 MB values leave the arena free for a 40 MiB one; a `FLUSHALL` while a value arrives is answered with an error and the keys written since are untouched; a `GET` of an 8 MiB value whose key is then `SET`, `DEL`eted or written by `SET` is closed, and one whose key is only given an `EXPIRE`, or whose neighbour is written, is not; while 256 MiB is sent to one client that reads as fast as it can, the longest wait of a `PING` from another was 16 ms; while a 1.5 MB value arrives into a 4 MiB arena that other connections churn and compact, it arrives intact. Throughput gate (`-P 1 16`, set and get): 1.00, 1.33, 1.12 and 1.00 times Redis; idle connections cost 368 bytes each at 10,000 (it was 330: five more ints of state). A 16 MiB `SET` takes 0.3 s from a Python client and the `GET` 0.15 s; the copy from the input block to the record is a byte loop (`store.fill`), because the language has a block copy only within one slice. The differential (527 cases and 8 small-index cases) has 0 differences; the mutants of `tests/stream_mutants.py` are all caught except one that is not tried (the 256 KiB a turn may send: on loopback the kernel's buffer is full long before it is reached).

**What it costs, said last.** `meta` is 10 ints for each key, not 9 (the stamp): 8 bytes for each of the possible keys, 8 MB at the default million. A `SET` that replaces a value needs room for both until the last byte is here. `SET key <big> EX 10` and `SET … NX` with a value over a block are refused, `SETRANGE` and `APPEND` cannot make a value over 16 KiB, and the commands in the list above refuse values over 16 KiB: Redis does none of these.

**Order of work.** One PR for `SET`+`GET` streaming with the guards (they define what a big value is, so they cannot be separated), one for the configurable maximum and its README line.

## 23. `COPY` (epic #8, issue #10, slice 3)

**The claim.** `COPY source destination [DB db] [REPLACE]` answers byte for byte as Redis 7.0.15 does, for strings and hashes, and the copy carries the source's kind and expiry. **Gate**, fixed before the code: the differential cases in `tests/differential.py` (arity, string, expiry kept, missing source, existing destination with and without `REPLACE`, the destination's expiry dropped, a key onto itself, bad options, a hash, a string over a hash, inside `MULTI`) have 0 differences, and the arity sweep includes `COPY`.

**How.** `store.copy_key` reserves a staged record for the destination (`reserve`, §22), copies the source's value into it with `copy_within`, and commits it with the source's expiry and kind (`commit` makes a string; the kind is set after). The source is protected while `reserve` makes room, so a compaction or eviction cannot take it. Nothing is visible until `commit`, so a refusal (no room: `-OOM`, as `RENAME`) leaves both keys as they were. Costs room for a second copy of the value, like `RENAME`.

**A deliberate difference.** There is one database, so `DB 0` is accepted and any other number answers `-ERR DB index is out of range`. Redis has 16 and takes `DB 1`; the check `COPY a b DB 1` is therefore not in the differential, and this is the same difference `SELECT 1` has (§12).

**Not built:** `OBJECT` (built in §24), `MEMORY USAGE`, `INCRBYFLOAT`, `LCS`, `COMMAND INFO/DOCS/GETKEYS` remain open in issue #10. *(The absolute-time commands were listed here as blocked by there being no calendar; that was wrong, see §25.)*

## 24. `OBJECT ENCODING`, `IDLETIME`, `FREQ` (epic #8, issue #10, slice 4)

**The claim.** `OBJECT ENCODING key` answers what Redis 7.0.15 does for a value set whole: `int` for a string that is a whole 64-bit integer (Redis's `string2ll` rule, the one `INCR` uses), `embstr` up to 44 bytes, `raw` above that; `listpack` for a hash of at most 512 fields (`hash-max-listpack-entries`; the first draft of this section said 128, which CI's mutant run showed was wrong) none of whose fields or values is over 64 bytes, `hashtable` otherwise. `OBJECT IDLETIME` is whole seconds since the key was last used; `OBJECT FREQ` answers Redis's LFU error, because no policy here is LFU. A missing key is a null, and the errors (unknown subcommand, arity per subcommand, `OBJECT` alone) are Redis's. **Gate**, fixed first: the differential cases named `OBJECT …` have 0 differences, and the mutants for the `int` rule and the 512-field limit are caught.

**How.** The encoding is worked out from the bytes when asked, not recorded. `IDLETIME` needs the key's last-use stamp (`meta` slot 7) as it was, and `find` sets it to now, so `store.peek` is `find` without the stamp and `find` is `peek` plus the stamp. Redis's `OBJECT` does not touch the key either.

**Where it differs, said first.** (1) Redis records an encoding when the value is written: a string made by `APPEND` or `SETRANGE` is `raw` however short, a hash that once grew past a limit stays `hashtable` after it shrinks, and a hash of 129 fields it was converted from stays so. This cache answers from what the value is now, so those cases differ. The differential does not do them. (2) `OBJECT REFCOUNT` and `OBJECT HELP` are not built and answer `unknown subcommand`. (3) `IDLETIME` counts the cache's millisecond clock, Redis's 1-second LRU clock, which ticks every 100 ms, so a key just written reads 0 or 1 there and 0 here. The differential therefore does not compare `IDLETIME`'s value (CI found the 1 in a run); only that a missing key is null and the arity errors match.

## 25. Times of day: `EXPIREAT`, `PEXPIREAT`, `EXPIRETIME`, `PEXPIRETIME`, `EXAT`, `PXAT` (epic #8, issue #10, slice 5)

**A claim corrected.** §15 and the first version of §23 said these could not be built because the cache has a monotonic clock and no calendar. The compiler has `clock_unix_ms(&Clock)` (`docs/native-sockets.md` §10.5 in the cancho repository), on the same `clock` capability the cache already holds, so the authority report does not widen.

**How.** The store keeps expiries in its own milliseconds (`tick`'s monotonic clock). At start the cache reads the wall clock once and the store keeps the difference (`wall`: unix milliseconds minus monotonic ones). A time of day goes in as `T - wall` and `EXPIRETIME`/`PEXPIRETIME` add `wall` back, so a time given comes out as the same number, to the millisecond. A time at or before now is the store's `now`, which every caller already treats as over: `EXPIREAT` deletes the key (Redis does too, and answers 1), `GETEX` deletes it, `SET` stores a key that is gone at its next read. `EXAT 0`, `-5` and a time of day in seconds too large to be milliseconds are `invalid expire time in '<command>' command`, as in Redis. `EX`/`PX`/`EXAT`/`PXAT`/`KEEPTTL` exclude each other as before, and an option given twice, the last wins.

**What it costs, said first.** The offset is read once, so the cache's idea of the date drifts from the machine's by however far the two clocks drift apart in the life of the process (and a clock step on the host is not followed): a key given `EXPIREAT` for a moment ahead may live a little longer or shorter than on Redis, which reads the wall clock every time. A key with a *relative* expiry is not affected. `TTL` of a key set by `EXPIREAT` is computed from the monotonic clock, so it stays correct across a clock step, and `EXPIRETIME` does not.

**Gate**, fixed before the code: the differential cases for these commands have 0 differences (they use a time of day far ahead, 4102444800, and far behind, so that the answer does not depend on when the test runs, and `TTL` is not compared for them), the mutants for the offset, the seconds-to-milliseconds step, the past time and `SET`'s absolute branch are caught, and the arity sweep includes the four commands. The two known divergences for `SET … EXAT` and `EXPIREAT` in `tests/differential.py` are gone.

## 26. `COMMAND INFO` from a generated table (epic #8, issue #10, slice 6)

**The gap.** §12 recorded that `COMMAND` keeps no table of flags and key positions, so `COMMAND INFO` answered an empty array and `COMMAND COUNT`/`LIST` came from a hand-kept list of names that had fallen behind the dispatcher (54 names for 75 commands). Cluster-aware and proxy-aware clients read `COMMAND INFO` (arity, flags, first/last key, step, ACL categories, key specs) to find a command's keys, and the cluster-lite issue (#24) needs it.

**The choice.** The table is *copied from the oracle*, not written: `tests/gen_cmdinfo.py` asks a Redis 7.0.x server for `COMMAND INFO <name>` for every name the dispatcher knows (`is_word(name, "…")` in `src/*.cho`), once in RESP2 and once under `HELLO 3` (the two differ in the types of the flag lists and key specs), and writes `src/cmdinfo.cho`: one `if` per command that puts the entry's bytes. `COMMAND COUNT`, `COMMAND LIST` and `COMMAND`/`COMMAND INFO` with no names are built from the same table, so they cannot disagree. CI regenerates the file and fails if it differs, so a command added without regenerating, or an arity changed in the source, is found; the differential has a `COMMAND INFO` case for each name in both protocols, so the table is compared byte for byte with Redis rather than trusted.

**Where it differs, said first.** (1) The table holds the 75 commands this cache implements, not Redis's 240: `COMMAND COUNT` and plain `COMMAND` remain listed as known divergences, with the new reason. (2) A container command (`CLIENT`, `CONFIG`, `COMMAND`, `OBJECT`) carries Redis's full subcommand list, including subcommands this cache does not implement (`CLIENT KILL`, `CONFIG SET`…): the entry describes the Redis command. (3) Redis lists subcommands in the order of a dictionary that differs from one process to the next, so the differential cannot compare those four byte for byte; `tests/keyspace.py` compares them with the subcommands sorted by name. (4) `COMMAND INFO config|get`, `COMMAND DOCS config|get` (a subcommand by name) and `COMMAND GETKEYSANDFLAGS` are not built; an unknown name, including that form, is a null, as in Redis for a name it does not know. (5) The file is 88 KB of generated text, and `COMMAND` with no names answers all of it in one reply (about 90 KB; the reply buffer holds 1 MiB).

**Built, and what building it found.** The first draft of the differential showed the three subcommand-bearing commands differing, and a byte comparison of one against a second Redis showed the same differences between two Redis processes: only the order of subcommands, which is how (3) was found, rather than a bug in the table.

**`COMMAND GETKEYS`** (same slice). The generator also writes `cmdinfo.key_spec(name, subcommand)`: arity, first key, last key and step of each command and each subcommand, read from the same entries. `COMMAND GETKEYS` walks the range (a negative last key counts from the end of the line) and answers the keys; the refusals are Redis's, and their order was found by asking it: an unknown command (or subcommand) is `Invalid command specified`; a command whose entry has no keys is `The command has no key arguments` *before* its arity is looked at (`KEYS a b c` is that, not an arity error); then `Invalid number of arguments specified for command`; then `Invalid arguments specified for command` for a line too short for its range. `COMMAND GETKEYS` itself has arity -4. Differential: eight cases of 78 lines, every command this cache implements, plus the refusals and any-case names, 0 differences against Redis in a manual run before they were written down; two mutants (a wrong step, and the arity checked before the keys). Not built: commands whose keys are found by a count in the line (`EVAL`, `ZUNIONSTORE`…), which this cache does not implement.

**`COMMAND DOCS`** (same slice). The generator writes a second module, `src/cmddocs.cho`, with each command's docs map (summary, since, group, complexity, arguments, and a container's `subcommands`) as Redis 7.0 gives it in RESP2 and RESP3: a module of its own because the two tables together would pass the 2,000-line ceiling (and the formatter will not shorten an entry to fewer lines). `COMMAND DOCS` with names answers a map of the known ones, in the order asked and with an unknown name left out (Redis does not answer a null); with none it answers all 75. A container's `subcommands` map is written sorted by key, for the same reason as the entries. Differential: `COMMAND DOCS` of each non-container command in both protocols plus four cases of names, 0 differences; the four containers are compared in `tests/keyspace.py` with `subcommands` sorted; one mutant (every name counted as known). The generated modules are 100 KB and 124 KB, and a full `COMMAND DOCS` is 42 KB.

## 27. Strictly ordered use stamps (epic #8, issue #30)

**What was seen.** Issue #30 recorded one CI failure of `tests/expiry.py` ("a key used all the time survives 20,000 writes that evict") and a hypothesis: eviction takes, of five sampled keys, the one with the smallest last-use stamp, the stamp is the millisecond of the loop turn, and keys of one burst all carry the same stamp, so a key used all the time can lose a tie it should not. The issue asked for the tie rate to be measured before anything was changed.

**Measured.** The scenario of the test, with the commands sent in pipelined bursts of 200 (so that a burst is one turn of the loop and one millisecond), run against a fresh 1 MiB `allkeys-lru` cache 200 times (a script of 60 lines, `hot.py`, not kept: its check is now part of `tests/expiry.py`): **`hot` was evicted in 82 of 200 runs (41%)**. The failure the issue saw, with the test's own slower sending, was the rare end of the same thing. The estimate of §3 of the issue was right in kind and badly low in size: `hot` loses an eviction when it is among the five sampled (about 0.5%) and the other four are not older, which includes every key written in the same millisecond as its last read, and a fast server writes hundreds of keys in a millisecond. With 19,000 evictions the chance of losing it once is large as soon as a few percent of the keys tie with it.

**The change.** A use of a key is stamped with the millisecond in the high bits and, in the low 24, how many uses have been stamped in that millisecond so far (`store.use_stamp`; `tick` starts the count again when the millisecond changes). Every use gets a stamp larger than the one before it, so no two keys tie, and "used longest ago" always has an answer. A stamp is 8 bytes as before (the millisecond fits above bit 24 for 8 million years of uptime), the cost is one add on each use, and `OBJECT IDLETIME` divides the stamp back to the millisecond. This is option 2 of the issue in a stronger form: not a tie-break rule, but no ties.

**Measured after.** The same 200 runs: **`hot` lost in 0**. `tests/expiry.py` has the burst scenario as a check of its own; a mutant that stamps with the millisecond alone fails it, and a store unit test checks the order of uses within and across milliseconds.

**What it does not change.** Eviction is still a sample of five, not exact LRU; and the hit rate is unchanged: `bench/hitrate.py` (Zipf 0.99 over 2,000,000 keys, cache-aside, three million requests counted) gives **0.8538** for the cache before and after, against Redis's 0.8583, at 129.1 and 129.0 MiB resident. The workload has no burst of writes to tie on, so it could not show the fix; it shows that the fix does not cost anything where there was no tie to break.

## 28. Eviction policies and `CONFIG SET` (epic #8, issue #17, slice 1)

**The gap.** Two policies, fixed at start, and `CONFIG SET` refused. A client or an operator that sets `volatile-ttl`, or a framework that switches the policy while the cache runs, found neither.

**Built (slice 1).** `allkeys-random`, `volatile-lru`, `volatile-random` and `volatile-ttl` beside `noeviction` and `allkeys-lru`, selectable on the command line (any of the six names) and by `CONFIG SET maxmemory-policy` while running; `CONFIG SET maxmemory-samples` (1 to 2,147,483,647 as in Redis; the sampler uses at most 64) and `CONFIG GET` of both, and `INFO` names the policy in force. `victim` is one function for all of them: the random policies take the first key they draw that they may, the others sample `maxmemory-samples` keys they may take and choose the one used longest ago (`*-lru`, with the strictly ordered stamps of §27) or whose expiry is soonest (`volatile-ttl`). "May take" is: in use, not the key being written, and, for `volatile-*`, with an expiry. There is no branch on the `GET`/`SET` path: the policy is read only when the arena or the key table is full.

**`volatile-*` with few volatile keys.** Drawing at random may miss the few keys that have an expiry, so after `max(40, 8 × samples)` draws `victim` looks from a random start through at most 65,536 entries for any key it may take, and past that the write is refused with `-OOM`, as when there is none. The bound is the stall bound of the issue: a refusal costs at most 65,536 steps (about 100 µs), and a store of up to 65,536 entries is searched whole. A store of a million keys of which a handful have an expiry can therefore refuse a write that Redis, which keeps the volatile keys in a dictionary of their own, would serve. Said plainly, not measured to be rare.

**Where it differs from Redis.** (1) `CONFIG SET` of every other setting is refused, each with Redis's own `CONFIG SET failed (possibly related to argument …)` frame and a reason of this cache's; Redis accepts `port`, `timeout`, `hz` and the rest. `maxmemory` in particular **cannot be changed**: the arena is allocated at start and nothing is allocated afterwards, so it can neither grow nor, today, be lowered (lowering it is slice 2's question). (2) `allkeys-lfu` and `volatile-lfu` are valid values in Redis and are refused here with a reason that says they need a use counter this cache does not keep yet. (3) The order of `CONFIG GET maxmemory*` follows this cache's list, not Redis's internal one. (4) A parameter Redis has and this cache does not know is answered `Unknown option or number of arguments for CONFIG SET`, which Redis would not say for a parameter it has.

**What is checked.** `CONFIG SET`: 15 differential cases against Redis, in both protocols, covering every policy, every refusal text, several pairs at once, the order Redis looks at names before values (unknown, immutable and repeated names first, then values, nothing applied unless all are good) and arity, 0 differences. The policies: `tests/expiry.py` runs each on a 1 MiB arena written to far past its size and checks that the `volatile-*` policies take only keys with an expiry (300 keys without one all survive 6,000 writes of keys with one), that a full arena of keys without an expiry refuses under them, that `volatile-ttl` takes the keys that expire soonest, that `allkeys-random` refuses nothing, and that `CONFIG SET maxmemory-policy allkeys-lru` makes a full `noeviction` cache take writes. Five mutants (the newest key taken, the volatile filter ignored, the latest expiry taken, `CONFIG SET` that keeps the old policy, a repeated parameter accepted) are caught.

**Not built, said first.** The LFU policies and their settings (`lfu-log-factor`, `lfu-decay-time`); `CONFIG SET maxmemory` lowering the limit; the settings `requirepass`, `timeout`, `maxclients`, `hz`; a hit-rate benchmark per policy; `bench/stall.py` per policy. Slice 2.

## 29. Cluster-lite: slots, redirects and `CLUSTER` (epic #8, issue #24, slice 1)

**The claim, fixed before the code.** N cache processes started with the same static slot map answer a cluster-aware client as Redis Cluster would for the commands this cache has: the same slot for every key (`CRC16(key) mod 16384`, with `{hash tags}`), `-MOVED slot host:port` for a key another node owns, `-CROSSSLOT` for keys in different slots, and `CLUSTER KEYSLOT`/`MYID`/`INFO`/`SLOTS`/`NODES`/`SHARDS` in Redis's shapes. **Gate**: `CLUSTER KEYSLOT` of tens of thousands of random and hash-tagged keys agrees with a Redis in cluster mode; a 3-node Redis cluster and a 3-node cache cluster with the same layout (0–5460, 5461–10922, 10923–16383) are sent the same lines to the same node and answer byte for byte once the node ports and ids are mapped; mutants of the slot function, the redirect and the cross-slot check are caught. Not in this slice: gossip, failure detection, failover, `ASK`/migration (as the issue says), `redis-py`/`ioredis` cluster clients (next), `COUNTKEYSINSLOT`.

**Configuration: an argument, not a file.** `--cluster=0-5460=127.0.0.1:7000,5461-10922=127.0.0.1:7001,10923-16383=127.0.0.1:7002` (any argument after the sixth; ranges `a-b` or a single slot `a`, a node may have several). The node this process is is the one whose port is the port it listens on; two nodes on one port are refused. A map that overlaps itself, names a slot past 16383 or has no entry for this port stops the start with status 2. Slots that no entry names are allowed and answer `-CLUSTERDOWN Hash slot not served` (and `cluster_state:fail`), as in Redis. Reading a file would widen the authority report (a path from the command line is an unbounded read); an argument does not.

**Where the state lives.** In the `world` array that every command already receives read-only (`info`, 8 ints so far): after them, whether cluster mode is on, which node this is, how many nodes there are, the nodes (port, and the host one byte per int) and one int per slot for its owner. 150 KB, allocated at start with the rest. Node ids are 40 hex digits derived from `host:port`: Redis's are random and a client treats them as opaque.

**Where the check sits.** One place, before `txn.handle` in the loop of `src/cache.cho`: so a command queued in `MULTI` is checked when it is queued and a refusal makes `EXEC` answer `EXECABORT`, as Redis does (seen on a live 3-node cluster: `MULTI`, `SET foo` → `-MOVED`, `SET bar` → `+QUEUED`, `EXEC` → `-EXECABORT`). The keys of a command line are found with the generated key positions (`cmdkeys.key_spec`, §26): no keys, or a line the arity check would refuse, is not looked at; keys in two slots are `-CROSSSLOT Keys in request don't hash to the same slot` *before* any `-MOVED` (seen: `DEL bar foo`); one slot owned elsewhere is `-MOVED`.

**Differences, said first.** `CLUSTER NODES` has no gossip to report: the last two numbers of a line are 0, the epoch is the node's number; `CLUSTER INFO` has no message counters (zeros) and epochs 0. A node does not learn the others' state: the map is what it was started with, so a node that dies keeps its slots until an operator restarts the others with another map. `SELECT` with a database other than 0 answers Redis's `SELECT is not allowed in cluster mode`.

## 30. `INFO` as an exporter reads it (epic #8, issue #18, slice 1)

**The gap.** `INFO` printed six sections with 16 fields between them. Exporters (`redis_exporter`, Datadog, Grafana's dashboards) read `commandstats`, `errorstats`, `keyspace_hits`/`misses`, `instantaneous_ops_per_sec`, `used_memory_peak`, the `Keyspace` line's `avg_ttl` and the sections a standalone Redis always prints, and a field that is missing makes the cache look broken to monitoring.

**Built (slice 1: `INFO`).** The sections of Redis 7.0 in its order, each with the fields this cache can report truthfully: **Server** (`redis_mode` is `cluster` in cluster mode, `uptime_in_days`, `hz`), **Clients** (`maxclients`, which is 10,000 and no longer the stale 256 of `CONFIG GET`, and the zero fields), **Memory** (`used_memory_human`, `used_memory_peak` and `_human`, `maxmemory_human`, `mem_allocator`), **Persistence** and **Replication** (the true constants of an instance with neither: `loading:0`, `aof_enabled:0`, `role:master`, `connected_slaves:0`…), **Stats** (`keyspace_hits`, `keyspace_misses`, `total_error_replies`, `instantaneous_ops_per_sec`), **Commandstats**, **Errorstats**, **Cluster**, **Keyspace** (`expires` and an `avg_ttl` that is the mean over the first 4,096 entries, not a sample). `INFO` takes several sections (`INFO server clients`) as Redis 7 does, and `commandstats` is in `INFO commandstats`, `ALL` and `EVERYTHING` and not in the default, as there.

**How it is counted, and what it costs.** Everything is an int in the `world` array (§29), nothing allocated. For each command the loop writes `stats.record` after the reply: the command's number from `cmdnames.index_of` (generated, §26: found by name length and first letter, so a command is compared with one or two names and not with all 78), whether the reply is an error, and, if so, its code (`ERR`, `WRONGTYPE`, `OOM`, `EXECABORT`, `MOVED`, `CROSSSLOT`, `CLUSTERDOWN`, `NOAUTH`, `READONLY`, or other: nine counters and one). A command refused before it ran, which Redis counts as `rejected_calls` and not as a call, is the same here: a wrong number of arguments, `OOM`, a redirect, `NOAUTH`; any other error is a call that failed. Once a turn the loop takes a sample for the ops-per-second window (16 samples 100 ms apart, as Redis does) and notes the arena's peak. Hits and misses are counted by `store.find_read`, which the commands that read a key use (`GET`, `MGET`, `GETRANGE`, `GETEX`, `GETDEL`, `STRLEN`, `EXISTS`, `TTL`, `PTTL`, `EXPIRETIME`, `PEXPIRETIME`, `TYPE`); the hash commands share one lookup with the writes and are not counted. The throughput gate (§2) was re-run with the counting in, and the figures are below.

**Not reported, said first.** `usec` and `usec_per_call` of `commandstats` are 0: timing a command would need a second clock read for each, which the loop does not do (§2; `max_turn_ms` is the longest turn). `cpu`, `latencystats` and `modules` are absent: there is no way to measure CPU without a call this cache does not have. The streamed `SET` (§22) and the commands run by `EXEC` are not counted individually (`EXEC` is). `total_net_input_bytes` and the other byte counters are absent. `rejected_connections` is absent. Most of the persistence and replication fields are absent, not zeroed: only the ones whose true value is a constant.

**What is checked.** `tests/info.py` runs 27 commands on Redis and on the cache and reads `INFO everything` of each: every section the cache prints is one Redis prints; every field of it is a field Redis has in that section (but `server_name`, `compactions`, `compactions_forced`, `max_turn_ms` and `used_memory_dead`, which are this cache's and said so); `calls`, `rejected_calls` and `failed_calls` of every command run are equal; `errorstats`, `keyspace_hits`, `keyspace_misses`, `total_error_replies` and the `Keyspace` line's keys and expires are equal; sections can be asked for by name and several at once; the numbers that move (`total_commands_processed`, the peak) move.

**Throughput gate, re-run** (`bench/vs_redis.sh`, one server core, five rounds, Redis and the cache interleaved; this machine, 4 cores, so the pinning is 0 / 2-3). Ratio to Redis, the cache before this change (`main`) and after:

| | before | after |
|---|---|---|
| `set`, `-P 1` | 1.20 | 1.15 |
| `set`, `-P 16` | 1.33 | 1.33 |
| `get`, `-P 1` | 1.10 | 1.10 |
| `get`, `-P 16` | 1.49 | 1.00 (5 runs), then 1.50 and 1.50 (10 runs) |

The one cell that moved needed a closer look and is **noise, not cost**. At `get -P 16` the cache runs in one of two modes, about 1.33M or about 2.0M requests per second, whichever the scheduler gives it for a run; the old build was in the high mode in 3 of its 5 runs and the new build in none, which is what made the ratio read 1.00. Measured again with 10 rounds per build, old, new, old, new in turn so that drift in the machine shows up as drift: the old build's medians were 1.98M and 1.99M (ratios 1.49 and 1.50), the new build's 1.99M and 1.99M (1.50 and 1.50), and the new build was in the high mode in 7 and 8 runs of 10 against the old build's 6 and 8. Neither run flagged the load generator as the limit. What this does not show is the cost of one counter, which is below what these runs can resolve: the count of a command is a name-length switch, one `is_word` and two adds. `set -P 1` is 5% lower after (1.15 against 1.20) in a single run of five; it was not re-run, and is reported, not explained.

## 31. The slow log (epic #8, issue #18, slice 2)

`SLOWLOG GET [count]`, `LEN` and `RESET`, and the settings `slowlog-log-slower-than` (microseconds, default 10000; negative switches the log off) and `slowlog-max-len` (default 128), both in `CONFIG GET` / `CONFIG SET`. `src/slowlog.cho` keeps the entries in a ring the store owns, fixed at start: 128 entries of `store.slow_room()` bytes of text each; an entry's slot is its id modulo the ring, so nothing is allocated when a command is slow.

**What an entry holds.** The id, the time of day (seconds), the duration, the arguments, the client's address and its name. As in Redis, at most 32 arguments are kept (the first 31 and a marker `... (N more arguments)`), each cut at 128 bytes with a marker `... (N more bytes)`, and the arguments of `AUTH`, and the two after `AUTH` in `HELLO`, are `(redacted)`. `SLOWLOG GET` lists the newest first; the count is 10 by default, `-1` is all, anything below `-1` is `-ERR count should be greater than or equal to -1`. The ids go on after `RESET`, as in Redis.

**What is logged.** A command that ran, once it has run, whose time is at least the threshold *as it then is* (so the `CONFIG SET` that sets it is judged by the new value, as in Redis). Not logged: a command refused before it ran (a wrong number of arguments, an unknown command or subcommand, `NOAUTH`, `OOM`, `MOVED`, `CROSSSLOT`, `CLUSTERDOWN`), one only queued in `MULTI`, and `EXEC` itself; the commands `EXEC` runs are logged one by one, and `MULTI` is logged. This is what `tests/slowlog.py` compares with Redis 7.0 after `CONFIG SET slowlog-log-slower-than 0`: the arguments, ids and order, the client name, every count and every error text. Time, duration and the client's port are machine matters and only checked to be of the right kind.

**What it costs, and the choice that follows.** The clock is the store's, one reading per call, 1 ms in resolution, so a duration is a whole number of milliseconds times 1000. Two clock readings around every command would be a cost on the path that the gate protects, and the load generator hides it at `-P 16`. So by default only a command that *can* be slow is timed (`cmdnames.may_be_slow`: the commands that walk many keys or bytes, from the generated table), and any command with more than 8 words; when the threshold is 1000 µs or less every command is timed, since at a resolution of a millisecond any command may reach it. `CONFIG` is always timed, because it may be the command that switches the log on. The consequence: with the default threshold a fast command is never logged, which is the point, and a command the table does not list as possibly slow but which was slow (stalled by the scheduler, say) is missed. That is the price of not reading the clock twice for every `GET`.

**Limits, said plainly.** `slowlog-max-len` above 128 is refused (the ring is fixed); `SLOWLOG HELP` is not supported (it answers an error that says so); the resolution is 1 ms, not 1 µs; a log switched off and on again keeps the entries it had.

**Mutants.** `tests/slowlog_mutants.py` (21) change the cut, the counts, the redaction, the order, the defaults, the threshold edge and which commands are logged; `tests/slowlog.py` must fail on each.

**Throughput gate, re-run** (`bench/vs_redis.sh -r 10`, the final code, Redis and the cache interleaved). Ratio to Redis: `set -P 1` 1.10, `set -P 16` 1.33, `get -P 1` 1.10, `get -P 16` 1.00. The last cell is the bimodal one of section 30: in this run the cache's ten runs were nine at about 1.33M and one at 2.0M, Redis's likewise at about 1.33M (one run each at 1.5M and 1.0M), so the median ratio reads 1.00 with both servers in the same mode; it is noise in the scheduler's mode, not a cost of the slow log. The other three cells are within a few percent of section 30's (`set -P 1` 1.15 there, 1.10 now: inside the run-to-run spread of 166k to 210k requests per second in this run). The 21 mutants of `tests/slowlog_mutants.py` were all caught.

## 32. `CLIENT LIST`, `INFO` and `KILL` (epic #8, issue #18, slice 3)

The commands that look at other connections, in `src/clients.cho`. The loop (`src/cache.cho`, `execute_in`) answers them itself, because only it holds the table of connections; every other command goes on to `commands.execute` as before, inside `MULTI`/`EXEC` too.

**What a connection now remembers.** Four more ints in its row (`stride()` 18 → 22): when it connected, when it last ran a command, which command that was (`cmdnames.index_of`), and a mark that `CLIENT KILL` sets. The two writes on the command path are stores of ints, no clock read (the turn's time is the store's).

**`CLIENT LIST [TYPE normal|master|replica|pubsub] [ID id ...]`** writes Redis 7.0's line for each connection: `id addr name age idle flags db sub psub ssub multi qbuf qbuf-free argv-mem multi-mem rbs rbp obl oll omem tot-mem events cmd user redir resp`. The flags are `x` (in `MULTI`), `c` (to be closed once its answers have gone) or `N`. Every connection is a `normal` one, so `TYPE master|replica|pubsub` lists nothing; a type nobody has is `-ERR Unknown client type`; `ID` takes any number (0 and negatives find nothing) and refuses what is not one. **`CLIENT INFO`** is the caller's own line.

**What an entry cannot say, and says so.** `fd` and `laddr` are left out: `std.conns` gives neither a descriptor nor the local address of a connection, and a made-up value would be worse than none. `cmd` is the command's name without its subcommand (`client`, where Redis says `client|list`). The memory fields are what this cache can count (`qbuf`, `omem`, the queue of a `MULTI`); `rbs`, `rbp`, `argv-mem`, `obl` and `oll` are constants or zero because the buffers are lent in blocks of a fixed size, not grown. `tests/client_commands.py` checks that every field is one Redis has, that the cache's line has Redis's fields less those two, and that the fields the commands decide (`name`, `flags`, `multi`, `resp`, ...) agree.

**`CLIENT KILL`.** The old form (`CLIENT KILL addr`) answers `OK` or `-ERR No such client`; the new form (`ID`, `TYPE`, `ADDR`, `USER`, `SKIPME`) answers how many it closed, skipping the caller unless `SKIPME no`. The filters are Redis's, with Redis's errors; `USER` knows only `default`; **`LADDR` is refused** with an error that says why (no local address). Other connections are closed at once, and the loop skips any event that was already waiting for them; the caller, if it is among them, gets its answer and is closed after it, as `QUIT` does. `CLIENT KILL MAXAGE` is Redis 7.4's and is a syntax error here as it is in 7.0.

**`CLIENT NO-EVICT on|off`** is accepted and does nothing: the cache has no client eviction (`maxmemory-clients`), so there is nothing to protect a client from.

**A reply bound.** The list is written into the turn's answer buffer, which holds 3,342,336 bytes after what the turn has already written; an entry is at most 512 bytes, so a table that could not fit (about 6,500 connections) is answered with `-ERR too many clients to list in one reply`, not cut. The limit of connections is 10,000, so the largest tables are refused rather than listed in pieces: a gap, not a Redis behaviour.

**Not in this slice:** `CLIENT PAUSE`/`UNPAUSE` (they need the loop to hold commands back and write replies later), `CLIENT TRACKING*` (the tiered-mode issue), `CLIENT REPLY`, `CLIENT UNBLOCK`, `CLIENT GETREDIR`.

**Mutants.** `tests/clients_mutants.py` (22) change the filters, the flags, the kill selection and what happens to the killed; `tests/client_commands.py` must fail on each.

**Throughput gate, re-run** (`bench/vs_redis.sh -r 10`, the final code). Ratio to Redis: `set -P 1` 1.10, `set -P 16` 1.33, `get -P 1` 1.16, `get -P 16` 1.00 (both servers in the same mode of the bimodal cell of section 30, 1.33M requests per second each, so the median ratio reads 1.00). Unchanged from section 31: the command path gained two int stores and a comparison. The full differential (943 cases + 8 small-index cases, 0 differ) and all 22 mutants of `tests/clients_mutants.py` (two first survived, because the test compared neither `resp` nor `cmd`; it does now) were run on the final code.

## 33. `LATENCY` and `MEMORY` (epic #8, issue #18, slice 4)

**The latency monitor** (`src/latency.cho`). `CONFIG SET latency-monitor-threshold <ms>` (0, the default: off; negative or not a number: Redis's errors) turns on a record of commands that took longer than that. There are two events, as in Redis: `command`, for a command that can be slow (`cmdnames.may_be_slow`, the generated list also used by the slow log), and `fast-command`, for one that cannot. Each event keeps its last 160 samples in the store (`store.lat_block()` ints, nothing allocated when one is taken); a second sample in the same second keeps the longer, as Redis does. `LATENCY LATEST` answers `[event, time, latest ms, longest ms]` for each event that has samples, `HISTORY event` its samples oldest first (an event nobody has: an empty array), `RESET [event ...]` how many events had samples and forgets them, `DOCTOR` Redis's text when the monitor is off and when nothing has been seen.

**What is not the same.** The loop's clock is whole milliseconds, so a command that took a fraction of a millisecond across a tick reads as 1. An event is therefore noted only when the reading is *more* than the threshold, which means the command really took longer; a command that took between the threshold and one millisecond more may be missed. With a threshold of 1 every command is timed (two clock reads), otherwise only those that can be slow. Not supported, with an error that says so: `LATENCY GRAPH`, `HISTOGRAM`, `HELP`, and `DOCTOR` when there are events to report (Redis writes an analysis; this cache does not). The compaction slice, the natural third event class, is not timed yet.

**`MEMORY`** (`src/memory.cho`). `USAGE key [SAMPLES n]` answers the bytes of a key's name and value plus `store.record_overhead()` (the record's header, its row and its index slot), or nil; `STATS` answers `peak.allocated`, `total.allocated` (the arena in use), `keys.count`, `keys.bytes-per-key` and `dataset.bytes` -- a subset of Redis's fields, all of them Redis's names; `DOCTOR` Redis's text for an instance with under 5 MiB of data and its text for the rest; `PURGE` is `OK` and does nothing (there is no allocator to ask). `MALLOC-STATS` and `HELP` are errors that say they are not supported. The byte counts are the cache's own: `tests/latency.py` checks their shape, their names and that a longer value costs more, not that they equal Redis's allocator's.

**Checks.** `tests/latency.py` runs the same commands on Redis and the cache (the setting and its errors; the replies with the monitor off, on and with nothing seen; after `KEYS *` over 200,000 keys: the event, the shape of the sample, `HISTORY`, `RESET`; and `MEMORY`); `tests/latency_mutants.py` (17 mutants) must be failed by it. Three mutants an earlier draft had are gone because no deterministic test can tell them apart: the clock counts whole milliseconds, so the edge of the threshold (a command that took exactly the threshold) cannot be provoked on purpose; a `fast-command` sample needs a command that cannot be slow to take over a millisecond, which only a stall of the machine does; and `LATEST`'s longest differs from its latest only after two samples of different lengths in different seconds.

**Throughput gate, re-run** (`bench/vs_redis.sh -r 10`, the final code). A first full run read `get -P 1` at 0.95 of Redis, so that cell was measured again, `main` and this branch alternating, three times each (10 rounds, `-t get -P 1`). The cache's own median: `main` 221.9k, 210.3k, 222.1k requests per second; this branch 222.0k, 222.0k, 222.0k. The ratio moved only because Redis did (200k to 235k between runs on this machine, and two of the six runs lost their Redis reading); the cache did not slow down. The other cells of the full run: `set -P 1` 1.06, `set -P 16` 1.49, `get -P 16` 1.50 (Redis in its lower mode, the cache in its upper one). The full differential (943 cases + 8, 0 differ) and all 16 mutants of `tests/latency_mutants.py` were run on the final code.

## 34. `MONITOR` (epic #8, issue #18, slice 5)

`MONITOR` turns a connection into a watcher: it answers `+OK`, and from then on every command another connection runs is written to it as `+<seconds>.<microseconds> [0 <address>] "arg" "arg" ...`. Nothing is added to the connections' machinery: a watcher is an ordinary connection with one more int in its row (`stride()` 22 → 23) and the loop writes its lines through the same `emit` and the same lent output block as any answer. The cost on the command path when nobody is watching is one load of a counter (`stats.monitors`); with watchers, one line is formatted per command into the turn's answer buffer, after the answer, and offered to each watcher.

**What is shown, as Redis shows it** (`tests/monitor.py` runs the same script on both and compares the lines with the time and the port taken out): the commands that ran, with their arguments as the client sent them, quoted with `\\`, `\"`, `\n`, `\r`, `\t`, `\a`, `\b` and `\xHH` for other bytes outside 32..126; the arguments of `AUTH`, and the two after `AUTH` in `HELLO`, as `(redacted)` (the same rule as the slow log); `MULTI`; the commands an `EXEC` runs, one by one, and then `EXEC`; not shown: commands refused before they ran (a wrong number of arguments, an unknown command, `MOVED`, ...), a command only queued in `MULTI`, `MONITOR` itself, and the commands Redis flags `admin` (`CONFIG`, `SLOWLOG`, `LATENCY`, `CLIENT LIST`/`KILL`/..., `CLUSTER` administration): which they are is read from the oracle's `COMMAND INFO` by the generator and written as `cmdnames.skips_monitor`. A watcher sees its own commands after their answers.

**What a watcher may do.** Redis makes a watcher a replica; so do we: any command that reads or writes the keyspace or may be replicated is refused with `-ERR Replica can't interact with the keyspace` (`cmdnames.touches_keyspace`, generated the same way); `PING`, `INFO`, `CLIENT ID` and the like are answered; a second `MONITOR` is ignored without an answer; `RESET` ends the watching; closing the connection does.

**Bounding the cost.** The issue calls `MONITOR` a documented performance footgun. Here a watcher that does not read costs one block of output (`output_size()`, 64 KiB) and no more: when the lines for it no longer fit in that block, the watcher is closed, as Redis closes a client that falls too far behind, and the cache goes on (`tests/monitor.py` has a watcher that never reads while 2,000 commands go by). With any watcher, every command pays for its line; with none, nothing.

**Not the same.** A `SET` whose value is too long for a block and arrives in pieces (`docs/design.md` section 22) is not shown. The time has the loop's resolution (a millisecond), so the microseconds are always a multiple of 1,000. A refused `EXEC` (aborted by a `WATCH`) is shown like any other.

**Mutants.** `tests/monitor_mutants.py` (18) must be failed by `tests/monitor.py`.

**Throughput gate, re-run** (`bench/vs_redis.sh -r 10`, `main` and this branch alternating; the cache's own medians, since Redis's moved by up to 15% between runs and some runs lost their Redis reading). `set -P 1`: `main` 199.8k and 199.8k requests per second, this branch 210.3k; `get -P 1`: `main` 222.0k and 200.0k, this branch 222.2k; `get -P 16`: `main` 1,996k and 1,996k, this branch 1,996k. With no watcher the command path gained one load of a counter, and it shows nowhere; the cost with watchers is a line formatted and offered per command, which was not measured against a load (section 34 says so). The full differential (947 cases + 8, 0 differ) and all 18 mutants of `tests/monitor_mutants.py` were run on the final code.

## 35. The snapshot (epic #8, issue #21, slice 1: the format, the load, and a blocking `SAVE`)

**What this slice is.** A file format for everything the store holds, a load of it before the first connection is accepted, and `SAVE` (blocking) to write it. `BGSAVE`, the `save <seconds> <changes>` policy, `LASTSAVE`, `INFO persistence`, `SHUTDOWN SAVE` and the append-only log are later slices of #21; a blocking `SAVE` stalls every client for as long as it takes to write the arena, which is why it is the slice that proves the format and not the one that ships it. This section says so at the top because the stall is the project's whole claim and this slice does not keep it for `SAVE`.

**The file** (`dump.cho` in the directory `--dir=` names; `src/persist.cho` is the one place that knows it). All numbers are little-endian. A 32-byte header: `CANCHODB`, the version (1), the number of keys, when it was written (Unix ms). Then one record per key that has not run out: 32 bytes (key length, value length, kind, when the key ends as Unix ms or 0) and then the key's bytes and the value's. Then the CRC-64/XZ of everything before it, 8 bytes. Strings and hashes are written as the store holds them (a hash is its packed record), so a round trip is the identity on what the store keeps. Expiry is absolute: the time the cache was down counts against its keys, and a key that ended meanwhile is not loaded (this is also what Redis's RDB does).

**Writing it crash-safely.** `dump.tmp` is created (a left-over one from a crash is removed first), written, `fsync`ed, renamed over `dump.cho`, and the directory is `fsync`ed. Every step goes through the handle `open_dir` returned and one name at a time (`dir_open_new_mode`, `dir_rename`, `dir_sync`): no path is spent after the directory is opened and no link is followed. A failure at any step leaves the old `dump.cho` as it was and answers `-ERR the snapshot could not be written (errno N)`.

**Reading it.** The file is read whole (at most 1 GiB, else `persist.too-large`), its checksum is checked before anything is parsed, and then each record is bounds-checked against the file before it is used. A file that is wrong in any way stops the start (exit status 4) with `refusing to start: <rule tag>` on standard error and loads nothing it cannot vouch for: `persist.truncated`, `persist.bad-magic`, `persist.version`, `persist.checksum`, `persist.record-bounds`, `persist.count`, `persist.bad-kind`, `persist.no-room` (the data does not fit this arena; keys are not dropped silently), `persist.too-large`, `persist.io`. A missing `dump.cho` is a first start; a directory that cannot be opened is `persist.io`. The listener is already bound while the file loads (the line `listening on N` comes first) but nothing is accepted until it has.

**What `tests/persist.py` checks.** A randomised dataset (300 keys: strings of every size class from empty to 100,000 bytes, binary keys and values, hashes, expiries) through `SAVE` and a restart, key by key and TTL by TTL; the records of a second snapshot of a loaded cache are byte-identical to the first's (the format is canonical); a file cut at twelve lengths, forty single-bit alterations at random places, and files that are right outside and wrong inside (a valid checksum over a wrong magic, version, count, kind, length or value too large for the arena) each refuse to start with their own tag and exit status 4, never a crash; `SAVE` without a directory, with an argument, over a left-over temporary file; a missing directory. The order of the calls is read from the kernel's side (`strace`, installed in CI): the temporary file is created, `fsync`ed, renamed over `dump.cho`, and the directory `fsync`ed, in that order. `tests/persist_mutants.py` (27) must be failed by it.

**What this changes about authority, said plainly.** The report used to say the program reads one named file. A directory chosen by an argument cannot be named in a type (`narrow` takes a literal; cancho#330), so the report now says `fs_read("")` and lists `dir_read`, `dir_write`, `file_read` and `file_write`; it still has no `fs_write`, because nothing is written through a path. The code opens `--dir` once and works beneath that handle, and no test can show the report proves that; it is a property of the code, checked by the tests above and not by the type system. The README says this in the authority paragraph, and CI's authority step now asserts `fs_read("")`, `dir_write` and no `fs_write`.

**Not in this slice, and a known cost.** The values are copied from the arena into the file as they lie, so a `SAVE` of a large arena blocks for as long as the disk takes; no stall number is claimed here. The next slice's design choice (an incremental `BGSAVE` that fixes the arena's end and copies before overwriting) is described in the issue and will be measured, not assumed.

**Checks run on the final code, and what they showed.** CI ran the full differential (951 cases + 8 small-index cases, four framings each) and every harness on the snapshot branch: green. The nine files of mutants, 199 in all, ran as nine parallel jobs after the one job they had shared passed its hour (the run on this PR was cancelled at 60 minutes; `.github/workflows/mutants.yml` is split now), and all were caught; a first local pass had missed five of the snapshot's 27 (a key that ended while the cache was down, a `SAVE` that fails, an ended key too big for the arena, each byte of the checksum, and the order of the `fsync`s), each now has the check that catches it.

**Throughput gate.** `bench/vs_redis.sh`, `main` against this branch, alternating, the cache's own medians (Redis moved between 190k and 200k requests per second at depth 1 on this machine, so ratios to it are not repeated here): `set -P 1` 210.3k on both; `get -P 1` 210.3k and 221.9k on `main`, 222.0k on this branch; `get -P 16` 1,992k on `main` and 1,996k on this branch; `set -P 16` 1,330k on both. The loop now passes a filesystem capability through `step`, `process` and `execute_in`; with no `--dir` that costs one more argument in three calls a turn, and it shows nowhere. Not every cell was run in both directions: two attempts at the whole gate were killed by a restart of the machine's worker before they finished, and the cells above are the ones that completed.

## 36. `BGSAVE`: a snapshot written a slice a turn (epic #8, issue #21, slice 2)

**The claim, and what it does not claim.** `BGSAVE` writes the snapshot of section 35 without stopping the cache: a slice of about 256 KiB per turn, then the loop goes on to its clients. Redis's `BGSAVE` is a point-in-time copy made with `fork`; cancho has no `fork`, and copying every record aside before any write could change it (the other way to get a point in time) would put a hook on every write path of the store. The choice made (by the project's owner, between that, a fuzzy save and doing nothing for now) is the cheap one, stated first so that nobody reads more into the file than is there:

> **Every key in the file is whole, and was that key's value at some moment during the save. The file is not one moment.** A key changed after its turn is saved as it was, one changed before as it became; a key deleted during the save may or may not be in it; a key created after the save began is not in it. A restart from it gives a cache that is a valid cache, not one that equals a prefix of the command history, so the issue's crash-prefix gate is restated for `BGSAVE` (below) and still holds, unchanged, for `SAVE`.

**How.** The state of a save is twelve ints of the loop's `world` (`stats.persist_at()`): whether one is running, the next entry to look at, the running CRC-64, the bytes and records written, when it began, when the last one ended and how. Nothing else is kept between turns, not even a file: every slice opens the directory (`open_dir`, one call) and the file (`dir_open_append`), writes, syncs and closes them, which costs two syscalls and is nothing next to the slice. A record is written whole within a slice, so a key is never torn; the entries a save looks at are the ones that existed when it began (new ones are not chased). The header's count of keys is `-1` for such a file (it is not known until the end) and `load` reads to the end instead; the count of a `SAVE`'s file is still checked.

**Two stalls found by measuring, and what was done.** The first version wrote the slices and then did one `fsync` and one `rename` at the end. `strace -T` found the two outliers: the `fsync` of the whole file, 100 MiB of dirty pages at once (167 ms), and `rename` over the old `dump.cho`, which frees the old file's blocks before it returns (about 50 ms; it grows with the file, so for a gigabyte arena it would have been half a second, which would have made the claim false). So each slice is synced as it is written, and the old snapshot is no longer replaced: it is renamed to `dump.old`, the new one renamed to `dump.cho`, and the old one is given back 4 MiB a turn (`file_truncate`, then removed). Between the two renames there is no `dump.cho`; `load` then takes `dump.old`, and with both it takes `dump.cho`. Both cases are tested, and so are the order of the calls (`strace`) and a `kill -9` in the middle of a save.

**Measured** (`bench/bgsave_stall.py`: 300,000 keys of 300 bytes, about 97 MiB of records, one client asking `PING` as fast as it can while another does the thing named; this machine, a shared VM with a disk of unknown make):

| while another client | PINGs | median | p99 | longest |
|---|---|---|---|---|
| does nothing | 179,733 | 0.03 ms | 0.06 ms | 4.31 ms |
| runs `BGSAVE` again and again | 3,165 | 2.16 ms | 3.50 ms | **12.11 ms** |
| runs `SAVE` (blocking) | 36,978 | 0.03 ms | 0.08 ms | **727 ms** |

Read it carefully. The median wait of a `PING` during a `BGSAVE` is 2 ms, not 0.03: every turn writes and syncs a slice, and a request that arrives during a turn waits for it. That is the price of the slice, and it is the knob (`persist.step`'s budget, 256 KiB; a smaller one lowers it and makes a save take longer). The longest wait was 167 ms before the two changes above, then 60 ms, then 12 ms; the remaining outliers are the disk's own (a single slice's `fsync` took 25 to 36 ms twice in the `strace` run, and the longest `PING` of a run is whichever one met such a sync). A 4 ms outlier in the line that does nothing says this machine alone produces some. `SAVE` stalls everyone for as long as it takes to write the whole arena, 727 ms for 97 MiB here, in proportion to the arena. What is *not* bounded: a single record is written whole in one slice, so a value of 100 MiB takes one slice of that size (a `SET` of one is already one such stall).

**What else this adds.** `LASTSAVE`; `INFO persistence` with the fields of Redis that are true (`rdb_bgsave_in_progress`, which is 1 until the old file has been given back too, `rdb_last_save_time`, `rdb_last_bgsave_status`, `rdb_last_bgsave_time_sec`, `rdb_current_bgsave_time_sec`); `BGSAVE [SCHEDULE]`; `SAVE` and `BGSAVE` refuse while one is running, as in Redis. `rdb_changes_since_last_save` is still 0 and means nothing until the `save` policy of slice 3 counts writes.

**Checks.** `tests/persist.py` now also runs a `BGSAVE` over 300,000 keys while 1,000 keys are changed, 1,000 deleted and 1,000 created: the keys nobody touched are all there and as they were, each changed key is whole (old or new), each deleted one is gone or whole, each created one is absent or whole; the longest `PING` is under 50 ms; a second `BGSAVE` and a `SAVE` are refused; `LASTSAVE` moves; `kill -9` in the middle leaves the last whole snapshot; the next `BGSAVE` removes the left-over temporary file; a save whose temporary file is taken from under it fails, says so, and leaves the snapshot; a `BGSAVE` that cannot start says so. `tests/persist_mutants.py` has 42 mutants now.

## 37. The `save` policy, the changes counter and `SHUTDOWN` (epic #8, issue #21, slice 3)

**What it does.** A cache that is given a directory (`--dir=`) can now save by itself, the way Redis does with `save <seconds> <changes>`: when that many writes have happened since the last save and that many seconds have passed since it, a `BGSAVE` (section 36) is started by the loop. `SHUTDOWN` saves first if a snapshot is wanted, and stops the cache. `rdb_changes_since_last_save` is a real number.

**The policy.** Up to eight pairs (`store.save_pairs()`; Redis keeps any number, and a ninth is refused with a message that says why rather than dropped). The default is empty, which is not Redis's default (`3600 1 300 100 60 10000`): a cache that starts saving to disk because nobody said otherwise is a surprise, and this one has been given no directory by default either. `--save=900:1,300:10` sets it at start (pairs of `seconds:changes`, separated by `:` or `,` or spaces; a wrong one stops the start, status 2); `CONFIG SET save "900 1 300 10"` changes it at run time and `CONFIG GET save` answers it in Redis's spelling. What `CONFIG SET save` refuses is what Redis refuses (an odd count of numbers, 0 seconds, a negative or a non-number), with `Invalid save parameters`; it was checked against Redis 7.0.15, which also accepts `""` (no policy) and `900 0` (every 900 seconds, whether anything changed or not), and so does this.

**When a pair is due**, checked once a turn while no save is running: `dirty >= changes` and `now - lastsave >= seconds`, and after a *failed* save also five seconds since it was tried, so that a full disk is not retried every turn (Redis's `serverCron` has the same three tests). `lastsave` is the end of the last save, or the start of the cache. This five-second delay is the one part of the rule no test here reaches: the failure is tested (the directory is removed under a running cache; the status becomes `err` and the writes stay counted), the retry interval is not, because nothing observable distinguishes five seconds from none without counting attempts, and the cache does not count them. It is stated here so that nobody reads it as tested.

**The counter.** `dirty` is one more for each command that Redis flags `write` (the generated tables carry the flag: `cmdnames.is_write`), ran, and did not answer with an error or the integer 0 (`DEL` of nothing, a `SETNX` that did not set). **One for a command, not one for each key it touched**: Redis counts `MSET a 1 b 2 c 3` as three, this cache as one. That only matters to someone who sets a policy with a change count and writes many keys per command; it is written here rather than hidden. Commands inside `MULTI` count when `EXEC` runs them and not when they are queued; `DISCARD` counts nothing. A write that the policy's save did not see (it came after the save began) stays counted: the save settles exactly the `dirty` it began with (`job[12]`), not zero. A failed save settles nothing. `SAVE` and the `SHUTDOWN` save, being whole, settle everything.

**`SHUTDOWN [NOSAVE | SAVE] [NOW] [FORCE] [ABORT]`.** It saves if `SAVE` was given, or if a policy is set and `NOSAVE` was not (Redis's rule), and then the loop ends: the connection is closed with no reply, as in Redis, and the exit status is 0. A save under way is dropped and a whole one is written by the command (the one under way would be old). If the save fails, the answer is `-ERR Errors trying to SHUTDOWN. Check logs.` and the cache stays up; `NOSAVE` is how to stop it anyway. `SAVE` with no directory is that error too. `NOW` and `FORCE` are taken and mean nothing (there are no replicas to wait for, and a failed save is never overridden, which is stricter than Redis's `FORCE`). `ABORT` answers `ERR No shutdown in progress.`, as Redis does when there is none, and there never is one: a shutdown here is not asynchronous. `SHUTDOWN SAVE NOSAVE` and any other word are `ERR syntax error`.

**Checks.** `tests/policy.py` (the policy and `SHUTDOWN` above, with real waits: a pair of 1 second and 2 changes does nothing for 2.5 s on one write, saves on the second, saves nothing more when nothing changes; a pair of 3 seconds is not due at 1.2 s) and `tests/policy_mutants.py` (33 mutants). The differential suite does not compare this slice against Redis: its subject is time and files.

**What the mutants found, and what was done.** The first run of the 35 let five through. Two were real holes: nothing showed that a save which *fails part way* leaves the writes counted (the test failed the save at its start, where nothing is settled either way), and nothing showed that `SAVE` settles them. Both now have checks (a save over 40 MB whose directory is removed while it runs; `SAVE` after a write). One was a gap in the message (`900x 1` was refused, but as "too many", not as invalid). Two were equivalent in behaviour and were removed with the code they guarded: a `digits` count that the next test already implied, and a zeroing of the output that both callers had already done (the contract now says the caller gives zeros).

**Throughput, measured with the policy and the counter in the loop** (`bench/vs_redis.sh`, 3 rounds of 500,000 requests, 3-byte values, the server on one core and the benchmark on two others; this machine, a shared VM, measured after a container restart): `SET` at depth 1, Redis 181,554 and cache 199,840 requests a second (1.10); `GET` at depth 1, 181,554 and 199,840 (1.10); at depth 16 both servers answer about 1,000,000 a second for `SET` and for `GET` (1.00). The depth-16 cells sit on the same ceiling for both servers, so they show that the counter and the per-turn policy check cost nothing the benchmark can see there; they do not rank the two servers, and the script's wider-client check for such a cell was not read into this claim. The gate of section 2 is not restated by this run: it is 3 rounds, not 5, and 500,000 requests, not 1,000,000. What it does say is that a write now does one more addition (`add_dirty`) and one table lookup (`is_write`) and the turn eight comparisons, and the depth-1 result did not move from the 1.10 of the previous slices.

## 38. The append-only log: the design (epic #8, issue #21, slice 4; no code yet)

This section is the design and the measurements that choose it. Nothing here is built; section 39 and the ones after it say what was.

**The question.** A snapshot (sections 35 to 37) loses everything since the last one. The log keeps `appendfsync always | everysec | no` of that: a file the cache appends to as it writes and replays at start. Redis logs the *commands* (rewritten so that they replay the same). Two things in this cache make that the wrong choice, and the design follows from them.

1. **There is no `fork`, so the rewrite is fuzzy.** Redis rewrites its log by forking a point-in-time image and logging the commands that arrive meanwhile on top of it. Section 36's snapshot is not a point in time: it is made of whole keys, each true at some moment of the save. Replaying *commands* over such a base is wrong: `INCR k` that happened after the save began, replayed over a base whose `k` already includes it, counts twice. Replaying *the state a command left* is right whatever the base holds, because it says what the key is, not how it got there.
2. **A cache evicts.** Replaying commands under a full arena makes the cache choose its own victims again, with different clocks and a different random stream, so a restart could drop the hot keys and keep the cold ones. What was evicted has to be in the file.

**The decision: log the state, not the command.** After a write command ran and changed something, the log gets, for each key the command named (`cmdkeys.key_spec`, the table generated from Redis), one record: *the key as it now is* (kind, value, absolute expiry in Unix ms), or *the key is gone*. These are the records of the snapshot (section 35, `put_head`) with two more kinds: *deleted* (key only) and *expiry only* (key and expiry), the latter so that `EXPIRE`, `PERSIST` and the like do not copy a value to change a deadline. `FLUSHALL` and `FLUSHDB` are a record of their own. An evicted key is a *deleted* record, put in the log by the same loop that finds it was evicted. A key that ended by its time needs no record: its expiry is in the file as an absolute time, and replay is run on the clock of the log (below), so it ends at the same moment.

**Why that converges, stated so it can be tested.** The state of a key after replay is the last record about it, or, if there is none, what the base holds. A record is idempotent (the same record twice is the same state), so a base that is a fuzzy snapshot started at time *T*, plus every record logged from *T* on, gives for each key: the last thing the log says, or, if the log says nothing since *T*, the value the key had throughout the save. That is the state the cache had at the end of the log, key by key. It is also what makes the rewrite possible without a `fork` (below).

**What it costs, and where it is wrong to use.** A record is the whole value. `SET k v` of a 3-byte value is a 32-byte head, the key and the value: about 51 bytes for a 16-byte key, the same size as the RESP command (`*3\r\n$3\r\nSET\r\n...`, about 45). `APPEND` of one byte to a 1 MiB value, or `HSET` of one field in a hash of 100,000, logs the whole key: **write amplification proportional to the value, where Redis's is proportional to the command**. A workload of small values (what the project's claim is about) is the same; a workload that grows a big value a byte at a time is not, and the section that builds this says so in the README's limits. A field-level record for hashes is the obvious follow-up and is not part of this slice.

**The file.** `appendonly.cho` in the directory of `--dir=`, written through the same directory handle as the snapshot (`dir_open_append`; the authority report does not change). A header (magic `CANCHOLG`, the version, the Unix ms at which the log began), then *batches*. A batch is what one turn of the loop appended: its records, then a trailer of the batch's length, the Unix ms of the turn and the CRC-64/XZ of the batch. **The time matters**: replay sets the store's wall clock to each batch's time before applying it, so the expiry of a key replays against the clock it was written under, not the clock of the restart; a key that was due before the crash is due at replay, and gone when the cache opens. A batch is one `write`, so one turn's records are never half in the file except by a crash.

**Replay at start.** The log is read after the base (the snapshot of section 35, if there is one) and before the first `accept`. A batch whose trailer is not there or does not match, **at the end of the file**, is the write the crash cut: it is dropped, the file is cut back to the last whole batch, and the cache starts. The same fault **anywhere else** is not a crash but damage: the start is refused with a rule tag, as for a snapshot. (Redis's `aof-load-truncated` is the first half of this; it has no second half, it fails on a bad middle too.)

**The three policies, and what each stalls.** The loop appends the turn's batch once, at the end of the turn, whatever the policy.
* `no`: the batch is `write`n and the kernel syncs when it likes. Costs a `write` a turn.
* `everysec`: as `no`, and the file is `file_sync`ed when a second has passed since the last. The cache has no thread to do that on, so **the sync is in the loop**. What it costs is the sync's latency, measured below; it stalls every client for that long, once a second. Redis does this on a background thread and a stall only happens if the thread falls behind. A thread is not available to this program (`docs/processes.md`), so the claim is a smaller one: *bounded by the disk's sync time, once a second*, and the README says what it measured.
* `always`: the batch is `write`n and synced **before the replies of that step are sent**. The step already holds its replies in a buffer until it sends them, so this is one `file_sync` between executing and sending, per connection step: `PIPELINE` depth gives the amortisation Redis's own `always` gets. Throughput is the disk's.

**The rewrite (`BGREWRITEAOF`) is the incremental `BGSAVE` of section 36 with the log switched at its start.** At begin, the loop opens a new log file and writes to it from then on; the snapshot being written is the new base. When the save finishes, a manifest (a small file, written the crash-safe way of section 35) is replaced to name the new base and the new log, and the old two are removed a slice at a time as `dump.old` is. A crash at any point leaves a manifest that names a consistent base and its logs: before the replace, the old pair; after, the new. By the argument above, the new base plus the new log is the state at the end. **This is the property the fuzzy snapshot and the state log have together and neither has alone**, and it is the reason for the whole design.

**Measured first: the latency of the sync, on this machine** (a shared VM, ext4 on a virtual disk, idle; Python's `os.fsync` after an `O_APPEND` write, 2,000 repeats, 300 for 256 KiB):

| appended before the sync | p50 | p99 | longest |
|---|---|---|---|
| 100 B | 0.15 ms | 0.35 ms | 2.61 ms |
| 4 KiB | 0.23 ms | 0.70 ms | 2.58 ms |
| 256 KiB | 0.60 ms | 2.85 ms | 5.86 ms |
| 4 KiB, `fdatasync` | 0.22 ms | 0.49 ms | 2.05 ms |

A `write` of 100 bytes without a sync: median 0.8 µs, p99 2.7 µs, longest 38 µs. **Read these carefully.** They say a sync here is cheap, which is a property of this machine's disk (a virtual one whose cache is far from the media), not of disks. Section 36's own `strace` found a single slice's `fsync` at 25 to 36 ms twice, on the same machine, with 256 KiB of dirty pages queued behind other writes; a log with a busy disk will see those. Nothing in this section is a claim about a disk that is not this one, and the `everysec` stall is stated in the README as "the sync's time, once a second", with these numbers beside it, not as a number of its own.

**What is not decided here, and what would change the design.**
* *Field-level records* for hashes (above) change the amplification claim, not the shape.
* *RDB compatibility* of the base is still the separate, later question of the issue.
* If the `everysec` sync turns out to stall by tens of milliseconds on a disk that is not this one, the answer is a thread (if the language gets one for a program like this) or sync in slices; neither is promised.

**The slices, each a PR with its own gate:** (a) writing: the records, the batches, `no` and `everysec` and `always`, `CONFIG SET appendonly`/`appendfsync`, `--appendonly=`, `INFO persistence`'s `aof_*` fields, the file read back by the test; (b) replay at start, the torn tail, the damaged middle, and the issue's crash gate (`kill -9` at every phase, and the loaded dataset equals a prefix of the command history); (c) `BGREWRITEAOF` and the manifest, with the stall measured as section 36's was. Each states what it measured and what it did not.
