# SQL walkthrough verification

An earlier version of the SQL walkthrough was executed on 2026-11-04 against a new local TiDB v8.5.3 Playground on macOS arm64. Its exact input is preserved in [manual.sql](manual.sql) in this record directory. The current [walkthrough](../../manual.sql) removes save/restore steps and redundant statements; its execution is recorded [separately](../sql-walkthrough-simplified-v8.5.3/README.md). Both SQL checks are separate from the 19-scenario, 1,900-commit measurement run in `results/local-v8.5.3`.

The installed launcher was `playground:v1.16.2-feature.fts`. The cluster was started without custom configuration files:

```bash
tiup playground v8.5.3 --tag txn-protocols-sql-check-20261104 \
  --db 1 --pd 1 --kv 3 --tiflash 0 \
  --without-monitor --port-offset 30000
```

All SQL ran in one connection using the installed MySQL 8.0.46 client. The equivalent command from the experiment directory is:

```bash
mysql --host 127.0.0.1 --port 34000 --user root --batch --raw \
  < results/sql-walkthrough-v8.5.3/manual.sql > mysql-output.txt
```

The expected and observed protocols matched in all ten transactions:

| Step | Table | Rows | Payload bytes per row | Batch threshold bytes | Requested path | Observed protocol |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | lab_single | 8 | 32 | 16384 | 2PC | `2pc` |
| 2 | lab_single | 8 | 32 | 16384 | Async Commit | `async_commit` |
| 3 | lab_single | 8 | 32 | 16384 | 1PC | `1pc` |
| 4 | lab_cross | 8 | 32 | 16384 | 1PC | `2pc` |
| 5 | lab_single | 8 | 2304 | 16384 | 1PC | `1pc` |
| 6 | lab_single | 8 | 2336 | 16384 | 1PC | `2pc` |
| 7 | lab_single | 8 | 1024 | 65536 | 1PC | `1pc` |
| 8 | lab_single | 8 | 1024 | 4096 | 1PC | `2pc` |
| 9 | lab_single | 8 | 1024 | 65536 | 1PC | `1pc` |
| 10 | lab_single | 1 | 32768 | 16384 | 1PC | `1pc` |

The initial Region checks showed one Region for `lab_single` and two for `lab_cross`. After the payload-size comparison, `lab_single` still occupied the same single Region. The final global batch threshold was restored to its original value, 16,384 bytes.

[mysql-output.txt](mysql-output.txt) contains the client output, including the complete `@@tidb_last_txn_info` JSON values. [verification.json](verification.json) records the protocol sequence and layout checks. This SQL-only check did not collect latency distributions, request attempt counters, slow-log timings, or Raft counters; those remain in the separate complete measurement record.
