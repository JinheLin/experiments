#!/usr/bin/env python3
"""Export LAION vectors and compute exact top-10 for each corpus prefix."""
import argparse
import os
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

DIM = 768
SIZES = (4_096, 65_536, 524_288)


def export(source, target, rows):
    partial = target.with_suffix(".partial")
    with partial.open("wb") as out:
        remaining = rows
        for batch in pq.ParquetFile(source).iter_batches(batch_size=8192, columns=["emb"]):
            array = batch.column(0).slice(0, remaining)
            assert not array.null_count and np.all(np.diff(array.offsets) == DIM)
            vectors = np.asarray(array.flatten().to_numpy(), dtype="<f4")
            assert np.isfinite(vectors).all()
            vectors.tofile(out)
            remaining -= len(array)
            if remaining == 0:
                break
        assert remaining == 0
    os.replace(partial, target)


def exact(train, queries, rows, target):
    # Squared L2 = ||x||² + ||q||² − 2 x·q. The query norm cancels in rankings.
    # Keep at most 8,192 × 1,000 distances in memory.
    best = np.full((len(queries), 10), np.inf)
    ids = np.full(best.shape, -1, dtype=np.int64)
    for start in range(0, rows, 8192):
        block = train[start:min(start + 8192, rows)]
        distances = np.einsum("ij,ij->i", block, block)[None, :] - 2 * (queries @ block.T)
        idx = np.argpartition(distances, 9, axis=1)[:, :10]
        candidates = np.concatenate((best, np.take_along_axis(distances, idx, axis=1)), axis=1)
        candidate_ids = np.concatenate((ids, start + idx), axis=1)
        keep = np.argpartition(candidates, 9, axis=1)[:, :10]
        best = np.take_along_axis(candidates, keep, axis=1)
        ids = np.take_along_axis(candidate_ids, keep, axis=1)
    np.savetxt(target, ids, fmt="%d", delimiter=",")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--ground-truth", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    train = args.output / "train.f32"
    queries = args.output / "queries.f32"
    if args.ground_truth:
        corpus = np.memmap(train, mode="r", dtype="<f4", shape=(SIZES[-1], DIM))
        test = np.fromfile(queries, dtype="<f4").reshape(-1, DIM)
        for rows in SIZES:
            exact(corpus, test, rows, args.output / f"truth-{rows}.csv")
    else:
        export(args.dataset / "train-00-of-100.parquet", train, SIZES[-1])
        export(args.dataset / "test.parquet", queries, 1000)


if __name__ == "__main__":
    main()
