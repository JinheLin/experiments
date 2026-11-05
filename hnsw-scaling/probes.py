#!/usr/bin/env python3
"""Collect per-socket bandwidth references and PEBS load-use latency samples."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

from run import IMC, LOCAL, REMOTE, SPREAD


def run(command, **kwargs):
    return subprocess.run(command, check=True, text=True, **kwargs)


def bandwidth(stream, out):
    base = ["sudo", "-n", "perf", "stat", "-a", "-A", "-x,", "-I", "1000", "-e", IMC]
    cases = (
        ("node0", LOCAL[:18]),
        ("dual", LOCAL[:18] + REMOTE[:18]),
        ("dual-local", LOCAL[:18] + REMOTE[:18]),
    )
    for rep in range(1, 4):
        run([*base, "-o", str(out / f"idle-imc-{rep}.csv"), "--", "sleep", "6"])
        for label, cpus in cases:
            with (out / f"stream-{label}-{rep}.txt").open("x") as log:
                run([
                    *base, "-o", str(out / f"stream-{label}-imc-{rep}.csv"), "--",
                    "sudo", "-n", "-u", os.environ["USER"], "--",
                    "env", f"OMP_NUM_THREADS={len(cpus)}", "OMP_DYNAMIC=false",
                    "GOMP_CPU_AFFINITY=" + " ".join(map(str, cpus)),
                    "numactl", "--physcpubind=" + ",".join(map(str, cpus)), str(stream.resolve()), label,
                ], stdout=log)


def latency(binary, data, out, replica=False, story=False):
    cases = (
        ("local-1", LOCAL[:1], "--membind=0", ""),
        ("local-18", LOCAL[:18], "--membind=0", ""),
        ("local-36", LOCAL[:36], "--membind=0", ""),
        ("remote-18", REMOTE[:18], "--membind=1", ""),
        ("interleave-36", SPREAD[:36], "--localalloc", "-interleave"),
    )
    if replica:
        cases = tuple((f"replica-{workers}", SPREAD[:workers], "--localalloc", "") for workers in (54, 72))
    if story:
        cases = (
            ("unbound-36", list(range(36)), "", ""),
            ("unbound-54", list(range(54)), "", ""),
            ("local-18", LOCAL[:18], "--membind=0", ""),
            ("local-36", LOCAL[:36], "--membind=0", ""),
        )
    for label, cpus, placement, suffix in cases:
        for rep in range(1, 4):
            with tempfile.TemporaryDirectory(prefix="hnsw-pebs-") as temp:
                folder = Path(temp)
                ctl, ack = folder / "control", folder / "ack"
                raw = folder / "perf.data"
                os.mkfifo(ctl)
                os.mkfifo(ack)
                unbound = label.startswith("unbound-")
                workload = [
                    str(binary.resolve()), "bench", str(data / f"laion-524288{suffix}.usearch"),
                    str(data / "queries.f32"), "pair" if replica else "shared", "64",
                    f"unbound:{len(cpus)}" if unbound else ",".join(map(str, cpus)),
                    "5" if story else "2.5", "2" if story else "0.6",
                ]
                if not unbound:
                    workload = ["numactl", f"--physcpubind={cpus[0]}", placement, *workload]
                command = [
                    "env", f"HNSW_PERF_FIFOS={ctl},{ack}",
                    "perf", "mem", "record", "-t", "load", "-U", "--ldlat", "30",
                    "-c", "10007", "--delay=-1", f"--control=fifo:{ctl},{ack}", "-o", str(raw), "--",
                    *workload,
                ]
                if replica:
                    command.append(str(data / "laion-524288-node1.usearch"))
                record = run(command, capture_output=True, timeout=60)
                if replica:
                    line = next(line for line in record.stderr.splitlines() if line.startswith("view_pages="))
                    pairs = [tuple(map(int, part.split(","))) for part in line.split("=", 1)[1].split(";")]
                    assert all(counts[node] / sum(counts) >= 0.95 for node, counts in enumerate(pairs))
                with (out / f"pebs-{label}-{rep}.log").open("x") as log:
                    log.write(record.stdout + record.stderr)
                with (out / f"pebs-{label}-{rep}.txt").open("x") as samples:
                    run(["perf", "script", "-i", str(raw), "-F", "weight,data_src"], stdout=samples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--binary", type=Path, default=Path("target/release/hnsw-scaling"))
    parser.add_argument("--stream", type=Path, default=Path("target/stream-read"))
    parser.add_argument("--only", choices=("bandwidth", "latency", "replica-latency", "story-latency"))
    args = parser.parse_args()
    if args.only in (None, "bandwidth"):
        bandwidth(args.stream, args.output)
    if args.only in (None, "latency", "replica-latency", "story-latency"):
        latency(args.binary, args.data, args.output, replica=args.only == "replica-latency",
                story=args.only == "story-latency")


if __name__ == "__main__":
    main()
