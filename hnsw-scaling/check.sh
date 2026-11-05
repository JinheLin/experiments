#!/usr/bin/env bash
set -euo pipefail
data=${1:-data}
release=${2:-target/release}
for pair in 4096:16 4096:64 65536:64 524288:64; do
    n=${pair%:*}
    ef=${pair#*:}
    index="$data/laion-$n.usearch"
    printf '%s\n' "rows=$n,ef=$ef"
    numactl --physcpubind=0 --membind=0 "$release/hnsw-scaling" \
        recall "$index" "$data/queries.f32" "$data/truth-$n.csv" "$ef"
    numactl --physcpubind=0 --membind=0 "$release/hnsw-scaling-diagnostic" \
        work "$index" "$data/queries.f32" "$ef"
done
