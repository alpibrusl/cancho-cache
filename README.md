# lexsys-cache

A Redis-compatible cache written in [lex-sys](https://github.com/alpibrusl/lex-sys): no `Ffi`, no `unsafe`, and a
checkable authority report.

**Status: step C0.** The protocol and the loop answer `PING` and `ECHO` and refuse the rest as Redis does; there is no
storage yet (`GET`/`SET` is C1). `docs/design.md` has the plan, the pre-registered gate the cache is held to, the
measured Redis baseline, and what C0 found.

```sh
lex-sys build --std src/cache.ls src/resp.ls -o build/cache
build/cache 6379
redis-cli -p 6379 PING                                   # PONG

lex-sys test tests/resp_test.ls src/resp.ls --std        # the parser, over every string up to six bytes
python3 tests/differential.py build/cache                # byte-for-byte against redis-server
python3 tests/fuzz_server.py build/cache                 # hostile bytes
bench/vs_redis.sh -t ping_mbulk -P "1 16"                # against redis-server, one core each
```

Licence: EUPL-1.2.
