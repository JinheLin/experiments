# Simplified SQL walkthrough verification

The simplified article walkthrough was executed on 2026-11-04 against a fresh local TiDB v8.5.3 Playground on macOS arm64, using a MySQL 8.0.46 client. It removes save/restore statements, combines each pair of session protocol settings, removes repeated settings, and updates all eight rows without an unnecessary key-range filter. All ten transaction cases remain.

The installed launcher was `playground:v1.16.2-feature.fts`. No custom configuration files were used:

```bash
tiup playground v8.5.3 --tag txn-protocols-sql-simple-20261104 \
  --db 1 --pd 1 --kv 3 --tiflash 0 \
  --without-monitor --port-offset 30000
```

[manual.sql](manual.sql) preserves the exact input for this execution and matches the current [walkthrough file](../../manual.sql). All statements ran in one connection. The equivalent command from the experiment directory is:

```bash
mysql --host 127.0.0.1 --port 34000 --user root --batch --raw \
  < manual.sql > mysql-output.txt
```

The input contains 58 SQL statements, down from 68 in the earlier walkthrough. Each transaction selected the expected protocol:

| Step | Table | Rows | Payload bytes per row | Batch threshold bytes | Actual protocol | one_pc_fallback |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | lab_single | 8 | 32 | 16384 | `2pc` | false |
| 2 | lab_single | 8 | 32 | 16384 | `async_commit` | false |
| 3 | lab_single | 8 | 32 | 16384 | `1pc` | false |
| 4 | lab_cross | 8 | 32 | 16384 | `2pc` | true |
| 5 | lab_single | 8 | 2304 | 16384 | `1pc` | false |
| 6 | lab_single | 8 | 2336 | 16384 | `2pc` | true |
| 7 | lab_single | 8 | 1024 | 65536 | `1pc` | false |
| 8 | lab_single | 8 | 1024 | 4096 | `2pc` | true |
| 9 | lab_single | 8 | 1024 | 65536 | `1pc` | false |
| 10 | lab_single | 1 | 32768 | 16384 | `1pc` | false |

The Region queries showed one Region for `lab_single` and two for `lab_cross`. After the size comparison, `lab_single` still occupied Region `275`. A separate post-run check confirmed `SUM(v) = 65` for `lab_single` and `SUM(v) = 8` for `lab_cross`, matching all updates. The final batch threshold was 16,384 bytes, explicitly set for the last case; no save/restore step was used. Playground was stopped after verification.

[mysql-output.txt](mysql-output.txt) contains the SQL output. [verification.json](verification.json) records the input hash, component versions, selected protocols, Region checks, and final data checks. Latency distributions and request attempt counts remain in the separate 19-scenario measurement record under `results/local-v8.5.3`.
