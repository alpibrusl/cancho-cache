# lexsys-cache: a Redis-compatible cache in lex-sys

Status: **C0, C1 and C2 built** (sections 8, 9, 10); compaction and the client handshake improved in sections 11 and 12. The numbers in section 2 are measurements of Redis; section 8's are of this project.

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

## 10. C2, built and measured: expiry, eviction, and thirty commands

**What exists.** `src/store.ls` now writes records with an 8-byte header (entry number and size), which lets the arena be walked from the
front: **compaction** slides the live records down in place, and **eviction** under `allkeys-lru` samples five keys and removes the one used
longest ago, reclaiming a sixteenth of the arena at a time so that a compaction is paid for once per sixteenth written. Expiry is lazy (a key
whose time has passed is gone when asked for) and swept (up to a hundred times a second, 256 keys a look, repeating while more than a quarter of
what it looks at has run out: Redis's rule). `src/commands.ls` is the commands: `PING ECHO GET SET` (with `NX XX GET EX PX KEEPTTL`)
`SETNX SETEX PSETEX GETSET GETDEL MGET MSET DEL UNLINK EXISTS TOUCH STRLEN TYPE INCR DECR INCRBY DECRBY EXPIRE PEXPIRE` (with `NX XX GT LT`)
`TTL PTTL PERSIST DBSIZE FLUSHALL FLUSHDB SELECT`: **30 of Redis 7.0's 240**, with Redis's argument checks and error texts, including every
overflow case of 64-bit integers.

**Correctness.**
* `tests/differential.py`: **111 cases + 8 small-index cases**, four framings each, byte-identical to Redis. Five divergences are deliberate and asserted
  as divergent (inline commands, a bulk not followed by CRLF, more than 64 arguments, `EXAT`/`PXAT`/`EXPIREAT`, and `SELECT 1`: one database).
* Mutation: **nine broken command layers** were each caught (a `TTL` that rounds down, an overflow check off by one, `INCR` accepting `+7`, `GT` on a key
  with no expiry, `NX` ignored, `SET ... GET` replying before the write, `KEEPTTL` ignored, `MSET` with an odd argument count, `PERSIST` that does not
  persist); **twelve broken stores** (compaction without the offset check, without re-pointing, never compacting, evicting the newest, the expiry boundary
  off by one, no use stamp, and others) were each caught by `tests/store_test.ls` (12 tests, including a model that knows exactly when a `SET` must be
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
copy (about 0.5 GB/s) because lex-sys has no memmove primitive; two ways out, neither built: a `copy_within` builtin (a language change, one small, and measurable), or compaction
that moves a bounded amount per turn (a design change).

**Still not Redis, stated.** 30 commands of 240; no lists, hashes, sets, sorted sets, streams, pub/sub, scripting, transactions; no persistence, replication, cluster, `AUTH`, TLS, RESP3 or
`HELLO`/`CLIENT`/`INFO`/`CONFIG` (so a client library that insists on a handshake may not connect); no `EXAT`-style absolute times; one database; at most 64 arguments and a value
that fits a 16 KiB input buffer; a constant hash seed; one core. Compaction pauses of up to ~115 ms at 64 MiB.

## 11. The compaction pause, after `copy_within`

Section 10 measured the weakness: a worst-case pause of 114.7 ms on a 64 MiB arena, from a byte-at-a-time copy (lex-sys had no block move). lex-sys now has
`copy_within(buf, dst, src, n)`, a bounds-checked `memmove` inside one slice (`lex-sys/docs/memory-moves.md`, lex-sys #186), and compaction uses it.

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

What was built (`src/session.ls`, with `src/reply.ls` holding the helpers `src/commands.ls` shares with it): `HELLO` (2 and 3, `AUTH`, `SETNAME`), `AUTH`, `CLIENT ID|GETNAME|SETNAME`, `INFO`
(five sections; `redis_version:7.0.15` is what clients read to decide which commands to try, and the command set mirrors 7.0, so that is what it says; `server_name:lexsys-cache` says what this is), `CONFIG GET|SET|RESETSTAT`,
`COMMAND|COUNT|LIST`, `QUIT`, `RESET`: **38 commands**. **RESP3** is per connection and covers what this server produces: the null (`_`), and a map for `HELLO` and `CONFIG GET`; integers, errors and strings are the same in both.

**Checks.** `tests/differential.py` is at **131 cases + 8 small-index cases**, four framings each, byte-identical to Redis, now including every `AUTH`/`HELLO`/`CLIENT`/`CONFIG GET` error text and answer and six RESP3 cases (the connection's id, which no two servers share, is normalised out
of `HELLO`'s answer). Its first run found three texts I had guessed wrongly (`NOPROTO unsupported protocol version`, `AUTH` with too many arguments is a syntax error, and a `CONFIG GET` pattern with no wildcard is echoed as typed). `tests/session.py`
checks what Redis cannot arbitrate: `COMMAND COUNT` equals `COMMAND LIST` and every listed command is answered; `INFO`'s shape and that its numbers move; ids unique and increasing; names per connection; `QUIT` closes; replies that follow a long
in-place-written one are intact (with a client that checks the CRLF after every bulk string: **an earlier version skipped those two bytes blindly, and a mutant that dropped the last one survived**); and **redis-py (RESP2, RESP3 and its default) and ioredis** connect and get through a session. Nine mutants of the session layer
were each caught. The gate benchmark is unchanged (1.23, 1.99, 1.18, 1.50 against Redis on the four cells).

**Deliberate divergences, in the harness's known list:** `CONFIG SET` is refused (the settings are fixed at start); `CONFIG GET` knows ten settings with this server's own values (`maxmemory` is the arena, `databases` is 1);
`COMMAND` and `COMMAND COUNT`/`LIST` describe 38 commands and keep no table of flags and key positions; `CONFIG GET` with several patterns answers in pattern order where Redis uses its hash table's (and its order differs between runs, so it
cannot be asserted either way); `AUTH` accepts the `default` user with any password and refuses any other, as a Redis with no password does.

**Found, not fixed: `MULTI`/`EXEC`.** redis-py's `pipeline()` is a transaction by default and sends `MULTI`, so `r.pipeline()` fails against this server (`pipeline(transaction=False)` works, and is what the test uses). Transactions were declared a non-goal; this is the case for
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
