#!/bin/bash
# The gate of docs/design.md section 2: the cache against Redis on one core, interleaved in the same session.
#
#   bench/vs_redis.sh [-t set,get] [-P "1 16"] [-d 3] [-r 5] [-n 1000000] [path/to/cache]
#
# Server pinned to core 0, redis-benchmark (2 threads, 50 clients) to cores 2-3. For every test and pipeline depth,
# `r` rounds, each round one run of Redis then one of the cache; prints every run, the medians and the ratio.
# Client headroom: once per cell the faster of the two servers is also run with a wider client (cores 1-3, 3 threads); if that
# is more than 5% faster the cell is reported CLIENT-BOUND and no ratio is to be quoted for it.
tests=set,get; depths="1 16"; size=3; rounds=5; n=1000000
while getopts t:P:d:r:n: o; do case $o in t) tests=$OPTARG;; P) depths=$OPTARG;; d) size=$OPTARG;; r) rounds=$OPTARG;; n) n=$OPTARG;; esac; done
shift $((OPTIND - 1)); cache=${1:-$(dirname "$0")/../build/cache}
rp=6395; cp=6396
taskset -c 0 redis-server --port $rp --save "" --appendonly no --protected-mode no > /dev/null 2>&1 & rpid=$!
taskset -c 0 "$cache" $cp > /dev/null 2>&1 & cpid=$!
trap 'kill $rpid $cpid 2>/dev/null' EXIT
sleep 1
bench() { # port test depth cores threads -> requests per second
  taskset -c "$4" redis-benchmark -p $1 -c 50 -n $n -P $3 -d $size -t $2 -q --threads "$5" 2>&1 | tr '\r' '\n' | grep 'requests per second' | tail -1 | sed -E 's/^[^:]*: ([0-9.]+) requests.*/\1/'
}
median() { sort -n | awk '{a[NR]=$1} END {print a[int((NR+1)/2)]}'; }
for t in ${tests//,/ }; do for P in $depths; do
  r=(); c=()
  for i in $(seq $rounds); do r+=("$(bench $rp $t $P 2,3 2)"); c+=("$(bench $cp $t $P 2,3 2)"); done
  rm_=$(printf '%s\n' "${r[@]}" | median); cm=$(printf '%s\n' "${c[@]}" | median)
  fast=$cp; fm=$cm; if awk -v a="$rm_" -v b="$cm" 'BEGIN {exit !(a > b)}'; then fast=$rp; fm=$rm_; fi
  wide=$(bench $fast $t $P 1,2,3 3)
  note=""; if awk -v w="$wide" -v m="$fm" 'BEGIN {exit !(w > 1.05 * m)}'; then note=" CLIENT-BOUND (a wider client reached $wide): no ratio"; fi
  ratio=$(awk -v a="$cm" -v b="$rm_" 'BEGIN {printf "%.2f", a / b}')
  printf '%-10s P=%-3s d=%-4s redis %s | cache %s | median redis %s cache %s ratio %s%s\n' "$t" "$P" "$size" "$(printf '%s ' "${r[@]}")" "$(printf '%s ' "${c[@]}")" "$rm_" "$cm" "$ratio" "$note"
done; done
