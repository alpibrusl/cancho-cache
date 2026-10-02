# lexsys-cache

A Redis-compatible cache written in [lex-sys](https://github.com/alpibrusl/lex-sys): no `Ffi`, no `unsafe`, and a
checkable authority report.

**Status: step C2.** Thirty commands of Redis 7.0's 240 (`GET`/`SET` with `NX XX GET EX PX KEEPTTL`, `INCR`/`DECR`/`INCRBY`/`DECRBY`, `EXPIRE`/`PEXPIRE` with
`NX XX GT LT`, `TTL`/`PTTL`/`PERSIST`, `MGET`/`MSET`, `SETNX`/`SETEX`/`GETSET`/`GETDEL`, `DEL`/`EXISTS`/`TYPE`/`STRLEN`, `DBSIZE`/`FLUSHALL`, ...), expiry, and an eviction
policy (`allkeys-lru`). On one core against Redis 7.0.15 it is at 1.07-2.00 times Redis's throughput and the same hit rate at the same resident memory, doing much less
than Redis does; its worst case is a compaction pause (about 115 ms on a 64 MiB arena). It is **not** a Redis replacement: no data structures, persistence, replication or
TLS, one database, and some clients' handshakes will not work. `docs/design.md` has the plan, the pre-registered gate, and every measurement with its caveats.

```sh
lex-sys build --std src/cache.ls src/resp.ls src/store.ls src/commands.ls -o build/cache
build/cache 6379
redis-cli -p 6379 PING                                   # PONG

lex-sys test tests/resp_test.ls src/resp.ls --std        # the parser, over every string up to six bytes
lex-sys test tests/store_test.ls src/store.ls --std       # the store: collisions, deletion, a random run against a model
python3 tests/differential.py build/cache                # byte-for-byte against redis-server
python3 tests/limits.py build/cache                      # a full arena and a full key table
python3 tests/expiry.py build/cache                      # expiry, the sweep, eviction under allkeys-lru
python3 bench/memory.py build/cache                      # bytes per key against Redis
python3 bench/hitrate.py build/cache                     # hit rate on a Zipf workload at equal resident memory
python3 bench/stall.py build/cache 64                    # the worst-case compaction pause
python3 tests/fuzz_server.py build/cache                 # hostile bytes
bench/vs_redis.sh -t set,get -P "1 16"                  # against redis-server, one core each (-k 100000 for a keyspace)
```

Licence: EUPL-1.2.
