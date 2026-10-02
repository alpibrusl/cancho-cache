# lexsys-cache

A Redis-compatible cache written in [lex-sys](https://github.com/alpibrusl/lex-sys): no `Ffi`, no `unsafe`, and a
checkable authority report.

**Status: step C1.** `PING`, `ECHO`, `GET`, `SET` (no options yet), `DEL` and `EXISTS` over a fixed-size store, the rest refused as
Redis refuses it. At 3-byte values on one core it is at 1.18-2.00 times Redis 7.0.15 on the pre-registered cells (and 1.00-1.25 on a
100,000-key keyspace), doing much less than Redis does; expiry and eviction are C2. `docs/design.md` has the plan, the pre-registered gate the cache is held to, the
measured Redis baseline, and what C0 found.

```sh
lex-sys build --std src/cache.ls src/resp.ls src/store.ls -o build/cache
build/cache 6379
redis-cli -p 6379 PING                                   # PONG

lex-sys test tests/resp_test.ls src/resp.ls --std        # the parser, over every string up to six bytes
lex-sys test tests/store_test.ls src/store.ls --std       # the store: collisions, deletion, a random run against a model
python3 tests/differential.py build/cache                # byte-for-byte against redis-server
python3 tests/limits.py build/cache                      # a full arena and a full key table
python3 tests/fuzz_server.py build/cache                 # hostile bytes
bench/vs_redis.sh -t set,get -P "1 16"                  # against redis-server, one core each (-k 100000 for a keyspace)
```

Licence: EUPL-1.2.
