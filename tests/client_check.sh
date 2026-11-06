#!/bin/bash
# `client` against a real cancho-cache and against a server that never answers. CANCHO is the compiler (default: cancho on the path). The cache is built here.
cd "$(dirname "$0")/.." || exit 2
C="${CANCHO:-cancho}"
mkdir -p build
"$C" build --std src/cache.cho src/resp.cho src/store.cho src/commands.cho src/reply.cho src/session.cho src/txn.cho src/glob.cho -o build/cache 2>&1 | grep -v "^ld:\|^warning"
"$C" build tests/client_check.cho src/client.cho src/client_wire.cho --std -o build/client_check 2>&1 | grep -v "^ld:\|^warning"
[ -x build/cache ] && [ -x build/client_check ] || { echo "could not build"; exit 2; }
P=$((20000 + RANDOM % 20000)); S=$((P + 1))
build/cache "$P" >/dev/null 2>&1 & cpid=$!
python3 - "$S" <<'PY' & spid=$!
import socket, sys, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(4)
conns = []
while True:
    c, _ = s.accept(); conns.append(c)   # accepted, read by nobody, answered never
PY
trap 'kill $cpid $spid 2>/dev/null; wait 2>/dev/null' EXIT
for _ in $(seq 1 60); do (exec 3<>"/dev/tcp/127.0.0.1/$P") 2>/dev/null && break; sleep 0.1; done
for _ in $(seq 1 60); do (exec 3<>"/dev/tcp/127.0.0.1/$S") 2>/dev/null && break; sleep 0.1; done
echo "== against cancho-cache"; build/client_check 127.0.0.1 "$P"; a=$?
if command -v redis-server >/dev/null 2>&1; then
    R=$((P + 100)); redis-server --port "$R" --save "" --appendonly no --bind 127.0.0.1 >/dev/null 2>&1 & rpid=$!
    trap 'kill $cpid $spid $rpid 2>/dev/null; wait 2>/dev/null' EXIT
    for _ in $(seq 1 60); do (exec 3<>"/dev/tcp/127.0.0.1/$R") 2>/dev/null && break; sleep 0.1; done
    echo "== against a real Redis ($(redis-server --version | cut -d' ' -f1-3)): the same replies"; build/client_check 127.0.0.1 "$R"; d=$?
else
    d=0; echo "== no redis-server on this machine: the comparison with a real Redis is skipped"
fi
echo "== against a server that never answers"; build/client_check 127.0.0.1 "$S" silent; b=$?
echo "== against a port nobody listens on"
out=$(build/client_check 127.0.0.1 $((S + 1)) 2>&1); echo "$out" | grep -q "FAIL connected" && { echo "ok   a refused connection is reported"; c=0; } || { echo "FAIL a refused connection is not reported: $out"; c=1; }
[ $((a + b + c + d)) -eq 0 ] && echo "all passed" || echo "FAILED"
exit $((a + b + c + d))
