# TiDB 提交协议的 Playground 验证

这组实验研究同一个问题：TiDB 的提交优化分别减少了客户端等待、提交请求，还是持久化状态转换。使用固定版本的 `tiup playground`，逐笔确认实际提交协议，再把前台慢日志与包含后台工作的请求尝试计数放在一起比较。

完整英文文章 [What Async Commit and 1PC Save in TiDB](https://github.com/JinheLin/notes/blob/master/tidb-commit-protocols/tidb-commit-protocols-en.md) 已把协议分析、实验方法、结果和边界解释整合在一起。本目录提供文章对应的完整脚本、配置和原始记录。

已完成的本机观测见 [OBSERVATIONS.md](OBSERVATIONS.md)。协议路径和批次回退可以在 Playground 中直接复现；本方案不把单机计时结果当成生产环境性能结论。

## 先用 SQL 逐步验证

只验证协议选择和回退条件时，使用 TiUP 和兼容 MySQL 8.0 的命令行客户端即可。启动一个全新的本地集群，不需要准备 Python 环境或自定义配置文件：

```bash
tiup playground v8.5.3 --db 1 --pd 1 --kv 3 --tiflash 0 \
  --without-monitor --port-offset 30000
```

保持这个终端运行，在另一个终端连接：

```bash
mysql --host 127.0.0.1 --port 34000 --user root
```

按照[文章中的 SQL 步骤](https://github.com/JinheLin/notes/blob/master/tidb-commit-protocols/tidb-commit-protocols-en.md#reproducing-the-workload)依次建表、切换提交协议，并在每次 `COMMIT` 后立即读取同一连接的 `@@tidb_last_txn_info`，再继续执行后文的大小边界和批次阈值实验。

完整 SQL 同时保存在 [manual.sql](manual.sql)，内容与文章中的 SQL 代码块一致。也可以在本目录一次执行全部步骤：

```bash
mysql --host 127.0.0.1 --port 34000 --user root < manual.sql
```

该文件使用独立的 `txn_protocol_sql` 数据库；再次验证时应使用新的 Playground，或自行换一个实验库名。SQL 会完成 10 笔事务，展示三种协议、跨 Region 回退、单 Region 内拆批回退，以及单个 32 KiB value 仍可使用 1PC。实验结束后，在 Playground 终端按 Ctrl-C 停止这个临时集群即可。

精简后的步骤已于 2026-11-04 在本机实际执行，输出和核对结果见 [SQL 验证记录](results/sql-walkthrough-simplified-v8.5.3/README.md)。它验证协议选择；下文的完整采集流程用于重复测量提交延迟、慢日志和包含后台工作的请求尝试次数。

## 核心论证与验证方法

| 编号 | 从原稿保留的论证 | 验证方法 | 判定依据及边界 |
| --- | --- | --- | --- |
| C1 | 传统 2PC 的前台需要完成 Prewrite 和包含 Primary 的 Commit | 对同一单 Region 事务关闭两个优化，记录实际协议、慢日志阶段时间和请求尝试增量 | 应为 `2pc`，有 Prewrite 和 Commit 请求尝试；阶段依赖还需结合协调器源码解释，不能从请求尝试次数单独推出先后关系 |
| C2 | Async Commit 将 Primary Commit 的等待移出前台，但保留后续 Commit | 只允许 Async Commit，提交后等待请求计数稳定，比较前台 `Commit_time` 与总 Commit 请求尝试次数 | 前台 `Commit_time` 为 0，同时 Commit 请求尝试仍有增量；不能把 0 解读成没有后台工作 |
| C3 | 成功的 1PC 消除了独立 Commit 请求和普通 prewrite lock 的中间持久化 | 只允许 1PC，使用相同单 Region 工作负载，检查实际协议及请求计数 | 实际为 `1pc`，每事务一个 Prewrite、零个 Commit 请求尝试；直接写最终 MVCC 记录的解释由 TiKV 源码支撑，不据此声称物理 I/O 减半 |
| C4 | 单 Region 并不足以保证 1PC | 固定键集合、行数和 Region，先扫描 value 大小，再固定事务内容只改变批次阈值 | 同一 Region 内，批次数增加时回退；阈值恢复后重新使用 1PC，以区分拆批原因与 Region 分裂、键数变化等原因 |
| C5 | 批次阈值不是严格的请求字节上限 | 在默认阈值下观察略超阈值的多行事务，以及单个较大 mutation | 超过阈值仍可能只有一批；依据是实际协议、请求尝试计数、慢日志 `Write_size` 和批次构造循环，不能只按 SQL 行数猜测 |
| C6 | 跨 Region 事务不能使用 1PC，但可以使用 Async Commit | 给同结构表显式拆成两个 Region，重复协议对照 | 请求开启 1PC 时实际回退为 `2pc`；关闭 1PC 且允许 Async Commit 时实际可为 `async_commit` |
| C7 | CommitTS 和恢复协议是优化成立的正确性前提 | 阅读固定版本的时间戳与恢复分支；后续可增加受控失败窗口实验 | 普通 Playground 的成功提交测试不能证明任意故障下的正确性，也不能直接验证 Leader Transfer、Merge 等全部不变量 |

文中要使用的主张是：**Async Commit 保留了独立 Commit 状态转换；1PC 在符合条件时消除了这一步。** 不把它扩大为“Async Commit 的全部工作量与 2PC 完全相同”，也不预设吞吐或 P99 必然改善。

## 固定版本和工作负载

- TiDB、TiKV、PD 均固定为 `v8.5.3`，不使用 `nightly`。
- 一个 TiDB、一个 PD、三个 TiKV，关闭 TiFlash 和独立监控组件。直接读取各组件的 `/metrics`，无需 Prometheus。
- 脚本通过 PD 确认相关 Region 有三个健康 Peer，避免把单副本结果误写成三副本结果。
- 显式使用 `BEGIN OPTIMISTIC`，隔离悲观锁获取、内存锁和 Fair Locking 的影响；不改变数据库默认事务模式。
- 表只有聚簇主键，没有二级索引、外键或触发器。除单行大 mutation 对照外，每次更新固定的 8 个键，保证不因额外索引写入改变参与 Region。
- `lab_single` 必须只有一个 Region；`lab_cross` 在 `id=4` 处分裂为两个 Region，每次更新覆盖分裂点两侧。每个场景前后保存 Region ID，发生变化则失败。
- 基线批次阈值固定为 16,384 字节。阈值实验只在此隔离集群中修改 `GLOBAL tidb_txn_commit_batch_size`，脚本退出时恢复原值。

TiDB v8.5.3 使用的 Client-Go 版本为 `827bf098b155`，来源是 [固定版本的 go.mod](https://github.com/pingcap/tidb/blob/v8.5.3/go.mod)。运行时会保存各组件的实际版本、Git Hash、Region 信息和脚本 SHA256。

## 完整采集：启动集群

从 `experiments` 仓库根目录执行以下命令。需要已安装 TiUP 和 Python 3；不依赖 Docker，也不需要编译程序。

```bash
cd tidb-commit-protocols
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

在一个终端启动 Playground，保持这个终端运行：

```bash
tiup playground v8.5.3 \
  --tag txn-protocols-v853 \
  --db 1 --pd 1 --kv 3 --tiflash 0 \
  --without-monitor --port-offset 30000 \
  --db.config "$PWD/tidb.toml" \
  --kv.config "$PWD/tikv.toml"
```

默认端口偏移后，SQL 为 `127.0.0.1:34000`，TiDB 状态接口为 `127.0.0.1:40080`，PD 为 `127.0.0.1:32379`。本次选择偏移端口是为了避开已有程序；若这些端口也已占用，换一组偏移并相应修改运行脚本的 `--port`。

`tikv.toml` 缩小了每个 TiKV 的块缓存及线程数，便于笔记本运行。三个实例仍共享同一台机器的 CPU、内存和磁盘。该拓扑能验证协议行为，不能模拟三台物理机器的网络与故障独立性。

TiUP 用法参见 [官方 Playground 文档](https://docs.pingcap.com/tidb/stable/tiup-playground/)。本次实际运行使用本机已有的 `playground:v1.16.2-feature.fts`；它是启动器版本，数据库组件仍为未经修改的 `v8.5.3`。完整启动命令记录在观测报告中。

## 完整采集：运行脚本

在另一个终端进入同一目录：

```bash
.venv/bin/python run.py \
  --repeats 100 --warmup 5 \
  --output results/my-v8.5.3-run
```

输出目录必须尚不存在，防止覆盖结果。脚本只连接本机，在专用的 `txn_protocol_lab` 数据库中创建、复用和更新实验表，不删除表。需要重新准备数据时，可使用 `--database txn_protocol_lab_second` 创建另一个实验库。

脚本完成 19 个场景，默认包含 1,900 个正式计时样本。每个场景另有 5 次预热和 1 次诊断事务，不计入正式样本及其请求计数窗口。

| 实验组 | 场景 | 控制变量 |
| --- | --- | --- |
| 单 Region 协议对照 | `single-2pc`、`single-async_commit`、`single-1pc` | 相同 8 行，每行 payload 32 字节；分别只允许目标协议 |
| 跨 Region 对照 | `cross-2pc`、`cross-async_commit`、`cross-1pc` | 同样的 8 行和 payload，但键落在两个 Region；最后一种应回退 |
| 批次边界扫描 | 每行 payload 为 512、1024、1800、2000、2048、2304、2336、4096、8192 字节 | 键数、键集合、单 Region 布局不变，只允许 1PC，观察实际选择 |
| 大 mutation 对照 | 单行 payload 为 32,768 字节 | 单个 mutation 超过默认批次阈值，检查是否仍可 1PC |
| 批次阈值因果对照 | `batch-wide`、`batch-narrow`、`batch-wide-again` | 相同 8 行、每行 payload 1024 字节；批次阈值依次为 65,536、4096、65,536 |

协议开关矩阵如下。关闭优化时允许使用常规 2PC；“1PC”配置仍可能回退。

| 配置 | `tidb_enable_1pc` | `tidb_enable_async_commit` |
| --- | --- | --- |
| `2pc` | 0 | 0 |
| `async_commit` | 0 | 1 |
| `1pc` | 1 | 0 |

不在基础对照中同时开启两种优化，避免一次回退后进入另一种优化路径而混淆解释。两个开关都只是候选协议开关，见 [TiDB 系统变量文档](https://docs.pingcap.com/tidb/stable/system-variables/#tidb_enable_1pc-new-in-v50)。

## 逐笔确认实际协议

脚本在每次 `COMMIT` 成功后，立即在同一连接读取：

```sql
SELECT @@tidb_last_txn_info;
```

保留完整的 `start_ts`、`commit_ts`、`txn_commit_mode`、`one_pc_fallback` 和 `async_commit_fallback`。不能换连接读取，也不能在中间插入另一笔写事务。

对于已经验证的三种正常路径，`txn_commit_mode` 分别为 `2pc`、`async_commit`、`1pc`。回退字段帮助识别尝试，但不能独自定位原因；Region 布局、请求批次数和阈值对照共同确定原因。字段定义见 [固定版本 Client-Go 的 TxnInfo](https://github.com/tikv/client-go/blob/827bf098b155/txnkv/transaction/txn.go#L915)。

## 区分前台等待与后台工作

脚本分别计时：

- `commit_ms`：只覆盖客户端执行 `COMMIT` 到收到响应。
- `transaction_ms`：覆盖 `BEGIN`、`UPDATE`、`COMMIT`，不含事后读取协议信息。

协议诊断、慢日志查询和指标抓取均不进入 `commit_ms`。样本循环本身有诊断开销，因此其总时间不能直接解释为数据库最大吞吐。

`tidb.toml` 将慢日志阈值设为 0。脚本在全部场景结束后，根据每个场景第一笔正式样本的 `start_ts` 找到对应的 `COMMIT` 记录，导出：

```sql
SELECT TIME, QUERY, TXN_START_TS,
       PREWRITE_TIME, COMMIT_TIME, GET_COMMIT_TS_TIME,
       PREWRITE_REGION, WRITE_KEYS, WRITE_SIZE
FROM information_schema.slow_query
WHERE TXN_START_TS = <样本的 start_ts>
  AND QUERY LIKE 'COMMIT%';
```

这些阶段时间的单位是秒。`Commit_time = 0` 表示该前台阶段字段记录为零，不等于整个客户端 `COMMIT` 延迟为零，也不能据此推断后台没有 Commit。`Prewrite_region` 是参与 Region 数，也不能当成 Prewrite 请求数：一个 Region 可以发送多个批次。

请求尝试次数来自 TiDB `/metrics` 中客户端请求耗时直方图的计数：

```text
tidb_tikvclient_request_seconds_count{scope="false",type="Prewrite",...}
tidb_tikvclient_request_seconds_count{scope="false",type="Commit",...}
```

在固定版本中汇总全部 Store、过滤 `scope="false"` 的用户请求。每个场景在预热及诊断事务后读取起点，正式样本结束后每 100 ms 检查一次，连续三次不变后读取终点，最多等待 10 秒。

这里的“请求尝试”指客户端埋点记录的请求调用，返回错误的尝试也可能计入，重试会使计数增加。它不是 gRPC 调用数、网络往返数或成功服务端写入数。原始结果中的 `rpc_per_transaction`、`user_prewrite_rpc`、`user_commit_rpc` 等字段名保留不变，按此测量口径解释。

“计数暂时稳定”本身不能证明后台已全部完成。因此脚本还要求：本工作负载中，每个非 1PC 事务应记录每 Region 一个 Commit 尝试，1PC 应为零；Commit 尝试总数多出或缺少都会令场景失败。对 1PC 还要求每事务恰好一个 Prewrite 尝试。其他路径的 Prewrite 尝试至少要覆盖参与 Region，实际总数如实保留。

同时保存三个协议的成功计数器作辅助检查。这些计数器包含内部及其他会话事务，不能取代逐笔 `tidb_last_txn_info`。脚本另采集一秒空闲窗口，检查用户写入计数是否受到背景活动污染。

## 批次边界的解释

Prewrite 按 key 和 value 的大小构造批次，而 Commit 主要按 key 分批，所以不能假设两者请求数一定相等。一个 Region 内的 4 次 Prewrite 请求可以对应一次 Commit 请求。

固定版本的构造逻辑可以概括为：

```text
while 还有 mutation 且 当前批累计大小 < 阈值:
    把下一个完整 mutation 加入当前批
```

检查发生在加入下一项之前，代码不会预先判断“当前大小加上下一项是否超限”。每个 mutation 都完整加入，最后一项可以使批次越过阈值；单个 mutation 不会因为超过阈值而被拆开。

例如，单行 32 KiB payload 场景的批次初始大小为 0，小于 16,384 字节，因此完整加入大小为 32,801 字节的 mutation。此时已无剩余 mutation，最终只生成一个超出阈值的批次，不会触发“多个 Prewrite 批次时关闭 1PC”的逻辑。其他条件满足时，事务仍可使用 1PC。因此应观察实际批次数，而不是声称“事务总量超过 16 KiB 就会回退”。参见 [批次大小计算](https://github.com/tikv/client-go/blob/827bf098b155/txnkv/transaction/2pc.go#L938)、[构造循环](https://github.com/tikv/client-go/blob/827bf098b155/txnkv/transaction/2pc.go#L2140)和[多批时关闭 1PC](https://github.com/tikv/client-go/blob/827bf098b155/txnkv/transaction/2pc.go#L1579)。

`Write_size` 是本次提交键值量的诊断信息，不是完整 RPC 的 protobuf 字节数。`MEM_BUFFER_BYTES` 仅作附加内存诊断，不用于计算这个边界。SQL payload、mutation 大小、请求大小和内存占用必须分开。

## 时间戳与持久化结论的范围

在固定 Client-Go 版本中，传统 2PC 在 Prewrite 后获取提交时间戳，而 Async Commit 和 1PC 在需要保证实时时序时，还会在 Prewrite 前获取 latest TS。因此慢日志的 `Get_commit_ts_time = 0` 不等于“不访问 PD”，也不能据此断言总 TSO 请求数减少。见 [Prewrite 前的时间戳协调](https://github.com/tikv/client-go/blob/827bf098b155/txnkv/transaction/2pc.go#L1751)和[传统 2PC 的提交时间戳获取](https://github.com/tikv/client-go/blob/827bf098b155/txnkv/transaction/2pc.go#L1888)。

独立 Commit 请求的有无是可直接观测的协议事实。结合 TiKV 的[普通 Commit 写路径](https://github.com/tikv/tikv/blob/v8.5.3/src/storage/txn/commands/commit.rs)与 [1PC 直接生成 Write 记录的路径](https://github.com/tikv/tikv/blob/v8.5.3/src/storage/txn/commands/prewrite.rs#L967)，可以解释独立持久化状态转换是否被消除。

脚本也保存 `tikv_raftstore_proposal_total{type="normal"}`，但只作整个集群的诊断量。它混有其他 Region 的活动，且短窗口中可能受到内部指标刷新时机影响。本次观测确实出现过某个短窗口为 0、随后窗口集中增加的情况，**不能把该增量除以事务数，写成精确的每事务 Raft proposal 数**。

本实验没有测量或证明 fsync 次数、落盘字节、Compaction 放大以及生产环境最大吞吐。若文章保留这些性能结论，需要独立的长时间测量和隔离方案。

## 输出与失败判定

输出目录包含：

| 文件 | 用途 |
| --- | --- |
| `metadata.json` | 系统信息、组件版本和 Hash、Region 和 Peer、会话设置、脚本 Hash |
| `transactions.csv` | 每笔正式样本的协议、回退标志、时间戳和客户端延迟 |
| `summary.json` | 全部场景结果、P50/P99、协议分布、请求尝试增量和最终恢复的批次设置 |
| `<场景>.json` | 单个场景的完整结果及前后 Region 布局 |
| `<场景>-before-*.prom` 和 `after-*.prom` | TiDB 及三个 TiKV 的原始选定指标 |
| `slow-query-samples.json` | 每个场景一笔正式样本的 COMMIT 慢日志 |
| `idle-control.json` | 空闲窗口的背景计数变化 |

脚本会检查：固定组件版本和拓扑、三副本状态、Region 稳定性、预期协议、请求数、所有更新后的累计值，以及批次扫描是否同时观察到 1PC 和 2PC。出现异常时保留已完成的结果，并在 `summary.json` 写入错误；不自动重试事务或把异常样本删掉。

结束后在 Playground 终端按 Ctrl-C。指定 tag 的数据会保留在 TiUP 数据目录中。不要为本实验停止其他 TiUP 集群或修改其他集群配置。

## 进一步验证延迟和故障恢复

若文章需要比较延迟，先在固定的空闲环境中预热，增加正式样本，并用多个独立轮次检验结果是否稳定。例如：

```bash
.venv/bin/python run.py \
  --repeats 1000 --warmup 20 \
  --output results/timing-trial-1
```

为每轮使用新的输出目录，保存完整分布，分开比较相同工作负载中的协议。100 个样本的 P99 主要用于检查流程，不能作为稳定的尾延迟结论。需要吞吐结论时，还应另设并发度及负载递增实验，记录成功数、失败数、重试、资源利用率和后台排空时间；当前串行脚本不承担饱和吞吐测试。

故障恢复应作为独立实验：对传统 2PC，在全部 Prewrite 完成但 Primary Commit 尚未完成的窗口中断协调器；对 Async Commit，在全部 Prewrite 已完成、后台 Commit 被受控阻塞的窗口中断协调器，然后用新会话触发读取和 Lock Resolver，检查两行是否恢复成一致的提交结果。还需要分别覆盖 Prewrite 不完整和 1PC 响应丢失的情况。

普通 `kill` 不能保证命中这些窗口；客户端收到成功后立即退出，也可能发生在后台 Commit 全部完成之后。标准 Playground 二进制没有提供本方案可依赖的受控暂停接口，所以这些是后续的定点故障实验，未包含在当前“通过”结果中。可通过 Playground 的 `--db.binpath`、`--kv.binpath` 接入含明确测试钩子的二进制，但需单独固定构建方式并验证钩子有效性。

当前最适合交给编辑讨论的范围，是 C1 至 C6 的可复现行为，以及它们揭示的“前台等待”和“状态转换工作”之间的区别。CommitTS 和故障恢复保留为解释优化安全性的必要背景，不把尚未进行的故障测试写成实证结论。
