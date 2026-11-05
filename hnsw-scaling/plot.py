#!/usr/bin/env python3
"""Plot the measured scaling curves, normalized work, and placement controls."""
import argparse
import csv
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BLUE = "#2166ac"
ORANGE = "#b34d13"
GREEN = "#19775e"
GRAY = "#646b75"


def series(records, **where):
    return sorted(
        [row for row in records if all(row[key] == str(value) for key, value in where.items())],
        key=lambda row: int(row["threads"]),
    )


def axis(ax, title, ylabel, ticks):
    ax.set(title=title, xlabel="Pinned workers", ylabel=ylabel, xticks=ticks)
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", linewidth=0.65, alpha=0.35)
    ax.set_axisbelow(True)


def values(data, metric, div=1):
    return [float(row[metric]) / div for row in data]


def curve(ax, data, metric, label, color, div=1, errors=False):
    if not data:
        return
    x = values(data, "threads")
    y = values(data, metric, div)
    if errors:
        low = values(data, "qps_min", div)
        high = values(data, "qps_max", div)
        ax.errorbar(x, y, yerr=[[m - l for m, l in zip(y, low)], [h - m for m, h in zip(y, high)]],
                    label=label, color=color, marker="o", markersize=4, capsize=2)
    else:
        ax.plot(x, y, label=label, color=color, marker="o", markersize=4)


def scaling(rows, out):
    fig, axes = plt.subplots(1, 3, figsize=(13.1, 3.55), layout="constrained")
    for ax, size, title in zip(axes, (4096, 65536, 524288), ("4K · 12.6 MiB", "64K · 201 MiB", "512K · 1.57 GiB")):
        for mode, label, color in (("shared", "Shared Index", BLUE), ("private", "Index per worker", ORANGE)):
            data = series(rows, suite="primary", rows=size, mode=mode)
            curve(ax, data, "qps", label, color, 1000, errors=True)
        axis(ax, title, "Throughput (kQPS)", (1, 4, 8, 12, 18, 24, 36))
        ax.axvspan(18, 37.2, color="#c8d1d9", alpha=0.25)
        ax.set_ylim(bottom=0)
    axes[0].legend(frameon=False)
    fig.savefig(out / "scaling.svg")
    plt.close(fig)


def topology(rows, out):
    fig, axes = plt.subplots(1, 3, figsize=(13.1, 3.55), layout="constrained")
    variants = (
        ("primary", "local", "node0", "Local · one socket", BLUE),
        ("topology", "remote", "node0", "Remote · one socket", ORANGE),
        ("topology", "spread", "node0", "Two sockets · pages on node 0", GRAY),
        ("topology", "spread", "interleave", "Two sockets · interleaved pages", GREEN),
    )
    for suite, placement, pages, label, color in variants:
        data = series(rows, suite=suite, rows=524288, mode="shared", placement=placement, page_policy=pages)
        curve(axes[0], data, "qps", label, color, 1000, errors=True)
        curve(axes[1], data, "cpu_cores", label, color)
        curve(axes[2], data, "p99_us", label, color, 1000)
    for ax, title, ylabel in zip(axes,
        ("Throughput", "Worker CPU time", "Tail latency"),
        ("Throughput (kQPS)", "Logical CPUs of worker time", "Sampled P99 (ms)"),
    ):
        axis(ax, title, ylabel, (1, 8, 18, 24, 36, 54, 72))
        ax.set_ylim(bottom=0)
    axes[0].legend(frameon=False, fontsize=8)
    fig.savefig(out / "topology.svg")
    plt.close(fig)


def costs(rows, out):
    fig, axes = plt.subplots(1, 3, figsize=(13.1, 3.55), layout="constrained")
    for size, label, color in ((4096, "4K", BLUE), (65536, "64K", ORANGE), (524288, "512K", GREEN)):
        data = series(rows, suite="primary", rows=size, mode="shared")
        curve(axes[0], data, "cycles_per_query", label, color, 1e6)
        curve(axes[0], data, "instructions_per_query", "_" + label, color, 1e6)
        axes[0].lines[-1].set_linestyle("--")
        curve(axes[1], data, "ipc", label, color)
        curve(axes[2], data, "l3_misses_per_query", label, color, 1000)
    for ax, title, ylabel in zip(axes,
        ("Work per query", "Instructions per cycle", "Retired load misses"),
        ("Millions per query", "IPC", "L3 misses per query (thousands)"),
    ):
        axis(ax, title, ylabel, (1, 4, 8, 12, 18, 24, 36))
        ax.axvspan(18, 37.2, color="#c8d1d9", alpha=0.25)
        ax.set_ylim(bottom=0)
    axes[0].text(0.96, 0.03, "Cycles (solid) · Instructions (dashed)", transform=axes[0].transAxes,
                 ha="right", va="bottom", fontsize=8, color=GRAY)
    axes[1].legend(frameon=False)
    fig.savefig(out / "costs.svg")
    plt.close(fig)



def slots(rows, out):
    fig, axes = plt.subplots(1, 2, figsize=(9.25, 3.55), layout="constrained")
    for mode, label, color in (("shared", "Shared Index", BLUE), ("private", "Index per worker", ORANGE)):
        data = series(rows, suite="short", rows=4096, mode=mode, placement="spread")
        curve(axes[0], data, "qps", label, color, 1000, errors=True)
    axis(axes[0], "Stock Rust API · 4K / ef=16", "Throughput (kQPS)", (18, 36, 72))
    axes[0].set_ylim(bottom=0)
    axes[0].legend(frameon=False)

    configs = (
        (4096, "local", 1, "4K local · 1"),
        (4096, "local", 18, "4K local · 18"),
        (4096, "spread", 36, "4K split · 36"),
        (4096, "spread", 72, "4K split · 72"),
        (524288, "local", 18, "512K local · 18"),
        (524288, "local", 36, "512K local · 36"),
        (524288, "spread", 54, "512K split · 54"),
        (524288, "spread", 72, "512K split · 72"),
    )
    for idx, (size, placement, workers, label) in enumerate(configs):
        data = series(rows, suite="slots", rows=size, placement=placement, threads=workers)
        qps = {row["mode"]: float(row["qps"]) for row in data}
        if len(qps) == 2:
            axes[1].barh(idx, qps["explicit"] / qps["shared"], height=0.68,
                         color=GREEN if size == 4096 else GRAY)
    axes[1].axvline(1, color=BLUE, linestyle="--", linewidth=1)
    axes[1].set(title="One shared view · fixed worker slots", xlabel="Fixed-slot QPS / pooled QPS",
                yticks=range(len(configs)), yticklabels=[label for _, _, _, label in configs])
    axes[1].invert_yaxis()
    axes[1].spines[["top", "right"]].set_visible(False)
    fig.savefig(out / "slots.svg")
    plt.close(fig)


def replicas(rows, out):
    fig, axes = plt.subplots(1, 3, figsize=(12.2, 3.6), layout="constrained")
    for pages, label, color in (
        ("interleave", "Shared interleaved file", BLUE),
        ("replica", "Local file per socket", GREEN),
    ):
        data = series(rows, suite="replicas", page_policy=pages)
        curve(axes[0], data, "qps", label, color, 1000, errors=True)
        curve(axes[1], data, "p99_us", label, color, 1000)
        curve(axes[2], data, "upi_tx_gib_s", label, color)
    for ax, title, unit in zip(axes, ("Throughput", "Tail latency", "Cross-socket traffic"),
                              ("Throughput (kQPS)", "Sampled P99 (ms)", "UPI TX payload (GiB/s)")):
        axis(ax, title, unit, (18, 36, 54, 64, 72))
        ax.set_ylim(bottom=0)
    axes[0].legend(frameon=False, fontsize=8)
    fig.savefig(out / "replicas.svg")
    plt.close(fig)


def smt_large(rows, out):
    scale = series(rows, suite="primary", rows=524288, mode="shared", placement="local")
    control = series(rows, suite="smt", rows=524288, mode="shared")
    if not scale or len(control) != 2:
        return
    fig, (left, right) = plt.subplots(1, 2, figsize=(9.5, 3.65), layout="constrained")
    one_worker = next(float(row["qps"]) / 1000 for row in scale if row["threads"] == "1")
    left.plot((1, 18), (one_worker, 18 * one_worker), "--", color=GRAY, label="Ideal on physical cores")
    curve(left, scale, "qps", "Measured", BLUE, 1000, errors=True)
    left.axvspan(18, 37.2, color="#c8d1d9", alpha=0.28)
    axis(left, "Single NUMA node · local pages", "Throughput (kQPS)", (1, 4, 8, 12, 18, 24, 36))
    left.set_ylim(bottom=0)
    left.legend(frameon=False, fontsize=8)

    layout = {row["placement"]: row for row in control}
    data = [layout["local"], layout["local-smt"]]
    y = values(data, "qps", 1000)
    low = values(data, "qps_min", 1000)
    high = values(data, "qps_max", 1000)
    bars = right.bar((0, 1), y, color=(BLUE, ORANGE), width=0.55, capsize=4,
                     yerr=[[m - l for m, l in zip(y, low)], [h - m for m, h in zip(y, high)]])
    right.bar_label(bars, labels=[f"{qps:.1f}" for qps in y], padding=6)
    axis(right, "Fixed at 18 workers · local pages", "Throughput (kQPS)", (0, 1))
    right.set(xticklabels=("18 cores × 1 thread", "9 cores × 2 threads"), xlabel="", ylim=(0, max(high) * 1.17))
    fig.savefig(out / "smt-large.svg")
    plt.close(fig)



def numa_pairs(path, out):
    with path.open() as file:
        rows = list(csv.DictReader(file))
    rounds = values(rows, "rep")
    fig, axes = plt.subplots(1, 2, figsize=(8.6, 3.25), layout="constrained")
    for ax, metric, unit in zip(axes, ("qps", "p99_us"), ("Throughput (kQPS)", "P99 (ms)")):
        for placement, color, label in (("local", BLUE, "Local DRAM"), ("remote", ORANGE, "Remote DRAM")):
            ax.plot(rounds, values(rows, f"{placement}_{metric}", 1000), color=color, marker="o",
                    markersize=4, label=label)
        axis(ax, unit, unit, rounds)
        ax.set(xlabel="Paired round", ylabel=None, ylim=(0, None))
    axes[0].legend(frameon=False, fontsize=8)
    fig.savefig(out / "numa-long.svg")
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--numa-pairs", type=Path, help="render only the longer paired NUMA comparison")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"svg.fonttype": "none", "font.size": 9, "axes.titleweight": "bold"})
    if args.numa_pairs:
        numa_pairs(args.numa_pairs, args.output)
        return
    with args.summary.open() as file:
        rows = list(csv.DictReader(file))
    scaling(rows, args.output)
    smt_large(rows, args.output)
    costs(rows, args.output)
    if any(row["suite"] == "topology" for row in rows):
        topology(rows, args.output)
    if any(row["suite"] == "slots" for row in rows):
        slots(rows, args.output)
    if any(row["suite"] == "replicas" and row["socket0_upi_tx_gib_s"] for row in rows):
        replicas(rows, args.output)


if __name__ == "__main__":
    main()
