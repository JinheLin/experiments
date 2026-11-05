#!/usr/bin/env python3
"""Reduce raw runs to per-cell medians and per-probe summaries."""
import argparse
from collections import defaultdict
import csv
import gzip
import json
from pathlib import Path
from statistics import median

from run import LOCAL, REMOTE

KEYS = ("rows", "mode", "ef", "placement", "page_policy", "threads")
SUITES = ("primary", "topology", "short", "slots", "smt", "replicas")
BASIC = ("qps", "cpu_cores", "cpu_us_per_query", "host_idle_pct", "p50_us", "p95_us", "p99_us")
EVENTS = {
    "cycles": "cycles:u",
    "instructions": "instructions:u",
    "l3_misses": "mem_load_retired.l3_miss:u",
    "local_dram_loads": "mem_load_l3_miss_retired.local_dram:u",
    "remote_dram_loads": "mem_load_l3_miss_retired.remote_dram:u",
}


def derived(record):
    stats = {}
    for line in record["core_perf"].splitlines():
        if line and not line.startswith("#"):
            value, _, event, _, running, *_ = line.split(",")
            stats[event] = float(value), float(running)
    seconds = float(record["seconds"])
    completed = int(record["completed"])
    assert int(record["errors"]) == int(record["short"]) == int(record["major_faults"]) == 0
    assert sum(int(worker["completed"]) for worker in record["workers"]) == completed
    assert len(record["workers"]) == record["threads"]
    row = {name: float(record[name]) for name in BASIC}
    row["completed"] = completed
    row["samples"] = int(record["samples"])
    row["minor_faults"] = int(record["minor_faults"])
    row["node0_pages_pct"] = 100 * int(record["mapped_pages_n0"]) / sum(
        int(record[f"mapped_pages_n{node}"]) for node in range(2)
    )
    for name, event in EVENTS.items():
        row[f"{name}_per_query"] = stats[event][0] / completed
    row["ipc"] = stats[EVENTS["instructions"]][0] / stats[EVENTS["cycles"]][0]
    row["core_pmu_running_pct"] = min(stats[event][1] for event in EVENTS.values())
    row["cpu_busy_worker_pct_min"] = min(float(w["busy_pct"]) for w in record["workers"])
    row["cpu_busy_worker_pct_max"] = max(float(w["busy_pct"]) for w in record["workers"])
    for socket in range(2):
        for operation in ("read", "write"):
            traffic = [
                float(line.split(",")[1]) for line in record["imc_perf"].splitlines()
                if line.startswith(f"CPU{socket * 18},") and f"cas_count_{operation}/" in line
            ]
            if traffic:
                assert len(traffic) == 6
                row[f"socket{socket}_{operation}_gib_s"] = sum(traffic) / seconds / 1024
        flits = [
            int(line.split(",")[1]) for line in record["imc_perf"].splitlines()
            if line.startswith(f"CPU{socket * 18},") and "unc_upi_txl_flits.all_data" in line
        ]
        if flits:
            assert len(flits) == 3
            row[f"socket{socket}_upi_tx_gib_s"] = sum(flits) * 8 / seconds / 1024**3
    if "socket0_upi_tx_gib_s" in row:
        row["upi_tx_gib_s"] = row["socket0_upi_tx_gib_s"] + row["socket1_upi_tx_gib_s"]
    if "socket0_read_gib_s" in row:
        row["imc_read_gib_s"] = row["socket0_read_gib_s"] + row["socket1_read_gib_s"]
    return row


def write_csv(path, rows, lineterminator="\r\n"):
    if rows:
        columns = list(dict.fromkeys(key for row in rows for key in row))
        with path.open("w", newline="") as file:
            writer = csv.DictWriter(file, columns, lineterminator=lineterminator)
            writer.writeheader()
            writer.writerows(rows)


def benchmark(results):
    rows = []
    for suite in SUITES:
        path = results / f"{suite}.jsonl"
        if not path.exists():
            continue
        cells = defaultdict(list)
        for line in path.read_text().splitlines():
            record = json.loads(line)
            cell = tuple(record[key] for key in KEYS)
            cells[cell].append(derived(record))
        for cell, repeats in sorted(cells.items()):
            aggregates = {
                key: median(repeat[key] for repeat in repeats)
                for key in repeats[0]
            }
            rows.append({
                "suite": suite, **dict(zip(KEYS, cell)), "reps": len(repeats), **aggregates,
                "qps_min": min(repeat["qps"] for repeat in repeats),
                "qps_max": max(repeat["qps"] for repeat in repeats),
                "core_pmu_running_pct_min": min(repeat["core_pmu_running_pct"] for repeat in repeats),
            })
    write_csv(results / "summary.csv", rows)


def bandwidth(results):
    rows = []
    for path in sorted((results / "probes").glob("*-imc-*.csv")):
        label, rep = path.stem.rsplit("-imc-", 1)
        intervals = defaultdict(list)
        for line in path.read_text().splitlines():
            if line and not line.startswith("#"):
                time, cpu, value, unit, event, running_ns, *_ = line.split(",")
                if 2.0 <= float(time) <= 6.05 and int(running_ns) >= 900_000_000:
                    assert unit == "MiB"
                    operation = "read" if "cas_count_read" in event else "write"
                    intervals[time, cpu, operation].append(float(value) * 1e9 / int(running_ns))
        for cpu in ("CPU0", "CPU18"):
            row = {"label": label, "rep": rep, "socket": int(cpu == "CPU18")}
            for operation in ("read", "write"):
                selected = [values for (_, group, op), values in intervals.items() if (group, op) == (cpu, operation)]
                assert selected and all(len(values) == 6 for values in selected)
                row[f"{operation}_gib_s"] = median(sum(values) / 1024 for values in selected)
            if label.startswith("stream-"):
                output = path.with_name(f"{label}-{rep}.txt").read_text()
                row["program_read_gib_s"] = float(output.split("read_GiB_per_sec=")[1].split(",")[0])
            rows.append(row)
    write_csv(results / "bandwidth-reference.csv", rows)


def latency(results):
    rows = []
    for path in sorted((results / "probes").glob("pebs-*.txt*")):
        label, rep = path.name.split(".")[0].removeprefix("pebs-").rsplit("-", 1)
        buckets = defaultdict(list)
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt") as file:
            for line in file:
                if "|LVL " in line:
                    source = line.split("|LVL ", 1)[1].split("|", 1)[0].strip()
                    cycles = int(line.split()[-1])
                    buckets[source].append(cycles)
        assert buckets, f"no PEBS samples in {path}"
        for source, samples in sorted(buckets.items()):
            rows.append({
                "label": label, "rep": rep, "source": source, "samples": len(samples),
                "p50_cycles": median(samples), "p95_cycles": sorted(samples)[(len(samples) * 95 + 99) // 100 - 1],
            })
    write_csv(results / "pebs-summary.csv", rows)



def review_metrics(record):
    row = derived(record)
    seconds = float(record["seconds"])
    row["involuntary_per_second"] = int(record["involuntary_switches"]) / seconds
    row["voluntary_per_second"] = int(record["voluntary_switches"]) / seconds
    for node in range(2):
        workers = sum(
            float(worker["busy_pct"]) / 100 for worker in record["workers"]
            if int(worker["cpu"]) in (LOCAL, REMOTE)[node]
        )
        row[f"other_cpu_cores_n{node}"] = float(record[f"host_busy_n{node}"]) - workers
    for line in record["core_perf"].splitlines():
        if ",,ref-cycles:u," in line:
            count, _, _, _, running, *_ = line.split(",")
            cycles = row["cycles_per_query"] * int(record["completed"])
            row["estimated_worker_ghz"] = 2.6 * cycles / float(count)
            row["core_pmu_running_pct"] = min(row["core_pmu_running_pct"], float(running))
    return row


def review(results):
    directory = results / "review"
    rows = []
    pairs = defaultdict(dict)
    for path in sorted(directory.glob("*.jsonl")):
        cells = defaultdict(list)
        for line in path.read_text().splitlines():
            record = json.loads(line)
            cell = tuple(record[key] for key in KEYS)
            rows_data = review_metrics(record)
            cells[cell + (record.get("pressure", ""), int(record["query_count"]))].append((rows_data, record))
            if path.stem == "numa-long":
                pairs[record["rep"]][record["placement"]] = rows_data
        for cell, repeats in sorted(cells.items()):
            metrics = [row for row, _ in repeats]
            qps = [row["qps"] for row in metrics]
            p99 = [row["p99_us"] for row in metrics]
            row = {
                "experiment": path.stem, **dict(zip((*KEYS, "pressure", "query_count"), cell)),
                "reps": len(repeats), **{key: median(metric[key] for metric in metrics) for key in metrics[0]},
                "qps_min": min(qps), "qps_max": max(qps), "p99_us_min": min(p99), "p99_us_max": max(p99),
            }
            if "slot_wait_ns" in repeats[0][1]:
                samples = [sample for _, record in repeats for sample in record["slot_wait_ns"]]
                waits = sorted(take + put for take, put, _ in samples)
                assert len(samples) == sum(row["samples"] for row, _ in repeats)
                row.update(
                    slot_samples=len(samples),
                    take_wait_ns_p50=median(take for take, _, _ in samples),
                    put_wait_ns_p50=median(put for _, put, _ in samples),
                    slot_total_wait_ns_p99=waits[(len(waits) * 99 + 99) // 100 - 1],
                    slot_wait_pct_sampled_wall=100 * sum(waits) / sum(ns for _, _, ns in samples),
                )
            rows.append(row)
    write_csv(directory / "summary.csv", rows, lineterminator="\n")
    comparisons = []
    for rep, pair in sorted(pairs.items()):
        if len(pair) < 2:
            continue
        local, remote = pair["local"], pair["remote"]
        comparisons.append({
            "rep": rep, "order": "local-remote" if rep % 2 else "remote-local",
            "local_qps": local["qps"], "remote_qps": remote["qps"],
            "remote_qps_change_pct": 100 * (remote["qps"] / local["qps"] - 1),
            "local_p99_us": local["p99_us"], "remote_p99_us": remote["p99_us"],
            "remote_p99_change_pct": 100 * (remote["p99_us"] / local["p99_us"] - 1),
        })
    write_csv(directory / "numa-pairs.csv", comparisons, lineterminator="\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--review-only", action="store_true")
    args = parser.parse_args()
    if args.review_only:
        review(args.results)
        return
    benchmark(args.results)
    bandwidth(args.results)
    latency(args.results)
    review(args.results)


if __name__ == "__main__":
    main()
