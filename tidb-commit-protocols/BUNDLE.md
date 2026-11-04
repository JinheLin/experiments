# TiDB Commit Protocol Reproduction Bundle

This directory contains the complete runner, configuration, SQL walkthrough and recorded results for [What Async Commit and 1PC Save in TiDB](https://github.com/JinheLin/notes/blob/master/tidb-commit-protocols/tidb-commit-protocols-en.md).

Run the commands below from this experiment directory (`cd tidb-commit-protocols` from the repository root).

The recorded run used TiDB, TiKV and PD v8.5.3 on macOS arm64, with one TiDB, one PD and three TiKV processes. It contains 19 scenarios and 1,900 measured commits. This bundle does not claim an independent Ubuntu reproduction or a production performance result.

## Verify the files

After extracting into a new directory, verify all payload files on Linux:

```bash
sha256sum -c SHA256SUMS
```

On macOS, use:

```bash
shasum -a 256 -c SHA256SUMS
```

## Check the protocol choices with SQL

With TiUP and a MySQL 8.0-compatible client installed, start a fresh cluster:

```bash
tiup playground v8.5.3 --db 1 --pd 1 --kv 3 --tiflash 0 \
  --without-monitor --port-offset 30000
```

In another terminal, connect and follow the SQL examples in the article:

```bash
mysql --host 127.0.0.1 --port 34000 --user root
```

Alternatively, execute the same SQL blocks as a file:

```bash
mysql --host 127.0.0.1 --port 34000 --user root < manual.sql
```

The walkthrough uses a separate `txn_protocol_sql` database and checks ten transactions. Use a new Playground or a different database name for another run. Stop the temporary Playground with Ctrl-C when finished. See the [checked SQL output](results/sql-walkthrough-simplified-v8.5.3/README.md).

## Collect the complete measurements

The following procedure reproduces the 19-scenario data collection, including client timings, slow logs and request attempt counts. Use a separate fresh cluster from the SQL walkthrough.

With TiUP and Python 3 installed, prepare the driver in this experiment directory:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Start the isolated local cluster in one terminal:

```bash
tiup playground v8.5.3 \
  --tag txn-protocols-v853 \
  --db 1 --pd 1 --kv 3 --tiflash 0 \
  --without-monitor --port-offset 30000 \
  --db.config "$PWD/tidb.toml" \
  --kv.config "$PWD/tikv.toml"
```

In a second terminal, enter the same experiment directory and run:

```bash
.venv/bin/python run.py \
  --repeats 100 --warmup 5 \
  --output results/review-run
```

The output directory must be new. See [README.md](README.md) for port settings, validation checks and measurement definitions. Stop this playground with Ctrl-C after the run; its tag retains its data.

Compare the selected protocols and recorded request attempts with the [recorded measurements](results/local-v8.5.3/summary.json). Identical millisecond timings are not an expected reproduction target. The article and guide explain which conclusions require additional measurements.
