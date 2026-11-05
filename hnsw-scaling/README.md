# USearch HNSW read-only scaling

[实验文章（中文）](ARTICLE.md) · [逐轮结果](results/) · [每组中位数](results/summary.csv)

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

Each pinned worker repeatedly calls the Rust API synchronously with one request in flight, using a fixed shuffled query order with a worker-specific starting offset. The warmup and index setup are outside the measured window. Search expansion is 64 except for the short-query suite, which uses 16. Every successful call must return ten matches. The harness samples every 67th call for wall-clock latency and measures per-worker CPU time with `getrusage(RUSAGE_THREAD)`. It records whole-host idle and I/O-wait ticks from `/proc/stat`. The workload uses a closed loop, so these latency quantiles describe calls issued by this benchmark and exclude client-side queuing at a separately imposed arrival rate.

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
