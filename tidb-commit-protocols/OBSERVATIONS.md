# TiDB v8.5.3 提交协议的本机观测

本次正式验证开始于 2026 年 11 月 03 日 21:17，共完成 19 个场景、1,900 笔正式样本，全部通过协议、请求计数和数据校验。每场景另有 5 次预热和 1 次诊断事务。

完整英文文章见 [What Async Commit and 1PC Save in TiDB](https://github.com/JinheLin/notes/blob/master/tidb-commit-protocols/tidb-commit-protocols-en.md)。本文保留该文章对应的运行环境、详细结果和证据链接。

本机为 macOS arm64，内存 16 GiB。集群由一个 TiDB、一个 PD、三个 TiKV 构成，使用原生 TiUP Playground。相关三个 Region 在验证前均有三个健康 Peer。数据、日志和所有实例共用本机资源。

## 版本与启动方式

| 组件 | 版本 | 实际 Git Hash |
| --- | --- | --- |
| tidb | 8.5.3 | `dc2548aac79a712265e831cff2a3a896bc0a5a38` |
| tikv | 8.5.3 | `13b9af5c34ad0dd62b5ae5ee02e05806e6d31e0b` |
| pd | 8.5.3 | `65e429d26e81db93c590890ddacf18c4b970f299` |

TiDB 二进制的 Go 构建信息另确认了 Client-Go 依赖为 `v2.0.8-0.20250711063936-827bf098b155`，与源码分析所固定的版本一致。

本次使用本机已有的启动器版本，实际启动命令如下；在本实验目录执行时，配置路径可写为当前目录：

```bash
tiup playground:v1.16.2-feature.fts v8.5.3 \
  --tag codex-txn-protocols-20261103 \
  --db 1 --pd 1 --kv 3 --tiflash 0 \
  --without-monitor --port-offset 30000 \
  --db.config "$PWD/tidb.toml" \
  --kv.config "$PWD/tikv.toml"
```

正式运行命令为：

```bash
.venv/bin/python run.py \
  --repeats 100 --warmup 5 \
  --output results/local-v8.5.3
```

## 单 Region 与跨 Region 的请求路径

每笔事务更新相同的 8 行，payload 为每行 32 字节。下表的请求数为各场景 100 笔正式事务的完成请求增量除以 100，包含其后台 Commit。

| 场景 | Region 数 | 实际协议 | Prewrite 尝试每事务 | Commit 尝试每事务 |
| --- | --- | --- | --- | --- |
| `single-2pc` | 1 | `2pc` | 1 | 1 |
| `single-async_commit` | 1 | `async_commit` | 1 | 1 |
| `single-1pc` | 1 | `1pc` | 1 | 0 |
| `cross-2pc` | 2 | `2pc` | 2 | 2 |
| `cross-async_commit` | 2 | `async_commit` | 2 | 2 |
| `cross-1pc` | 2 | `2pc` | 2 | 2 |

三个单 Region 场景的关键差别很清楚：Async Commit 保留 Commit 请求，1PC 没有独立 Commit。跨 Region 场景只允许 1PC 时，全部 100 笔实际回退为 `2pc`；只允许 Async Commit 时，全部 100 笔采用 `async_commit`。

## 前台阶段时间与后台 Commit

以下阶段时间来自每个场景第一笔正式事务的 COMMIT 慢日志。时间单位为秒，不是整个场景的平均值。

| 场景 | Prewrite_time | Commit_time | Get_commit_ts_time | Commit 尝试每事务 |
| --- | --- | --- | --- | --- |
| `single-2pc` | 0.000935166 | 0.000769708 | 0.000116625 | 1 |
| `single-async_commit` | 0.000882417 | 0.000000000 | 0.000000000 | 1 |
| `single-1pc` | 0.000686709 | 0.000000000 | 0.000000000 | 0 |

**Async Commit 的前台 `Commit_time` 为 0，但完整窗口中仍有每事务一次 Commit 请求尝试。1PC 的前台该字段也为 0，且未记录 Commit 请求尝试。** 这里的零值只对应慢日志中的前台阶段字段，整个客户端 `COMMIT` 仍有延迟。结合源码，可以解释两条路径在前台依赖和独立 Commit 请求上的差异。

`Get_commit_ts_time` 为 0 不能证明不访问 PD。固定源码中的 Async Commit 和 1PC 还可能在 Prewrite 前获取 latest TS；当前慢日志字段不覆盖这个阶段。

## 单 Region 内的拆批回退

以下场景均保持 8 个键、一个 Region，批次阈值为 16,384 字节。`Write_size` 是每场景第一笔正式事务的慢日志值。

| 每行 payload 字节 | Write_size 字节 | 实际协议 | Prewrite 尝试每事务 | Commit 尝试每事务 |
| --- | --- | --- | --- | --- |
| 512 | 4360 | `1pc` | 1 | 0 |
| 1024 | 8456 | `1pc` | 1 | 0 |
| 1800 | 14664 | `1pc` | 1 | 0 |
| 2000 | 16264 | `1pc` | 1 | 0 |
| 2048 | 16648 | `1pc` | 1 | 0 |
| 2304 | 18696 | `1pc` | 1 | 0 |
| 2336 | 18952 | `2pc` | 2 | 1 |
| 4096 | 33032 | `2pc` | 2 | 1 |
| 8192 | 65800 | `2pc` | 4 | 1 |

本次边界落在每行 payload 2304 与 2336 字节之间。每行 2336 字节时，`Prewrite_region` 仍为 1，但记录的 Prewrite 请求尝试已是每事务 2 次，实际协议变为 `2pc`。这说明回退可以发生在 Region 内部。

这不是通用 SQL payload 上限。编码开销、键长度、mutation 分布和具体版本会改变边界。

## 固定事务内容只改变批次阈值

保持同一张表、同一组 8 个键、每行 1024 字节 payload，依次调整全局阈值。

| 场景 | 批次阈值字节 | 实际协议 | Prewrite 尝试每事务 | Commit 尝试每事务 |
| --- | --- | --- | --- | --- |
| `batch-wide` | 65536 | `1pc` | 1 | 0 |
| `batch-narrow` | 4096 | `2pc` | 2 | 1 |
| `batch-wide-again` | 65536 | `1pc` | 1 | 0 |

三个场景各自的 100 笔样本全部呈现 `1pc → 2pc → 1pc`。场景前后 Region ID 未变。结束后已确认全局批次设置恢复为 16384 字节。

## 超过阈值仍可使用 1PC

默认阈值为 16,384 字节时，8 行、每行 2304 字节的事务 `Write_size` 为 18696 字节，全部 100 笔仍为 1PC。另一个单行场景的 payload 为 32,768 字节，`Write_size` 为 32801 字节，也全部为 1PC，只有一个 Prewrite 请求。

这与[批次构造循环](https://github.com/tikv/client-go/blob/827bf098b155/txnkv/transaction/2pc.go#L2140)一致：先检查当前累计值是否小于阈值，再加入下一个完整 mutation，不预先检查加入后是否超限，也不拆分单个 mutation。单行场景中，初始累计大小 0 通过检查，随后一次加入完整的 32,801 字节 mutation。此时已无剩余 mutation，因此只生成一个超出阈值的批次，没有触发多批时关闭 1PC 的逻辑。**事务是否被拆成多批，比“是否超过 16 KiB”更能解释该路径。**

## 客户端计时

以下数字保留用于核对计时过程。样本为串行、单轮、每场景 100 笔；P99 使用最近秩法，接近该场景的最大观测值。

| 场景 | COMMIT P50 毫秒 | COMMIT P99 毫秒 |
| --- | --- | --- |
| `single-2pc` | 0.670 | 2.166 |
| `single-async_commit` | 0.382 | 0.825 |
| `single-1pc` | 0.433 | 3.650 |
| `cross-2pc` | 0.941 | 1.825 |
| `cross-async_commit` | 0.645 | 1.472 |

本轮单 Region 的 1PC P99 高于 2PC；不能因为提交阶段更少，就声称所有尾延迟指标都会更好。单机调度、磁盘和其他应用活动会影响这些样本，正式性能比较需要独立轮次和更长的测量。当前结果不支持生产环境加速比、磁盘写入减半或最大吞吐提升等结论。

## 证据与可复核范围

- [完整结果](results/local-v8.5.3/summary.json)包含各场景协议分布、请求尝试增量、数据检查及 Region 稳定性。
- [逐笔记录](results/local-v8.5.3/transactions.csv)保留 1,900 笔正式样本的时间戳、实际协议、回退标志和延迟。
- [慢日志样本](results/local-v8.5.3/slow-query-samples.json)按 `start_ts` 对应每个场景第一笔正式事务。
- [环境信息](results/local-v8.5.3/metadata.json)记录组件 Hash、健康 Peer、脚本 Hash 和设置。
- [空闲对照](results/local-v8.5.3/idle-control.json)中，用户 Prewrite 和 Commit 请求增量均为 0。
- 同目录的 `.prom` 文件保留每个场景前后、TiDB 和三个 TiKV 的选定原始指标。

本次通过的是正常路径、Region 限制、拆批边界和阈值因果对照。没有执行受控故障窗口、Leader Transfer 或 Region Merge 正确性实验。集群级 Raft proposal 计数只作诊断，不当作可逐事务归因的工作量。

具体复现步骤与后续验证设计见 [README.md](README.md)。
