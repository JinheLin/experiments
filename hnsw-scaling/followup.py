#!/usr/bin/env python3
"""Repeat the large-index search with CPU headroom or changed query reuse."""
import argparse
from contextlib import contextmanager
import csv
import hashlib
import json
from pathlib import Path
import random
import subprocess
import sys

from run import SPREAD, fields, run_cell

RESERVED = {0, 1, 2, 18, 19, 20, 36, 37, 38, 54, 55, 56}
HEADROOM = [cpu for cpu in SPREAD if cpu not in RESERVED]
OUTSIDE = (1, 2, 19, 20)
# At 54 workers these cores have a single query thread and an unused SMT sibling.
ON_WORKERS = (15, 16, 33, 34)
QUERY_BYTES = 768 * 4


@contextmanager
def competitors(cpus):
    processes = []
    try:
        for cpu in cpus:
            processes.append(subprocess.Popen(
                ["taskset", "-c", str(cpu), sys.executable, "-c", "while True: pass"]
            ))
        yield
        if any(process.poll() is not None for process in processes):
            raise RuntimeError("CPU competitor exited during the query window")
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            process.wait()


def query_banks(data, diagnostic, work_csv):
    banks = {"original-1k": data / "queries.f32", "prefix-1k": data / "heldout-1000.f32"}
    full = data / "heldout-16384.f32"
    vectors = full.read_bytes()
    assert len(vectors) == 16_384 * QUERY_BYTES
    assert banks["prefix-1k"].read_bytes() == vectors[:1000 * QUERY_BYTES]
    for seed in (31, 47, 73):
        indices = random.Random(seed).sample(range(16_384), 1000)
        label = f"random-1k-{seed}"
        bank = data / f"heldout-{label}.f32"
        bank.write_bytes(b"".join(vectors[i * QUERY_BYTES:(i + 1) * QUERY_BYTES] for i in indices))
        banks[label] = bank
    banks["full-16k"] = full

    work = []
    for label, bank in banks.items():
        command = ["numactl", "--physcpubind=3", "--membind=0", str(diagnostic.resolve()),
                   "work", str(data / "laion-524288.usearch"), str(bank), "64"]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        work.append({"bank": label, "sha256": hashlib.sha256(bank.read_bytes()).hexdigest(),
                     **fields(result.stdout)})
    with work_csv.open("x", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(work[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(work)
    return banks


def measure(args, output, rep, label, cpus, *, competitors_on=(), query_block=None):
    case = (524288, "pair", 64, "spread", "replica", len(cpus))
    with competitors(competitors_on):
        row = run_cell(case, rep, args, cpus=cpus, query_block=query_block)
    output.write(json.dumps(row | {
        "variant": label, "competitor_cpus": competitors_on, "query_block": query_block,
    }) + "\n")
    print(rep, label, len(cpus), row["qps"], flush=True)


def cpu_runs(args, output, rep):
    counts = [36, 48, 54, 60, 72]
    random.Random(28719 + rep).shuffle(counts)
    for threads in counts:
        cpus = SPREAD if threads == 72 else HEADROOM[:threads]
        measure(args, output, rep, "full-host" if threads == 72 else "headroom", cpus)
    pair = [("competitors-outside", OUTSIDE), ("competitors-on-workers", ON_WORKERS)]
    for label, pins in pair if rep % 2 else reversed(pair):
        measure(args, output, rep, label, HEADROOM[:54], competitors_on=pins)


def query_runs(args, output, rep, banks):
    cases = [
        (label, path, None) for label, path in banks.items()
        if label not in ("original-1k", "full-16k")
    ]
    cases.extend([
        ("full-16k", banks["full-16k"], 16_384),
        ("blocked-16k", banks["full-16k"], 256),
    ])
    random.Random(26491 + rep).shuffle(cases)
    for label, bank, block in cases:
        args.queries = bank
        for threads in (54, 72) if rep % 2 else (72, 54):
            measure(args, output, rep, label, SPREAD[:threads], query_block=block)


def adjacent_runs(args, output, rep):
    args.queries = args.data / "heldout-16384.f32"
    pair = [("full-16k", 16_384), ("adjacent-16k", 1)]
    for label, block in pair if rep % 2 else reversed(pair):
        measure(args, output, rep, label, SPREAD[:54], query_block=block)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", choices=("cpu", "query", "adjacent"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--binary", type=Path, default=Path("target/release/hnsw-scaling"))
    parser.add_argument("--diagnostic", type=Path, default=Path("target/release/hnsw-scaling-diagnostic"))
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=8)
    parser.add_argument("--warmup", type=float, default=2)
    parser.set_defaults(suite="replica-long", imc=True, telemetry=True, latency_stride=4, queries=None)
    args = parser.parse_args()
    with args.output.open("x", buffering=1) as output:
        banks = None
        if args.experiment == "query":
            banks = query_banks(args.data, args.diagnostic, args.output.with_name("query-work.csv"))
        for rep in range(1, args.repeat + 1):
            if args.experiment == "cpu":
                cpu_runs(args, output, rep)
            elif args.experiment == "adjacent":
                adjacent_runs(args, output, rep)
            else:
                query_runs(args, output, rep, banks)


if __name__ == "__main__":
    main()
