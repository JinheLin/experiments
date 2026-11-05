#!/usr/bin/env python3
"""Run randomized, serial benchmark cells on the two-socket test host."""
import argparse
import itertools
import json
import os
from pathlib import Path
import random
import subprocess
import tempfile

SIZES = (4096, 65536, 524288)
LOCAL = [*range(18), *range(36, 54)]
REMOTE = [*range(18, 36), *range(54, 72)]
LOCAL_SMT = [cpu for pair in zip(LOCAL[:18], LOCAL[18:]) for cpu in pair]
SPREAD = [cpu for pair in zip(LOCAL, REMOTE) for cpu in pair]
EVENTS = ",".join((
    "cycles:u", "instructions:u", "mem_load_retired.l3_miss:u",
    "mem_load_l3_miss_retired.local_dram:u", "mem_load_l3_miss_retired.remote_dram:u",
))
IMC = "uncore_imc/cas_count_read/,uncore_imc/cas_count_write/"
UPI = "unc_upi_txl_flits.all_data"


def matrix(suite):
    if suite == "primary":
        for size, mode, threads in itertools.product(
            SIZES, ("shared", "private"), (1, 2, 4, 8, 12, 18, 24, 36)
        ):
            yield size, mode, 64, "local", "node0", threads
    elif suite == "topology":
        for placement, pages, counts in (
            ("remote", "node0", (1, 8, 18, 24, 36)),
            ("spread", "node0", (8, 18, 24, 36, 54, 72)),
            ("spread", "interleave", (8, 18, 24, 36, 54, 72)),
        ):
            for threads in counts:
                yield SIZES[-1], "shared", 64, placement, pages, threads
    elif suite == "numa-long":
        for placement in ("local", "remote"):
            yield SIZES[-1], "shared", 64, placement, "node0", 18
    elif suite == "replica-long":
        for threads in (54, 72):
            yield SIZES[-1], "pair", 64, "spread", "replica", threads
    elif suite == "replicas":
        for pages, threads in itertools.product(("interleave", "replica"), (18, 36, 54, 64, 72)):
            yield SIZES[-1], "pair", 64, "spread", pages, threads
    elif suite == "short":
        for mode, (placement, counts) in itertools.product(
            ("shared", "private"), (("local", (1, 8, 18, 36)), ("spread", (18, 36, 72)))
        ):
            for threads in counts:
                yield SIZES[0], mode, 16, placement, "node0", threads
    elif suite == "smt":
        for size, placement in itertools.product((SIZES[0], SIZES[-1]), ("local", "local-smt")):
            yield size, "shared", 64, placement, "node0", 18
    elif suite == "slot-audit":
        for mode, placement, pages, threads in (
            ("shared", "local", "node0", 36),
            ("shared", "spread", "interleave", 54),
            ("shared", "spread", "interleave", 72),
            ("pair", "spread", "replica", 54),
            ("pair", "spread", "replica", 72),
        ):
            yield SIZES[-1], mode, 64, placement, pages, threads
    elif suite == "slots":
        for size, placement, pages, threads in (
            (4096, "local", "node0", 1),
            (4096, "local", "node0", 18),
            (4096, "spread", "node0", 36),
            (4096, "spread", "node0", 72),
            (524288, "local", "node0", 18),
            (524288, "local", "node0", 36),
            (524288, "spread", "interleave", 54),
            (524288, "spread", "interleave", 72),
        ):
            for mode in ("shared", "explicit"):
                yield size, mode, 16 if size == 4096 else 64, placement, pages, threads



def fields(line):
    return dict(item.split("=", 1) for item in line.strip().split(","))


def run_cell(case, rep, args):
    rows, mode, ef, placement, pages, threads = case
    cpus = {"local": LOCAL, "local-smt": LOCAL_SMT, "remote": REMOTE, "spread": SPREAD}[placement][:threads]
    index = args.data / f"laion-{rows}{'-interleave' if pages == 'interleave' else ''}.usearch"
    policy = {
        "local": "--membind=0", "local-smt": "--membind=0",
        "remote": "--membind=1", "spread": "--localalloc"
    }[placement]
    cmd = [
        "numactl", f"--physcpubind={cpus[0]}", policy, str(args.binary.resolve()),
        "bench", str(index), str(args.queries or args.data / "queries.f32"), mode, str(ef),
        ",".join(map(str, cpus)), str(args.seconds), str(args.warmup),
    ]
    if mode == "pair":
        second = args.data / f"laion-{rows}-node1.usearch" if pages == "replica" else index
        cmd.append(str(second))
    with tempfile.TemporaryDirectory(prefix="hnsw-perf-") as temp:
        directory = Path(temp)
        core_ctl, core_ack = directory / "core-ctl", directory / "core-ack"
        os.mkfifo(core_ctl)
        os.mkfifo(core_ack)
        core_stat = directory / "core.csv"
        ctl = f"{core_ctl},{core_ack}"
        events = f"{EVENTS},ref-cycles:u" if args.telemetry else EVENTS
        cmd = [
            "env", f"HNSW_PERF_FIFOS={ctl}", f"HNSW_LATENCY_STRIDE={args.latency_stride}", "perf", "stat", "-x,", "--delay=-1",
            f"--control=fifo:{ctl}", "-o", str(core_stat), "-e", events, "--", *cmd,
        ]
        if args.imc:
            imc_ctl, imc_ack = directory / "imc-ctl", directory / "imc-ack"
            os.mkfifo(imc_ctl)
            os.mkfifo(imc_ack)
            imc_stat = directory / "imc.csv"
            imc_pair = f"{imc_ctl},{imc_ack}"
            cmd[1] += f" {imc_pair}"
            cmd = [
                "sudo", "-n", "perf", "stat", "-a", "-A", "-x,", "--delay=-1",
                f"--control=fifo:{imc_pair}", "-o", str(imc_stat), "-e", f"{IMC},{UPI}" if args.suite in ("replicas", "numa-long", "replica-long") else IMC, "--",
                "sudo", "-n", "-u", os.environ["USER"], "--", *cmd,
            ]
        result = subprocess.run(cmd, check=False, text=True, capture_output=True, timeout=max(120, args.seconds + args.warmup + 60))
        if result.returncode:
            raise RuntimeError(f"{case}\n{result.stdout}\n{result.stderr}")
        header, metrics = result.stdout.strip().splitlines()
        record = fields(header) | fields(metrics)
        total = int(record["mapped_pages_n0"]) + int(record["mapped_pages_n1"])
        node0 = int(record["mapped_pages_n0"]) / total
        if not (node0 >= 0.95 if pages == "node0" else 0.45 <= node0 <= 0.55):
            raise RuntimeError(f"{case} has unexpected index page placement {node0:.1%} on node 0")
        if mode == "pair":
            line = next(line for line in result.stderr.splitlines() if line.startswith("view_pages="))
            per_view = [list(map(int, part.split(","))) for part in line.split("=", 1)[1].split(";")]
            for node, counts in enumerate(per_view):
                fraction = counts[node] / sum(counts)
                if not (fraction >= 0.95 if pages == "replica" else 0.45 <= fraction <= 0.55):
                    raise RuntimeError(f"{case} view {node} has {fraction:.1%} local pages")
            record["per_view_numa_pages"] = per_view
        post = next(line for line in result.stderr.splitlines() if line.startswith("post_view_pages="))
        post_views = [list(map(int, part.split(","))) for part in post.split("=", 1)[1].split(";")]
        for node, counts in enumerate(post_views):
            fraction = counts[node] / sum(counts)
            if not (0.45 <= fraction <= 0.55 if pages == "interleave" else fraction >= 0.95):
                raise RuntimeError(f"{case} view {node} ended with {fraction:.1%} local pages")
        record["post_view_numa_pages"] = post_views
        wait_samples = [list(map(int, line.split("=", 1)[1].split(",")))
                        for line in result.stderr.splitlines() if line.startswith("slot_wait_ns=")]
        if args.suite == "slot-audit" and not wait_samples:
            raise RuntimeError("slot-audit requires a binary built with --features slot-timing")
        if wait_samples:
            record["slot_wait_ns"] = wait_samples
        return {
            "rep": rep, "mode": mode, "ef": ef, "placement": placement,
            "page_policy": pages, "threads": threads, "cpus": cpus, **record,
            "core_perf": core_stat.read_text(), "imc_perf": imc_stat.read_text() if args.imc else "",
            "workers": [fields(line) for line in result.stderr.splitlines() if line.startswith("cpu=")],
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("primary", "topology", "short", "slots", "smt", "replicas", "numa-long", "replica-long", "slot-audit"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--binary", type=Path, default=Path("target/release/hnsw-scaling"))
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=2)
    parser.add_argument("--warmup", type=float, default=0.75)
    parser.add_argument("--imc", action="store_true")
    parser.add_argument("--queries", type=Path)
    parser.add_argument("--latency-stride", type=int, default=67)
    parser.add_argument("--telemetry", action="store_true", help="also collect user-space reference cycles")
    parser.add_argument("--rows", type=int, choices=SIZES)
    args = parser.parse_args()
    cases = [case for case in matrix(args.suite) if args.rows in (None, case[0])]
    with args.output.open("x", buffering=1) as out:
        for rep in range(1, args.repeat + 1):
            if args.suite.endswith("-long"):
                ordered = cases if rep % 2 else reversed(cases)
            else:
                random.Random(85969 + rep).shuffle(cases)
                ordered = cases
            for case in ordered:
                record = run_cell(case, rep, args)
                print(f"{rep} {case} {float(record['qps']):,.0f} QPS", flush=True)
                out.write(json.dumps(record) + "\n")


if __name__ == "__main__":
    main()
