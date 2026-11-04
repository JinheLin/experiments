#!/usr/bin/env python3
"""Observe TiDB commit protocols on a dedicated local playground cluster."""

import argparse
import csv
import hashlib
import json
import math
import os
import platform
import random
import re
import sys
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pymysql


HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
METRIC = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)")
LABEL = re.compile(r'(\w+)="((?:\\.|[^"\\])*)"')
FAMILIES = (
    "tidb_tikvclient_request_seconds_count",
    "tidb_tikvclient_commit_txn_counter",
    "tidb_tikvclient_async_commit_txn_counter",
    "tidb_tikvclient_one_pc_txn_counter",
    "tikv_raftstore_proposal_total",
    "tikv_scheduler_command_duration_seconds_count",
)
PROTOCOLS = {"2pc": (0, 0), "async_commit": (0, 1), "1pc": (1, 0)}


def http_json(url):
    with HTTP.open(url, timeout=10) as response:
        return json.load(response)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def query(connection, sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def scalar(connection, sql):
    return next(iter(query(connection, sql)[0].values()))


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def fetch_metrics(node):
    with HTTP.open("http://" + node["STATUS_ADDRESS"] + "/metrics", timeout=10) as response:
        lines = response.read().decode().splitlines()
    selected = [line for line in lines if line.startswith(FAMILIES)]
    return node["TYPE"] + "@" + node["STATUS_ADDRESS"], "\n".join(selected) + "\n"


def snapshot(nodes):
    selected = [node for node in nodes if node["TYPE"] in ("tidb", "tikv")]
    with ThreadPoolExecutor(max_workers=len(selected)) as pool:
        return dict(pool.map(fetch_metrics, selected))


def total(snapshot_data, name, **labels):
    value = 0.0
    for content in snapshot_data.values():
        for line in content.splitlines():
            match = METRIC.match(line)
            if match and match[1] == name:
                actual = dict(LABEL.findall(match[2] or ""))
                if all(actual.get(key) == expected for key, expected in labels.items()):
                    value += float(match[3])
    return value


def requests(snapshot_data, kind):
    # Client-Go's scope=false identifies user requests in the pinned version.
    return total(snapshot_data, "tidb_tikvclient_request_seconds_count", type=kind, scope="false")


def delta(before, after):
    result = {
        "user_prewrite_rpc": requests(after, "Prewrite") - requests(before, "Prewrite"),
        "user_commit_rpc": requests(after, "Commit") - requests(before, "Commit"),
        "normal_raft_proposals_all_regions": (
            total(after, "tikv_raftstore_proposal_total", type="normal")
            - total(before, "tikv_raftstore_proposal_total", type="normal")
        ),
    }
    for name in ("commit_txn_counter", "async_commit_txn_counter", "one_pc_txn_counter"):
        metric = "tidb_tikvclient_" + name
        result[name + "_ok_all_sessions"] = total(after, metric, type="ok") - total(before, metric, type="ok")
    if any(value < 0 for value in result.values()):
        raise RuntimeError("A metric counter reset during measurement")
    return result


def save_snapshot(directory, name, data):
    for node, content in data.items():
        safe_name = node.replace("@", "-").replace(":", "-")
        (directory / f"{name}-{safe_name}.prom").write_text(content)


def settle(nodes, timeout=10):
    """Wait for a quiet interval; this is not a proof that all locks are resolved."""
    previous = None
    stable = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = snapshot(nodes)
        signature = (requests(current, "Prewrite"), requests(current, "Commit"))
        stable = stable + 1 if signature == previous else 0
        if stable >= 3:
            return current
        previous = signature
        time.sleep(0.1)
    raise RuntimeError("User write RPC counters did not become quiet")


def regions(connection, table):
    return query(connection, f"SHOW TABLE `{table}` REGIONS")


def region_ids(rows):
    return sorted(int(row["REGION_ID"]) for row in rows)


def verify_replicas(pd_url, rows):
    states = []
    for region_id in region_ids(rows):
        deadline = time.monotonic() + 40
        while True:
            state = http_json(f"{pd_url}/pd/api/v1/region/id/{region_id}")
            if len(state.get("peers", [])) == 3 and not state.get("pending_peers") and not state.get("down_peers"):
                states.append(state)
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(f"Region {region_id} does not have three healthy peers: {state}")
            time.sleep(0.25)
    return states


def configure(connection, protocol):
    one_pc, asynchronous = PROTOCOLS[protocol]
    query(connection, f"SET SESSION tidb_enable_1pc = {one_pc}")
    query(connection, f"SET SESSION tidb_enable_async_commit = {asynchronous}")


def transaction(connection, table, payload_bytes, inspect=False, row_count=8):
    start = time.perf_counter_ns()
    query(connection, "BEGIN OPTIMISTIC")
    query(connection, f"UPDATE `{table}` SET v = v + 1, payload = %s WHERE id BETWEEN 0 AND %s", (b"x" * payload_bytes, row_count - 1))
    buffer_info = None
    if inspect:
        buffer_info = query(connection, "SELECT MEM_BUFFER_KEYS, MEM_BUFFER_BYTES FROM information_schema.tidb_trx WHERE SESSION_ID = CONNECTION_ID()")
    commit_start = time.perf_counter_ns()
    query(connection, "COMMIT")
    commit_end = time.perf_counter_ns()
    # Read before any subsequent write transaction can overwrite this session field.
    info = json.loads(scalar(connection, "SELECT @@tidb_last_txn_info"))
    if info.get("error") or not info.get("txn_commit_mode"):
        raise RuntimeError(f"Missing or unsuccessful commit information: {info}")
    return {
        "commit_ms": (commit_end - commit_start) / 1e6,
        "transaction_ms": (commit_end - start) / 1e6,
        "info": info,
        "buffer_info": buffer_info,
    }


def measure(connection, nodes, output, name, table, protocol, payload, repeats, warmup, csv_writer, expected=None, row_count=8):
    configure(connection, protocol)
    before_regions = regions(connection, table)
    initial_sum = int(scalar(connection, f"SELECT SUM(v) FROM `{table}`"))
    for _ in range(warmup):
        transaction(connection, table, payload, row_count=row_count)
    # The diagnostic transaction is outside the timed sample and metric window.
    probe = transaction(connection, table, payload, inspect=True, row_count=row_count)
    before = settle(nodes)
    samples = []
    started = time.perf_counter()
    for iteration in range(repeats):
        sample = transaction(connection, table, payload, row_count=row_count)
        info = sample["info"]
        csv_writer.writerow({
            "case": name, "iteration": iteration, "table": table,
            "requested_protocol": protocol, "payload_bytes_per_row": payload, "rows_per_transaction": row_count,
            "commit_ms": sample["commit_ms"], "transaction_ms": sample["transaction_ms"],
            "start_ts": info["start_ts"], "commit_ts": info["commit_ts"],
            "actual_protocol": info["txn_commit_mode"],
            "one_pc_fallback": info["one_pc_fallback"],
            "async_commit_fallback": info["async_commit_fallback"],
        })
        samples.append(sample)
    wall_seconds = time.perf_counter() - started
    after = settle(nodes)
    after_regions = regions(connection, table)
    final_sum = int(scalar(connection, f"SELECT SUM(v) FROM `{table}`"))
    expected_sum = initial_sum + row_count * (warmup + 1 + repeats)
    if final_sum != expected_sum:
        raise RuntimeError(f"Incorrect final values: {final_sum} != {expected_sum}")
    modes = Counter(sample["info"]["txn_commit_mode"] for sample in samples)
    stable_regions = region_ids(before_regions) == region_ids(after_regions)
    correct_mode = expected is None or dict(modes) == {expected: repeats}
    observed = delta(before, after)
    expected_commit_rpcs = (repeats - modes.get("1pc", 0)) * len(before_regions)
    rpc_signature_valid = observed["user_commit_rpc"] == expected_commit_rpcs
    rpc_signature_valid &= observed["user_prewrite_rpc"] >= repeats * len(before_regions)
    if dict(modes) == {"1pc": repeats}:
        rpc_signature_valid &= observed["user_prewrite_rpc"] == repeats
    commits = [sample["commit_ms"] for sample in samples]
    result = {
        "case": name, "table": table, "requested_protocol": protocol,
        "payload_bytes_per_row": payload, "rows_per_transaction": row_count,
        "txn_commit_batch_size": int(scalar(connection, "SELECT @@global.tidb_txn_commit_batch_size")),
        "samples": repeats, "warmup": warmup, "actual_protocols": dict(modes),
        "commit_ms_p50": percentile(commits, 0.50), "commit_ms_p99": percentile(commits, 0.99),
        "transaction_ms_p50": percentile([sample["transaction_ms"] for sample in samples], 0.50),
        "sample_loop_seconds_including_diagnostics": wall_seconds,
        "rpc_and_cluster_counter_delta": observed,
        "rpc_per_transaction": {key: observed[key] / repeats for key in ("user_prewrite_rpc", "user_commit_rpc")},
        "probe": probe, "regions_before": before_regions, "regions_after": after_regions,
        "first_sample_start_ts": samples[0]["info"]["start_ts"],
        "region_ids_stable": stable_regions, "expected_protocol": expected,
        "expected_commit_rpcs": expected_commit_rpcs, "rpc_signature_valid": rpc_signature_valid,
        "passed": stable_regions and correct_mode and rpc_signature_valid,
    }
    write_json(output / f"{name}.json", result)
    save_snapshot(output, name + "-before", before)
    save_snapshot(output, name + "-after", after)
    print(f"{name}: {dict(modes)}, Prewrite/txn={observed['user_prewrite_rpc'] / repeats:g}, Commit/txn={observed['user_commit_rpc'] / repeats:g}, commit p50={result['commit_ms_p50']:.3f} ms", flush=True)
    if not result["passed"]:
        raise RuntimeError(f"Unexpected protocol, RPC count or Region change in {name}; see saved evidence")
    return result


def slow_query_samples(connection, cases):
    records = {}
    for case in cases:
        rows = query(connection, "SELECT TIME, QUERY, TXN_START_TS, PREWRITE_TIME, COMMIT_TIME, GET_COMMIT_TS_TIME, PREWRITE_REGION, WRITE_KEYS, WRITE_SIZE FROM information_schema.slow_query WHERE TXN_START_TS=%s AND QUERY LIKE %s ORDER BY TIME DESC LIMIT 1", (case["first_sample_start_ts"], "COMMIT%"))
        if not rows:
            raise RuntimeError(f"Missing COMMIT slow log for {case['case']}; use the supplied tidb.toml")
        records[case["case"]] = rows[0]
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=34000)
    parser.add_argument("--user", default="root")
    parser.add_argument("--database", default="txn_protocol_lab")
    parser.add_argument("--repeats", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.host not in ("127.0.0.1", "localhost"):
        parser.error("This runner is intended for a dedicated local playground")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", args.database):
        parser.error("Invalid database name")
    if args.repeats < 1 or args.warmup < 0:
        parser.error("Invalid sample count")
    args.output.mkdir(parents=True, exist_ok=False)
    connection = pymysql.connect(
        host=args.host, port=args.port, user=args.user,
        password=os.environ.get("TIDB_LAB_PASSWORD", ""), autocommit=True,
        cursorclass=pymysql.cursors.DictCursor, read_timeout=30, write_timeout=30,
    )
    original_batch = int(scalar(connection, "SELECT @@global.tidb_txn_commit_batch_size"))
    result = {"passed": False, "cases": []}
    try:
        nodes = query(connection, "SELECT TYPE, INSTANCE, STATUS_ADDRESS, VERSION, GIT_HASH FROM information_schema.cluster_info")
        if Counter(node["TYPE"] for node in nodes) != {"tidb": 1, "pd": 1, "tikv": 3}:
            raise RuntimeError("Expected exactly one TiDB, one PD and three TiKV instances")
        if any(node["VERSION"].lstrip("v") != "8.5.3" for node in nodes):
            raise RuntimeError("This experiment is pinned to v8.5.3")
        pd_url = "http://" + next(node["STATUS_ADDRESS"] for node in nodes if node["TYPE"] == "pd")
        query(connection, "SET GLOBAL tidb_txn_commit_batch_size = 16384")
        query(connection, f"CREATE DATABASE IF NOT EXISTS `{args.database}`")
        query(connection, f"USE `{args.database}`")
        for table in ("lab_single", "lab_cross"):
            query(connection, f"CREATE TABLE IF NOT EXISTS `{table}` (id BIGINT PRIMARY KEY CLUSTERED, v BIGINT NOT NULL, payload VARBINARY(65535) NOT NULL)")
            for key in range(8):
                query(connection, f"INSERT IGNORE INTO `{table}` VALUES (%s, 0, %s)", (key, b"seed"))
        query(connection, "SPLIT TABLE lab_cross BY (4)")
        single = regions(connection, "lab_single")
        cross = regions(connection, "lab_cross")
        if len(single) != 1 or len(cross) != 2:
            raise RuntimeError(f"Expected 1 and 2 Regions; got {len(single)} and {len(cross)}")
        metadata = {
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "platform": platform.platform(), "python": sys.version,
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "argv": sys.argv, "cluster": nodes,
            "tidb_version": scalar(connection, "SELECT TIDB_VERSION()"),
            "original_batch_size": original_batch,
            "single_regions": single, "cross_regions": cross,
            "replicas": verify_replicas(pd_url, single + cross),
            "client_go_revision": "827bf098b155",
            "random_seed": 20261103,
            "tidb_session_settings": query(connection, "SHOW VARIABLES WHERE Variable_name IN ('tidb_txn_mode','tidb_enable_async_commit','tidb_enable_1pc','tidb_guarantee_linearizability','transaction_isolation','tidb_dml_type')"),
        }
        write_json(args.output / "metadata.json", metadata)
        before_idle = settle(nodes)
        time.sleep(1)
        after_idle = snapshot(nodes)
        write_json(args.output / "idle-control.json", delta(before_idle, after_idle))
        fields = ["case", "iteration", "table", "requested_protocol", "payload_bytes_per_row", "rows_per_transaction", "commit_ms", "transaction_ms", "start_ts", "commit_ts", "actual_protocol", "one_pc_fallback", "async_commit_fallback"]
        with (args.output / "transactions.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            cases = [(f"single-{mode}", "lab_single", mode, 32, mode) for mode in PROTOCOLS]
            cases += [(f"cross-{mode}", "lab_cross", mode, 32, "2pc" if mode == "1pc" else mode) for mode in PROTOCOLS]
            random.Random(20261103).shuffle(cases)
            for name, table, mode, payload, expected in cases:
                result["cases"].append(measure(connection, nodes, args.output, name, table, mode, payload, args.repeats, args.warmup, writer, expected))
                stream.flush()
            # Sweep value size while leaving the number and encoded size of keys fixed.
            for payload in (512, 1024, 1800, 2000, 2048, 2304, 2336, 4096, 8192):
                name = f"size-{payload}"
                result["cases"].append(measure(connection, nodes, args.output, name, "lab_single", "1pc", payload, args.repeats, args.warmup, writer))
                stream.flush()
            result["cases"].append(measure(connection, nodes, args.output, "single-large-mutation", "lab_single", "1pc", 32768, args.repeats, args.warmup, writer, "1pc", row_count=1))
            stream.flush()
            # Hold the workload fixed; alter only the batching threshold, then restore it.
            for name, batch, expected in (("batch-wide", 65536, "1pc"), ("batch-narrow", 4096, "2pc"), ("batch-wide-again", 65536, "1pc")):
                query(connection, f"SET GLOBAL tidb_txn_commit_batch_size = {batch}")
                result["cases"].append(measure(connection, nodes, args.output, name, "lab_single", "1pc", 1024, args.repeats, args.warmup, writer, expected))
                stream.flush()
        modes_by_size = {case["case"]: case["actual_protocols"] for case in result["cases"] if case["case"].startswith("size-")}
        if not any("1pc" in modes for modes in modes_by_size.values()) or not any("2pc" in modes for modes in modes_by_size.values()):
            raise RuntimeError("Size sweep did not observe both 1PC and fallback")
        write_json(args.output / "slow-query-samples.json", slow_query_samples(connection, result["cases"]))
        result["passed"] = True
    except Exception as error:
        result["error"] = repr(error)
        raise
    finally:
        connection.rollback()
        query(connection, f"SET GLOBAL tidb_txn_commit_batch_size = {original_batch}")
        result["restored_batch_size"] = int(scalar(connection, "SELECT @@global.tidb_txn_commit_batch_size"))
        write_json(args.output / "summary.json", result)
        connection.close()
    print(f"Evidence saved to {args.output.resolve()}")


if __name__ == "__main__":
    main()
