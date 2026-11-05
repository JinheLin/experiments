#!/usr/bin/env python3
"""Apply a disjoint-CPU streaming reader to either memory node during a search."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import subprocess

from run import REMOTE, matrix, run_cell


@contextmanager
def reader(placement, args):
    if placement == "idle":
        yield {}
        return

    cpus = REMOTE[:8]
    node = "node0" if placement == "shared-controller" else "node1"
    env = os.environ | {
        "OMP_NUM_THREADS": str(len(cpus)),
        "OMP_DYNAMIC": "false",
        "GOMP_CPU_AFFINITY": " ".join(map(str, cpus)),
    }
    command = ["taskset", "-c", ",".join(map(str, cpus)), str(args.stream.resolve()),
               node, str(args.seconds + args.warmup + 30)]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True, env=env) as load:
        try:
            if not select.select([load.stdout], [], [], 30)[0]:
                raise RuntimeError("streaming reader did not finish first touch")
            ready = load.stdout.readline().strip()
            if not ready.startswith("pages_n0="):
                raise RuntimeError(f"unexpected streaming reader output {ready!r}")
            yield {"reader_pages": ready, "reader_cpus": cpus}
            if load.poll() is not None:
                raise RuntimeError("streaming reader stopped before the query window ended")
        finally:
            if load.poll() is None:
                load.terminate()
            try:
                load.wait(timeout=5)
            except subprocess.TimeoutExpired:
                load.kill()
                load.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--binary", type=Path, default=Path("target/release/hnsw-scaling"))
    parser.add_argument("--stream", type=Path, default=Path("target/stream-read"))
    parser.add_argument("--seconds", type=float, default=8)
    parser.add_argument("--warmup", type=float, default=2)
    parser.set_defaults(suite="numa-long", imc=True, telemetry=True, latency_stride=4, queries=None)
    args = parser.parse_args()
    placements = ["idle", "separate-controller", "shared-controller"]
    case = next(matrix("numa-long"))
    with args.output.open("x", buffering=1) as out:
        for rep in range(1, 4):
            for placement in placements:
                with reader(placement, args) as pressure:
                    result = run_cell(case, rep, args)
                out.write(json.dumps(result | pressure | {"pressure": placement}) + "\n")
                print(rep, placement, result["qps"], flush=True)
            placements = placements[1:] + placements[:1]


if __name__ == "__main__":
    main()
