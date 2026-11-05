# USearch HNSW read-only scaling

[技术博客（中文）](BLOG.md) · [文章副本](ARTICLE.md) · [逐轮结果](results/) · [每组中位数](results/summary.csv)

## Environment

Run on Linux x86-64 with Rust 1.92+, GCC/G++, Python 3 with NumPy and PyArrow, `numactl`, and `perf`. The recorded machine ran two Xeon Gold 6240 sockets with 18 physical cores and 36 logical CPUs per socket. See [host details](results/host.txt). The runner uses this host's CPU numbering and Cascade Lake events. Adapt its CPU lists, NUMA-node mapping and events for other hosts.

The recorded run used the official USearch Rust crate at 2.25.3, with NumKong 7.8.0 and its Skylake SIMD kernel. The data tools used VectorDBBench checkout `94aaf7c` and its Python 3.11.15 environment with NumPy 2.4.6 and PyArrow 24.0.0. The compile script disables optional NumKong targets that GCC 11 cannot compile on this host. It clears proxy settings. The dependency versions and checksums are in [Cargo.lock](Cargo.lock).

## Prepare vectors and build the graphs

```bash
cd hnsw-scaling
vdb=/DATA/disk1/jinhelin/cse-cluster/vectordbbench
py="$vdb/venv/bin/python"
dataset="$vdb/dataset/laion/laion_large_100m"
mkdir -p data results

"$py" prepare.py "$dataset" data
OPENBLAS_NUM_THREADS=5 OMP_NUM_THREADS=5 taskset -c 18-22 \
  "$py" prepare.py "$dataset" data --ground-truth
bash build.sh
# Use bash build.sh --offline once the locked crates are cached.

for n in 4096 65536 524288; do
  numactl --physcpubind=0-11 --membind=0 target/release/hnsw-scaling \
    build data/train.f32 "data/laion-$n.usearch" "$n" 12
done
numactl --physcpubind=0 --interleave=0,1 dd \
  if=data/laion-524288.usearch of=data/laion-524288-interleave.usearch bs=4M status=none
sha256sum data/*.usearch data/*.f32 > results/input-sha256.txt
```

`prepare.py` exports the first 524,288 rows of `train-00-of-100.parquet` and all 1,000 rows of `test.parquet` as little-endian float32. Index keys are row positions. Each graph indexes a prefix with 768 dimensions, squared L2, float32, connectivity 16 and build expansion 128. The builder inserts in parallel, so a rebuild can produce a different graph. All cells for a given size in the recorded experiment reuse the same graph. The interleaved file has identical bytes and a separately populated page cache.

The runner checks file page placement after prefaulting every mapping with `MADV_POPULATE_READ`. On another machine or after eviction, recreate the index files or the copy under the intended NUMA policy and check the recorded counts. File page placement persists across processes, so running `numactl` around an existing mapping alone does not relocate its cached pages.

## Run the stock Rust benchmark

```bash
python3 run.py primary results/primary.jsonl --imc --seconds 1.75 --warmup .6
python3 run.py topology results/topology.jsonl --imc --seconds 2 --warmup .6
python3 run.py short results/short.jsonl --imc --seconds 1.75 --warmup .6
python3 run.py smt results/smt.jsonl --imc --seconds 2 --warmup .6
```

Use a fresh output filename when repeating a run. Each suite runs three serial passes through a seeded shuffle of its cells. The two placement controls are independent. Workers can move between sockets, and the file pages can move from one-node placement to interleave. With `--imc`, the runner uses `sudo -n perf stat -a` for the socket memory controllers, then starts the workload under the original user. Omit `--imc` to run the benchmark using only per-process counters. The host needs access to user-space CPU counters in either case. Run the suites serially to avoid overlap.

Each pinned worker repeatedly calls the Rust API synchronously with one request in flight, using a fixed shuffled query order with a worker-specific starting offset. The warmup and index setup are outside the measured window. Search expansion is 64 except for the short-query suite, which uses 16. Every successful call must return ten matches. By default, the harness samples every 67th call for wall-clock latency and measures per-worker CPU time with `getrusage(RUSAGE_THREAD)`. It records whole-host idle and I/O-wait ticks from `/proc/stat`. The workload uses a closed loop, so these latency quantiles describe calls issued by this benchmark and exclude client-side queuing at a separately imposed arrival rate.

A shared Index reserves one USearch context per worker. With an Index per worker, each worker uses its own one-context Index view of the same file. Views share file pages and have separate lookup tables, scratch contexts, mutexes and virtual mappings. A second experiment isolates the slot mutex using one shared mapping.

## Measure graph work and bypass the slot pool

```bash
crate=$(echo "${CARGO_HOME:-$HOME/.cargo}"/registry/src/*/usearch-2.25.3)
python3 instrument.py "$crate" diagnostic
base="$PWD"
(
  cd diagnostic
  CARGO_TARGET_DIR="$base/target" bash "$base/build.sh" --offline --features diagnostic
)
bash check.sh > results/accuracy.txt
python3 run.py slots results/slots.jsonl --binary target/release/hnsw-scaling-diagnostic \
  --imc --seconds 2 --warmup .6
```

The diagnostic bridge surfaces two counters already computed in C++ and allows a worker to supply its own reserved slot ID. It leaves the graph and distance kernel intact. It uses a separate binary so the primary curves are from the stock crate. `check.sh` compares all 1,000 queries against the exact top ten for their corpus prefix and checks that pooled and explicit-slot calls agree on IDs, distances and graph work. The native `visited_members` counter measures traversal iterations. `computed_distances` counts the distance evaluations made by search.

## Calibrate the counters and generate the figures

```bash
mkdir -p results/probes
gcc -O3 -march=cascadelake -fopenmp -o target/stream-read stream-read.c
python3 probes.py results/probes
python3 summarize.py results
uv run --no-project --with matplotlib==3.11.2 python plot.py results/summary.csv figures
```

`perf stat` core events count user-space activity and are enabled only around the timed work. The IMC CSV includes six channels on each socket and counts host-wide traffic. The reference scan repeatedly reads a 1.5 GiB buffer for four seconds. It runs on one socket, on both sockets with 2 MiB stripes alternating between nodes, and on both sockets with each worker reading its local node. It binds each range before first touch and verifies physical page counts afterward. `perf mem record` samples qualifying loads with an Intel PEBS latency threshold of 30 cycles. Its weight measures load use latency, including time spent in the pipeline. The two sets of probes run separately from the throughput matrix. Bandwidth summaries use full one-second intervals after the first interval.

The JSONL files retain one record per run, raw core and IMC output and per-worker measurements. The summary CSV reports medians across repeats plus the observed minimum and maximum QPS. PEBS text can be compressed with `gzip -n` before running the summarizer. SVG figures are regenerated from the summary. The gray region in the single-socket figures marks the worker counts that use SMT siblings.

## Follow-up on the 512K index

The two layouts both use one Index view per socket, opened on that socket, and the same worker CPU lists. In the interleaved case both views map one common file. In the replica case each view maps a distinct byte-identical file with its pages on that view's node. Both sockets still search the entire graph for each query. This doubles the physical file pages of the index from about 1.57 to 3.14 GiB.

```bash
numactl --physcpubind=18 --membind=1 dd \
  if=data/laion-524288.usearch of=data/laion-524288-node1.usearch bs=4M status=none
sha256sum data/laion-524288{,-node1,-interleave}.usearch > results/replica-sha256.txt
python3 run.py replicas results/replicas.jsonl --imc --seconds 2.5 --warmup .75
mkdir -p results/probes
python3 probes.py results/probes --only replica-latency
python3 summarize.py results
uv run --no-project --with matplotlib==3.11.2 python plot.py results/summary.csv figures
```

Check that all three SHA-256 values match. The runner checks file page distribution per view and captures system-wide UPI transmitted data flits alongside IMC reads. The summary counts eight payload bytes per valid flit and adds the transmit directions of both sockets. Traffic includes the rest of the host. [Intel Cascade Lake uncore events](https://perfmon-events.intel.com/platforms/cascadelakex/uncore-events/uncore/)

## Review follow-ups on the large index

These runs reuse the same 524,288-vector graph and keep the follow-up data in `results/review/`. The longer NUMA comparison alternates local and remote placement across eight pairs, giving a Local–Remote–Remote–Local order. The graph pages stay on node 0. Each window runs for 20 seconds and times one in four closed-loop searches.

```bash
mkdir -p results/review
bash build.sh --offline
python3 run.py numa-long results/review/numa-long.jsonl \
  --repeat 8 --seconds 20 --warmup 2 --imc --telemetry --latency-stride 4
python3 run.py replica-long results/review/replica-long.jsonl \
  --repeat 4 --seconds 12 --warmup 2 --imc --telemetry --latency-stride 4

"$py" prepare.py "$dataset" data --held-out
sha256sum data/queries.f32 data/heldout-*.f32 > results/review/query-sha256.txt
for rows in 1000 16384; do
  python3 run.py replica-long "results/review/heldout-$rows.jsonl" \
    --queries "data/heldout-$rows.f32" --seconds 8 --warmup 2 --imc --telemetry --latency-stride 4
done

gcc -O3 -march=cascadelake -fopenmp -o target/stream-read stream-read.c
python3 pressure.py results/review/pressure.jsonl --seconds 8
```

The held-out banks use the first 1,000 and 16,384 vectors after the indexed prefix of the same training shard. Both banks search the original graph. Dual-socket runs now load a copy of the query bank on each socket before timing. The memory-interference test keeps 18 search workers on node 0. An eight-thread reader uses disjoint cores on node 1. Its 1.5 GiB buffer is bound and verified on either node 1 for the separate-controller control or node 0 for shared-controller pressure. The latter also adds UPI traffic.

The runner checks index pages again after timing. It records context switches, worker runtime and aggregate per-socket busy time. The review summary estimates non-worker CPU load by subtracting measured worker runtime from the relevant socket's busy time. Active user-space frequency is estimated from the ratio of core to reference cycles and the host's 2.60 GHz reference frequency. IMC and UPI counts include host traffic. [Intel counter definitions](https://perfmon-events.intel.com/platforms/cascadelakex/core-events/core/)

The slot timing runs use a separate diagnostic build. They time the two mutex acquisitions in a sampled query, including the lock call and any time the caller spends waiting to run. The timer readings add a small cost. This probe measures the lock acquisition path, while the fixed-slot experiment also changes how workers reuse their query contexts.

```bash
crate=$(echo "${CARGO_HOME:-$HOME/.cargo}"/registry/src/*/usearch-2.25.3)
python3 instrument.py "$crate" diagnostic-slot --slot-timing
base="$PWD"
(
  cd diagnostic-slot
  CARGO_TARGET_DIR="$base/target" bash "$base/build.sh" --offline --features slot-timing
)
python3 run.py slot-audit results/review/slot-audit.jsonl \
  --binary target/release/hnsw-scaling-diagnostic --seconds 5 --warmup 1.5 --latency-stride 257
python3 summarize.py results --review-only
uv run --no-project --with matplotlib==3.11.2 python plot.py \
  results/summary.csv figures --numa-pairs results/review/numa-pairs.csv
```

## Check CPU headroom and query reuse

Reuse the local index replicas and held-out banks prepared above. Build the stock
binary and refresh the generated diagnostic build after updating the Rust sources.
The diagnostic work probe runs each vector through the pooled and explicit-slot
paths and checks agreement before reporting mean graph visits and distance
evaluations. It runs separately from the timed searches.

```bash
mkdir -p results/followup
bash build.sh --offline
base="$PWD"
(
  cd diagnostic-slot
  cp "$base/src/query.rs" src/query.rs
  CARGO_TARGET_DIR="$base/target" bash "$base/build.sh" --offline --features slot-timing
)
python3 followup.py cpu results/followup/cpu.jsonl
python3 followup.py query results/followup/query.jsonl
python3 followup.py adjacent results/followup/adjacent.jsonl
python3 summarize.py results --followup-only
uv run --no-project --with matplotlib==3.11.2 python plot.py \
  results/summary.csv figures --query-reuse results/followup/summary.csv
```

The CPU sweep excludes three whole cores per socket from the search affinity at
36, 48, 54 and 60 workers. Those four configurations use the same 30 physical
cores and progressively add SMT siblings. The 72-worker comparison covers the
whole machine. The separate interference test keeps the 54-worker affinity
fixed and pins four CPU-bound processes either to reserved physical cores or to
four occupied logical CPUs with unused SMT siblings. Affinity applies to the
test processes. Host services continue using their normal CPU masks. The
benchmark and synthetic competitors inherited nice +5 on this machine. The
summary estimates non-query busy time on the exact worker CPUs from
`/proc/stat` minus summed worker runtime, including CPU time spent in other
tasks or kernel work.

The query comparison uses the original held-out prefix, three reproducible
1,000-vector draws from the same held-out bank, and two orders of all 16,384
vectors. The full pass cycles through the entire shuffled bank. The blocked
order repeats each consecutive 256-query block four times before advancing.
Both 16K orders use the same vectors and the same query frequencies across a
complete schedule. Each worker starts warmup at the same vector in both 16K
orders, with staggered offsets across workers. The adjacent-reuse control
pairs a full pass with four consecutive copies of each individual query at
54 workers. The graph-work CSV records the bank checksums and the mean work
for a complete pass. Timed runs use three 8-second windows per cell, sample one
call in four, and retain the observed minimum and maximum in the summary.
The plotted 2 ms line is an example budget for the closed-loop P99.

## Reproduce the unbound-to-single-socket investigation

The blog starts with an unbound workload and follows the perf evidence toward
NUMA and SMT controls. These runs use the existing 524,288-vector graph and the
original 1,000 test queries, with one shared Index throughout the new sweep.
The unbound entry point calls no affinity or memory-policy setter. Workers
inherit the host's full CPU mask. The file pages were already resident on node
0 from index preparation, and their placement is verified before and after
every window. The local entry point binds workers and new allocations to node
0. The same shared index file remains mapped in both cases.

```bash
bash build.sh --offline
mkdir -p results/story/probes
python3 run.py story results/story/scaling.jsonl \
  --repeat 3 --seconds 12 --warmup 2 --telemetry --imc --latency-stride 4
python3 probes.py results/story/probes --only story-latency
gzip -n results/story/probes/*.txt
python3 summarize.py results --story-only
uv run --no-project --with matplotlib==3.11.2 python plot.py \
  results/summary.csv figures --story results/story/summary.csv
```

The first pass sweeps unbound counts 18, 24, 36, 48, 54 and 72, then pinned
counts 18, 24 and 36 on node 0, then 18 workers on nine node-0 cores using both
SMT siblings. The second pass reverses this order, and the third repeats the
first. All throughput windows run for 12 seconds after two seconds of warmup.
PEBS runs separately in three five-second windows for unbound 36/54 and local
18/36, using the same 30-cycle qualifying-load threshold as the earlier probes.

The runner uses root perf permission for kernel scheduling events, then drops
back to the invoking user before launching the workload. Hardware events retain
their user-space modifiers. The raw core output includes CPU migrations and
context switches. After each sampled search, the worker also records its
current NUMA node. These counts describe sampled completions rather than exact
per-socket execution time. Unbound worker records use `cpu=-1`, and their
`cpus` list is empty. Pinned records retain their actual logical CPU numbers.

The process records `/proc/self/io` storage-read bytes across the timed work.
Major faults are retained in these raw results and the summary even when the
storage-read counter stays at zero. The summary computes IPC and retired
local/remote DRAM loads per completed query, alongside host-wide IMC and UPI
traffic. The ordinary suites keep their existing zero-major-fault validation.
The blog's two Markdown entry points contain the same narrative.
