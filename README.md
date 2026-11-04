# Experiments

Reproducible experiments for database and distributed systems articles.

## TiDB commit protocols

[tidb-commit-protocols](tidb-commit-protocols/) compares traditional 2PC, Async Commit, and 1PC in TiDB v8.5.3. It includes:

- A SQL walkthrough covering protocol selection, cross-Region fallback, request batching, and an oversized single mutation.
- A measurement runner and configuration files for 19 scenarios and 1,900 measured commits.
- Recorded SQL output, transaction data, slow-log samples, and request metrics.
- A standalone reproduction archive and SHA-256 checksums.

Start with the [English reproduction guide](tidb-commit-protocols/BUNDLE.md) or the [detailed Chinese guide](tidb-commit-protocols/README.md).

Article: [What Async Commit and 1PC Save in TiDB](https://github.com/JinheLin/notes/blob/master/tidb-commit-protocols/tidb-commit-protocols-en.md).

```bash
git clone https://github.com/JinheLin/experiments.git
cd experiments/tidb-commit-protocols
```
Reproducible database and distributed systems experiments
