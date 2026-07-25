# Fast Market 研究日志

只记录可复核的 shadow 数据、失败和运维证据。累计 ARRIVAL AVG 不作为当前网络 RTT；官方平台 delay 与可控执行路径分列。

## 2026-07-25 08:26 JST（2026-07-24 23:26 UTC）

### 运行与安全状态

- 本地 `/api/fast/state`：`ready=true`、`mode=SHADOW`、4/4 execution engines 在线、16 条逻辑 lanes、4 个市场、0 pending execution。
- champion：12 fills / 12 settled，equity `$7.790189`，净 P&L `-$22.209811`，fees `$1.602610`，win rate `25%`，ROI `-56.8945%`；`portfolio_incident_latched` 保持。
- observer 最终 API 快照：2931 candidates / 2540 fills / 2540 settled / 1572 wins，净 P&L `-$436.366074`，fees `$307.510504`，staked `$12122.052674`，accuracy `61.8898%`，ROI `-3.5998%`，累计 arrival avg `761.127ms`，运行 `65.838h`。
- `favorite-price-holdout-v1`：6 fills / 6 settled，净 P&L `-$6.512921`，fees `$0.521856`，equity `$23.487079`，win rate `50%`，ROI `-27.3899%`，运行 `8.227h`；已因 `challenger_portfolio_daily_loss_gate` 永久 `HALTED`，promotion `eligible=false`。冻结时间和规则未改。
- 三个 systemd user services 均为 `active/running`；主引擎 PID/启动时间未变，部署中未重启。

与 2026-07-24 15:25 UTC 交接快照相比：

- observer settled `2239 → 2540`（`+301`），净 P&L 约 `-$341.90 → -$436.37`（约再亏 `$94.47`）。
- challenger `1/0 → 6/6`，由约 `$29.95` 标记权益变为 `$23.487079`，并触发自身事故锁。
- 这些变化继续否定“高胜率等于正期望”；不支持推广或真实交易。

### 延迟原始指标

23:13 UTC 改动前，每项顺序为 P50 / P95 / max，单位 ms：

| Asset | warm HTTP | decision | event-loop lag | 可控路径 P95 合计 | ACK floor | 重连 RTDS/market |
|---|---:|---:|---:|---:|---:|---:|
| BTC | 78.00 / 159.95 / 803.44 | 0.04 / 0.08 / 75.14 | 21.43 / 216.97 / 774.32 | 377.00 | 377.00 | 0/0 |
| ETH | 78.29 / 158.36 / 480.98 | 0.04 / 0.10 / 66.43 | 21.05 / 216.99 / 774.31 | 375.45 | 375.45 | 0/0 |
| SOL | 78.32 / 157.54 / 326.37 | 0.04 / 0.10 / 0.28 | 21.05 / 216.98 / 774.32 | 374.62 | 374.62 | 0/1 |
| XRP | 78.03 / 155.77 / 398.65 | 0.03 / 0.07 / 0.11 | 21.43 / 216.99 / 774.31 | 372.83 | 372.83 | 2/0 |

- 官方 taker delay：四资产均为 `0ms`、verified；它没有混入上述可控路径。
- 独立只读 CLOB keep-alive probe（30 个 warm samples）：IPv4 P50/P95/P99/max `80.25/80.61/240.00/240.00ms`；IPv6 `79.66/81.28/159.46/159.46ms`，无错误。
- 23:26 UTC 改动后单点窗口的可控路径 P95：BTC `297.20ms`、ETH `296.86ms`、SOL `298.74ms`、XRP `300.09ms`。这是一次短窗口改善，不足以声称稳定改善，更不满足 `<100ms`。
- 最终 state age `0.40–1.36s`；0 pending execution。

### VPS 资源原始指标

- 内存：总计 `434,307,072B`，available `140,611,584B`；swap 总计 `1,073,737,728B`，使用 `136,396,800B`。
- 改动前 15 秒进程采样：主引擎 `66.89%` 单核、写 `7,598,080B`；challenger `7.45%` 单核、写 `2,097,152B`；dashboard `10.04%` 单核（采样期间本轮频繁请求 API）。
- `vmstat` 多个短窗口中 steal 持续约 `35–72%`，iowait 约 `1–22%`，不是一次孤立尖峰；但目前只有本轮记录，远未满足连续 7 天付费升级证据门槛。

### 统计研究快照

约 23:14 UTC 直接只读合并四个 observer SQLite（当时 2533 settled）：

- 净 P&L `-$449.966289`，fees `$306.509334`，slippage `$19.372922`，cost `$12088.558089`，ROI `-3.7222%`。
- accuracy `61.8634%`，Wilson 95% CI `[59.9552%, 63.7357%]`；Brier `0.228276`。
- 每笔平均 P&L `-$0.177642`，普通近似 95% CI `[-$0.322854, -$0.032429]`；区间完全小于 0。
- arrival latency P50/P95/max `559.678/2004.595/9951.273ms`。这是信号到模拟到达盘口的分布，不是当前 RTT。

价格区间（按 intended ask）：

| Ask 区间 | n | accuracy | 净 P&L | ROI | Brier |
|---|---:|---:|---:|---:|---:|
| 0.0–0.4 | 8 | 12.50% | -$12.164607 | -37.0964% | 0.316733 |
| 0.4–0.6 | 1047 | 49.2837% | -$345.067696 | -7.0090% | 0.262037 |
| 0.6–0.8 | 1160 | 67.4138% | -$77.187303 | -1.3828% | 0.222286 |
| 0.8–1.0 | 318 | 84.2767% | -$15.546682 | -1.0026% | 0.136740 |

方向差异：

- UP：n=1223，accuracy `61.3246%`，P&L `-$246.509883`，ROI `-4.2225%`，Brier `0.230531`。
- DOWN：n=1310，accuracy `62.3664%`，P&L `-$203.456406`，ROI `-3.2550%`，Brier `0.226170`。

arrival latency 分桶的净 ROI：`<=100ms` n=74、`+6.1563%`，但每笔 P&L 95% CI 跨 0；`100–250ms` n=450、`-6.7954%`；`250–500ms` n=656、`-4.0096%`；`500–1000ms` n=733、`-2.1674%`；`>1000ms` n=620、`-4.2196%`。低延迟小样本尚不能推翻总体负期望，也不能据此回改 holdout。

账本执行分布：

- champion latency P50/P95/max `40.292/73.991/73.991ms`，slippage `$0.338621`，但仅 12 笔且净 ROI `-56.8945%`。
- challenger latency P50/P95/max `643.080/9438.540/9438.540ms`，slippage `$0.050034`，仅 6 笔；Brier `0.317032`，样本远低于 500 笔和 336 小时门槛。

### 本轮假设、改动与验证

假设：已永久锁定且零持仓的 challenger 仍每 2 秒执行只读增量扫描、四账本汇总和约 80KB 状态重写，是明确的非关键软件浪费。锁定后放慢到 10 秒不会丢样本，因为 reader 保留 rowid 游标并在下一轮补读全部 observer 记录；COLLECTING 状态继续维持 2 秒。

实施的唯一代码改动：

- `fastchallenger.py`：只在返回状态为 `HALTED` 时把下一次轮询退避到至少 10 秒；异常路径仍按原 2 秒 fail-closed 重试。
- 未改 `favorite-price-holdout-v1` 冻结时间、筛选规则、账本、freshness、主引擎、live mode 或事故锁。

验证：

- 本地：38 项 Fast unittest 全过；`tests/validate_fast_dashboard.js` 通过；`py_compile`（fastchallenger/fastdash/fastbot/fastmulti）通过；`git diff --check` 无内容错误。
- VPS：部署只用 `deploy/push.sh`；远端 4 项 challenger 测试和静态编译通过。dashboard 按脚本安全重启；确认主引擎 0 pending 后，仅重启 `HALTED` 且 0 open positions 的 challenger。
- 重启后的首个 30 秒样本被一次性历史 rowid 回放污染（challenger `15.63%` 单核、写 `17,354,752B`），不能作为稳态对比；这是已记录的失败窗口。
- 回放完成后的 30 秒稳态：challenger `3.13%` 单核、写 `1,228,800B`。与改动前归一化比较，CPU 约下降 `58%`，写入速率约下降 `70.7%`。账本数值、事故锁和 observer 均保持。

### 结果与下一步

- `P95<100ms`：**未达成**。当前短窗口仍约 `297–300ms`；独立公网 warm P95 已约 `81ms`，剩余预算很窄，服务内 HTTP 尖峰和 event-loop lag 仍是主要项。
- 收益证据：**未达标**。总体 observer 费用、滑点和延迟后仍显著为负；holdout 仅 6 笔/8.227 小时且已事故锁定。
- 最新可验证教训：锁定后的非关键 follower 退避能明显减少自身软件写入和 CPU，但不能把短窗口改善解释为长期延迟达标；重启还会触发一次性历史扫描成本。
- 下一轮唯一优先项：在不重启、不改指标窗口的前提下持续记录至少一轮稳态 `vmstat` + 四引擎 P95，并定位主引擎约 0.5MB/s 写入中“每秒状态快照”与观察账本写入的可分离占比；没有连续证据前不再改代码，也不建议付费升级。

## 2026-07-25 14:35 JST（2026-07-25 05:35 UTC）

### 运行、安全与收益

- 部署前：`ready=true`、`mode=SHADOW`、4/4 execution engines、16 lanes、4 markets、0 pending execution、0 champion positions。champion 仍为 12/12 settled、equity `$7.790189`、P&L `-$22.209811`、fees `$1.602610`、ROI `-56.8945%`，`portfolio_incident_latched` 未动。
- 05:17 UTC 直接只读四个 observer SQLite：2709 settled / 1656 wins，P&L `-$563.054601`，fees `$330.365294`，slippage `$20.135369`，staked `$12,905.934701`，accuracy `61.1296%`（Wilson 95% CI `[59.2794%,62.9482%]`），ROI `-4.3628%`，Brier `0.230314`；每笔平均 P&L `-$0.207846`，普通近似 95% CI `[-$0.349411,-$0.066281]`。最终 API 因继续结算为 2717/2717、P&L `-$556.458462`、fees `$331.718211`、ROI `-4.2991%`。
- `favorite-price-holdout-v1` 仍冻结：6/6 settled，P&L `-$6.512921`，fees `$0.521856`，equity `$23.487079`，ROI `-27.3899%`，运行 `14.369h`，`HALTED` / `eligible=false`；未改规则、账本或事故锁。
- 价格区间 ROI：`0–0.4` n=14 `-39.0309%`；`0.4–0.6` n=1147 `-7.7839%`；`0.6–0.8` n=1211 `-1.8585%`；`0.8–1.0` n=337 `-0.7843%`。UP n=1297、ROI `-4.7506%`、Brier `0.232243`；DOWN n=1412、ROI `-4.0063%`、Brier `0.228542`。较高价格桶的 P&L CI 仍跨 0，不回改 holdout。
- selected probability 校准：`0.55–0.60` accuracy/ROI `42.48%/-13.0025%`；`0.60–0.70` `54.32%/-3.9368%`；`0.70–0.80` `64.73%/-3.3283%`；`0.80–0.90` `78.29%/+0.7848%`；`0.90–1.00` `87.02%/-0.3234%`。唯一正点估计的 P&L CI 跨 0。
- arrival latency P50/P95/max `560.262/2078.856/9951.273ms`（不是当前 RTT）。`<=100ms` 仍仅 n=74、ROI `+6.1563%`，每笔 P&L CI `[-$0.520683,+$1.123479]`；其余延迟桶为负。

### 延迟与资源原始指标

部署前完整窗口（HTTP / decision / event-loop 均为 P50/P95/max ms；括号为可控路径 P95、ACK floor、RTDS/market reconnect）：

- BTC `77.79/157.96/243.12`；`0.04/0.10/0.19`；`2.00/83.01/301.79`（`241.07`、`241.07`、`0/0`）。
- ETH `77.55/157.44/392.18`；`0.04/0.09/0.17`；`1.81/83.01/301.83`（`240.54`、`240.54`、`0/0`）。
- SOL `77.83/108.08/163.06`；`0.04/0.10/0.92`；`2.23/83.01/301.79`（`191.19`、`191.19`、`0/1`）。
- XRP `76.41/156.98/379.22`；`0.04/0.06/162.79`；`1.98/83.01/301.81`（`240.05`、`240.05`、`2/0`）。
- 官方 delay 四资产均 `0ms`、verified。独立 30-sample warm probe：IPv4 P50/P95/max `31.11/80.74/81.16ms`；IPv6 `38.64/80.37/80.41ms`，无错误。
- 重启后 HTTP 仅 19–20 个样本，可控路径 P95 `142.71–221.16ms`，不用于宣称改善或达标。
- 改动前 20.16s：主进程 CPU `54.13%` 单核、write_bytes `13,361,152B`（约 `662.8KB/s`）；四 champion DB 合计 mtime 85 次（`4.22/s`）。steal `28.6–68.6%`，iowait `1.6–16.5%`。
- 最终 15.12s：主进程 CPU `43.19%`、write_bytes `8,953,856B`（约 `592.2KB/s`）；四 champion DB 合计 mtime 8 次（`0.53/s`）。steal `14.3–62.3%`，iowait多数 `0%`、峰值 `13.0%`。DB 修改频率约降 `87.5%`，CPU 短窗约降 `20.2%`，但物理写盘仅降约 `10.7%`。
- 内存总计 `434,307,072B`，available `171,040,768B`；swap 使用 `79,859,712B / 1,073,737,728B`。三个 systemd user services 均 active/running。短窗 steal 证据只有 2 天，不满足付费升级门槛。

### 唯一改动、验证与失败

- 假设得到验证：`FastLedger.ensure_window()` 和 `FastLedger.state()` 在值不变时仍刷新 SQLite。现仅在 window open/source/metadata 或 account peak/drawdown 确实变化时更新；未改状态 freshness、策略、指标窗口、风控、holdout 或 live mode。
- 新增回归测试覆盖重复调用不刷新 `updated_ts`，同时验证真实 window 变化仍写入。
- 本地：39 项 Fast unittest、前端 validator、相关 py_compile、`git diff --check` 全过。VPS：仅用 `deploy/push.sh`；39 项 Fast unittest 分组全过，py_compile 通过；VPS 无 Node，前端 validator 只在本地执行。
- 部署前确认 0 pending、0 champion/challenger positions、四路 observer decision 均 WAIT；只重启主引擎，未重启 challenger。部署后 API ready、4/4 在线，日志无新错误。
- 失败：本机 C 盘可用空间为 0，首次补丁写入曾把 `fastledger.py`、随后一次日志写入把本文件截为 0 字节；两次均立即从当前 HEAD 的 git archive 恢复，并校验 blob hash完全一致，远端未受影响。测试临时目录/pycache 定向到 D 盘，没有删除用户数据。
- 最新教训：去除 SQLite no-op 可显著降低 DB 修改和一部分 CPU，但四份每秒原子状态 JSON 及文件系统元数据放大已成为主要物理写入；必须并列报告 DB mtime、process write_bytes 和 CPU。

### 结论与下一轮唯一优先项

- `P95<100ms`：**未达成**；部署前完整窗口 `191–241ms`，公网 warm P95 约 `80ms`。
- 收益证据：**未达标**；2709 笔费用、滑点和延迟后总体 ROI `-4.3628%`，每笔 P&L CI 完全低于 0；holdout 仅 6 笔/14.369h 且已永久锁定。
- 下一轮唯一优先项：不降低 1 秒状态 freshness、不重启清空指标，量化四份原子状态 JSON 的物理写放大，并评估安全的内存态临时路径或合并写入；同时记录第 3 天同口径 steal/iowait 与完整 P95。

## 2026-07-25 20:20 JST（2026-07-25 11:20 UTC）

### 运行、安全与收益

- 11:14:59 UTC 本地聚合 API：`ready=true`、`mode=SHADOW`、4/4 execution engines、16 lanes、4 markets；四引擎 state age `0.156–0.880s`，均为 `WAIT`，0 champion positions。
- champion 保持 12/12 settled、P&L `-$22.209811`、fees `$1.602610`、equity `$7.790189`、ROI `-56.8945%`，`portfolio_incident_latched` 未动。
- 约 11:17 UTC 直接只读四个 observer SQLite：2877 settled / 1738 wins，P&L `-$637.482067`，fees `$354.214469`，slippage `$21.319367`，staked/cost `$13,656.630967`，accuracy `60.4101%`（Wilson 95% CI `[58.6104%,62.1822%]`），ROI `-4.6679%`，Brier `0.232050`；每笔平均 P&L `-$0.221579`，普通近似 95% CI `[-$0.360105,-$0.083052]`。
- 与上一轮 2709 settled 的直接 SQL 快照相比：`+168` 笔，P&L 再降 `$74.427466`，ROI `-4.3628% → -4.6679%`；负期望证据增强。
- `favorite-price-holdout-v1` 仍冻结并永久 `HALTED`：6/6 settled、3 wins，P&L `-$6.512921`、fees `$0.521856`、slippage `$0.050034`、equity `$23.487079`、ROI `-27.3899%`，运行约 `20.041h`，promotion `eligible=false`。未改规则、账本或事故锁。

价格区间：

| intended ask | n | accuracy | 净 P&L | ROI | Brier |
|---|---:|---:|---:|---:|---:|
| 0.0–0.4 | 27 | 18.5185% | -$38.712910 | -34.3023% | 0.301195 |
| 0.4–0.6 | 1251 | 48.2014% | -$470.595505 | -8.0514% | 0.264358 |
| 0.6–0.8 | 1252 | 66.9329% | -$105.103476 | -1.7494% | 0.224513 |
| 0.8–1.0 | 347 | 84.1499% | -$23.070177 | -1.3643% | 0.137383 |

- 方向：UP n=1379、ROI `-4.3289%`、Brier `0.233394`；DOWN n=1498、ROI `-4.9805%`、Brier `0.230813`。
- selected probability 校准：`0.55–0.60` accuracy/ROI `41.61%/-14.1204%`；`0.60–0.70` `53.91%/-4.2174%`；`0.70–0.80` `65.12%/-2.4540%`；`0.80–0.90` `77.98%/+0.7122%`；`0.90–1.00` `86.57%/-0.9098%`。唯一正点估计仍不能替代自然时间 holdout，也不得用于回改冻结规则。
- observer arrival latency P50/P95/max `560.653/2084.145/9951.273ms`；这是信号到模拟到达盘口延迟，不是当前 RTT。`<=100ms` n=80、ROI `+5.6895%`，但每笔 P&L 95% CI `[-$0.528082,+$1.081610]` 跨 0；`100–250ms`、`250–500ms`、`500–1000ms`、`>1000ms` ROI 分别为 `-7.8834%/-5.6666%/-2.2860%/-5.3595%`。

### 延迟、资源与写入原始指标

11:14:59 UTC 完整 240 样本窗口（HTTP / decision / event-loop 均为 P50/P95/max ms；括号为可控路径 P95、官方 delay、RTDS/market reconnect）：

- BTC `38.95/81.07/298.46`；`0.04/0.08/0.10`；`18.53/103.70/379.32`（`184.85`、`0ms verified`、`1/1`）。
- ETH `35.91/82.99/318.07`；`0.02/0.05/47.51`；`18.70/103.70/379.32`（`186.74`、`0ms verified`、`1/0`）。
- SOL `37.96/81.41/241.10`；`0.02/0.05/0.13`；`18.52/103.71/379.33`（`185.17`、`0ms verified`、`1/1`）。
- XRP `37.03/81.64/321.11`；`0.02/0.05/0.10`；`18.87/103.70/379.32`（`185.39`、`0ms verified`、`1/1`）。
- 独立 30-sample IPv4 CLOB keep-alive probe：P50/P95/P99/max `80.17/80.68/114.32/114.32ms`，0 errors。公网 warm P95 本身已占 100ms 预算约 81%，不能靠隐藏 event-loop 尖峰宣称达标。
- 三个 systemd user services 均为 `active/running`。主引擎自 05:33:11 UTC 运行，challenger 自前一日 23:22:51 UTC 运行；本轮未重启。
- 内存总计 `434,307,072B`，available `155,119,616B`；swap 使用 `120,045,568B / 1,073,737,728B`。
- 25.04 秒 `/proc` 样本：主进程单核 CPU `64.50%`、write_bytes `14,315,520B`（`571,709B/s`）；challenger `2.96%`、`46,620B/s`；dashboard `0.72%`、0 writes。CPU 窗口 steal `49.00%`、iowait `2.25%`。
- 随后的 `vmstat 1 16` 中，逐秒 steal 多为 `26–60%`，iowait 多为 0、峰值 `9%`。这是第 3 天短窗证据，仍远未满足连续 7 天付费升级门槛。

四份原子状态 JSON 的同窗观测：

| 文件 | 当前大小 | 25.04s 内观测到的改写 | 逻辑载荷 |
|---|---:|---:|---:|
| BTC | 33,349B | 19 | 633,637B |
| ETH | 33,409B | 20 | 668,790B |
| SOL | 31,032B | 20 | 620,700B |
| XRP | 30,239B | 19 | 574,511B |

- 合计逻辑状态载荷 `2,497,638B / 25.04s = 99,746B/s`，约为同窗主进程 write_bytes 的 `17.4%`；主进程总写入与状态逻辑载荷之比为 `5.73×`，但该比值包含 observer SQLite、日志等其他写入，不能称为状态文件自身的物理放大倍数。
- 即使把每次载荷按 4KiB 向上取整，估算也约 `108KB/s`，只占主进程约 `19%`。证据不支持把剩余写入主要归因于四份状态 JSON，也不足以现在就承担跨进程 tmpfs 路径和启动顺序风险。

### 假设、验证、失败与决定

- 假设：“四份每秒原子状态 JSON 是主进程剩余约 0.57MB/s 写入的主要来源”。验证结果：**否定**；它们是稳定来源，但可观测逻辑载荷只解释约 `17–19%`，其余约 `80%` 仍需按 observer SQLite、决策日志和文件系统写入分离。
- 失败 1：首次批量输出 API 全量 JSON，因 recent records 过多被工具截断；随后改为字段投影，没有丢失必需指标。
- 失败 2：首个远端 `vmstat` 参数受 PowerShell CRLF 影响变成 `16\r` 而退出；已用独立只读命令成功重跑。
- 失败 3：两次 SQL 架构探测分别被 PowerShell 引号和 here-document 末尾换行阻断；均未修改远端。最终改用 base64 stdin，成功以 SQLite `mode=ro` 采集。
- 失败 4：首轮 Brier 脚本误把 `fair_probability` 当固定 UP 概率；该字段实际为所选方向概率。P&L/费用/滑点/延迟不受影响；Brier 与校准桶已重算为上述值，错误结果未用于结论。
- 本轮没有代码或运维改动；文档提交前本地 39 项 Fast unittest、前端 validator、相关 py_compile 与 `git diff --check` 全过。没有部署或服务重启。理由是没有足够证据支持一个明确且低风险的系统变更；保持 1 秒 freshness、指标窗口、shadow mode 和全部事故锁不变。

### 结论与下一轮唯一优先项

- `P95<100ms`：**未达成**；完整窗口可控路径 P95 `184.85–186.74ms`，独立公网 warm P95 `80.68ms`。
- 收益证据：**未达标**；2877 笔费用、滑点和延迟后 ROI `-4.6679%`，每笔 P&L 95% CI 完全低于 0；冻结 holdout 仅 6 笔/20.041h 且已永久锁定。
- 最新可验证教训：四份 1 秒状态 JSON 不是剩余物理写入的主要解释项；在迁移 tmpfs 或合并写入前，必须先隔离 observer SQLite、决策 JSONL、日志与原子替换的各自写入占比。
- 下一轮唯一优先项：不重启、不降低 freshness，使用低开销的按文件写入归因手段隔离主进程剩余约 `0.47MB/s`，优先确认 observer SQLite 与决策日志占比；没有可复现归因前继续只记录，不改代码、不建议付费升级。

## 2026-07-26 02:30 JST（2026-07-25 17:30 UTC）

### 运行、安全与收益

- 17:14 UTC 部署前本地聚合 API：`ready=true`、`mode=SHADOW`、4/4 execution engines、16 lanes、4 markets；四引擎 state age `0.083–1.041s`，champion/observer 均为 `WAIT`，0 pending execution、0 champion positions。
- champion 保持 12/12 settled、3 wins，P&L `-$22.209811`、fees `$1.602610`、slippage `$0.338621`、equity `$7.790189`、ROI `-56.8945%`；`portfolio_incident_latched` 未动。
- 17:20 UTC 直接只读四个 observer SQLite：3050 settled / 1848 wins，P&L `-$554.682802`，fees `$376.752185`，slippage `$21.945316`，staked/cost `$14,452.311602`，accuracy `60.5902%`（Wilson 95% CI `[58.8436%,62.3100%]`），ROI `-3.8380%`，Brier `0.232060`；每笔平均 P&L `-$0.181863`，普通近似 95% CI `[-$0.317117,-$0.046609]`。
- 与上一轮 2877 settled 的直接 SQL 快照相比：`+173` 笔，净 P&L 点估计回升 `$82.799265`，ROI `-4.6679% → -3.8380%`；但累计 P&L 和每笔 P&L 置信区间仍完全小于 0，不能解释为正期望或推广证据。
- `favorite-price-holdout-v1` 仍冻结并永久 `HALTED`：6/6 settled、3 wins，P&L `-$6.512921`、fees `$0.521856`、slippage `$0.050034`、equity `$23.487079`、ROI `-27.3899%`，运行约 `26.025h`，promotion `eligible=false`。冻结规则、账本和事故锁未改。

价格区间：

| intended ask | n | accuracy | 净 P&L | ROI | Brier |
|---|---:|---:|---:|---:|---:|
| 0.0–0.4 | 30 | 26.6667% | -$19.246484 | -15.5249% | 0.289421 |
| 0.4–0.6 | 1344 | 48.7351% | -$405.146375 | -6.4686% | 0.263672 |
| 0.6–0.8 | 1308 | 66.9725% | -$104.891205 | -1.6722% | 0.224451 |
| 0.8–1.0 | 368 | 83.9674% | -$25.398739 | -1.4170% | 0.138973 |

- 方向：UP n=1459、ROI `-2.4924%`、Brier `0.230481`；DOWN n=1591、ROI `-5.0735%`、Brier `0.233507`。
- selected probability 校准：`0.55–0.60` accuracy/ROI `42.81%/-11.2375%`；`0.60–0.70` `54.10%/-3.5719%`；`0.70–0.80` `65.51%/-1.5150%`；`0.80–0.90` `77.59%/+0.3000%`；`0.90–1.00` `85.90%/-1.6523%`。唯一正点估计桶的每笔 P&L 95% CI `[-$0.223491,+$0.252662]` 跨 0，不得用于回改冻结 holdout。
- observer arrival latency P50/P95/max `561.236/2082.640/9951.273ms`；这是信号到模拟到达盘口的分布，不是当前 RTT。`<=100ms` n=83、ROI `+7.5367%`，但每笔 P&L 95% CI `[-$0.424309,+$1.157770]` 跨 0；`100–250ms`、`250–500ms`、`500–1000ms`、`>1000ms` ROI 分别为 `-7.4949%/-5.4188%/-0.7046%/-4.6188%`。
- champion latency P50/P95/max `40.423/59.633/73.991ms`；challenger `721.601/7479.022/9438.540ms`。两者分别只有 12 笔和 6 笔，不能外推。

### 延迟与 VPS 资源原始指标

17:14 UTC 部署前完整 240 样本窗口（HTTP / decision / event-loop 均为 P50/P95/max ms；括号为可控路径 P95、官方 delay、RTDS/market reconnect）：

- BTC `39.43/80.72/237.94`；`0.04/0.08/75.88`；`18.08/77.24/454.95`（`158.04`、`0ms verified`、`1/1`）。
- ETH `39.09/80.61/377.58`；`0.03/0.07/0.13`；`18.37/65.40/454.94`（`146.08`、`0ms verified`、`1/0`）。
- SOL `47.66/83.11/323.07`；`0.02/0.06/0.10`；`18.11/65.39/454.95`（`148.56`、`0ms verified`、`1/1`）。
- XRP `38.99/80.65/483.17`；`0.02/0.05/52.12`；`18.11/65.39/454.95`（`146.09`、`0ms verified`、`1/1`）。
- 独立 30-sample IPv4 CLOB keep-alive probe：P50/P95/P99/max `18.84/80.05/80.39/80.43ms`，0 errors。公网 warm P95 已占 100ms 预算约 80%，当前完整窗口仍未达标。
- 25.28 秒 `/proc` 同窗：主进程单核 CPU `72.55%`、write_bytes `12,926,976B`（`511,350B/s`）、应用层 `wchar=2,817,879B`；CPU steal `50.97%`、iowait `2.63%`。
- 内存总计 `434,307,072B`，available `155,832,320B`；swap 使用 `118,550,528B / 1,073,737,728B`。
- 这是第 4 个有日期的短窗 steal 记录，仍不是连续 7 天证据；没有建议付费升级。

### 写入归因、假设与唯一改动

25.28 秒同窗按文件观测：

| 类别 | mtime 变化 | 应用层完整重写载荷 / 文件增长 |
|---|---:|---:|
| 四份 1 秒状态 JSON | 63 | 2,024,005B |
| 四份复盘 bundle JSON | 3 | 482,085B |
| fastbot.log | 5 | +402B |
| champion SQLite 家族 | 18 | +4,152B |
| observer SQLite 家族 | 18 | +32B |

- 状态与复盘 JSON 合计 `2,506,090B`，占同窗主进程应用层 `wchar` 的 `88.9%`；状态单项占 `71.8%`。上一轮把约 100KB/s 逻辑状态载荷直接除以约 0.57MB/s 块设备 write_bytes 后据此排除状态文件，口径不成立，因为两者之间还包含当前文件系统对原子重写的物理放大。
- 可复现浪费：`run_due_reviews()` 每 60 秒无条件更新 `updated_at` 并原子重写约 160KB bundle，即使 SQLite 没有生成任何新 hourly/daily/weekly review；这些文件没有运行时消费者，主状态中的 review 仍直接来自 SQLite。
- 本轮唯一代码改动：只有在确实创建新 review 或 bundle 文件缺失时才重写复盘 bundle；首次生成、真实新复盘、事故判断、1 秒状态 freshness、指标窗口、风控、holdout 和 live mode 均不变。
- 部署后 70.16 秒完整复盘周期：review mtime 变化 `0`、review 载荷 `0B`；状态 JSON 220 次改写、载荷 `6,369,229B`，占主进程 `wchar=6,600,728B` 的 `96.5%`。行为改动验证成功。
- 同一部署后窗口 process write_bytes 为 `543,296B/s`，没有比部署前形成可比下降；窗口相位、重启后初始化和 VPS steal 不同，不能声称物理写入或 P95 已改善。剩余写入研究重点现可收敛到四份状态快照及其原子替换，而不是 observer SQLite 或决策 JSONL。

### 测试、部署、失败与恢复

- 本地：39 项 Fast unittest、前端 validator、相关 py_compile、`git diff --check` 全过；新增回归断言在同一 review 周期内第二次调用不改 mtime 或内容。
- 首次短时 `strace` 写调用归因对 event loop 扰动明显，且采样包装脚本误把 `cancelled_write_bytes` 一并读入导致算术失败；该窗口未用于性能结论，后续改用 `/proc` + 文件 mtime/载荷低开销归因。
- 第一次调用 `bash deploy/push.sh` 被 Windows WSL `bash` 接管，`/c/...` 密钥路径在 WSL 下不存在，SSH 在上传前失败，VPS 未改变。随后显式使用 Git Bash 再运行同一个 `deploy/push.sh`，上传成功；没有手工 tar。
- VPS：39 项 Fast unittest 与静态编译通过；VPS 无 Node，前端 validator 只在本地执行。dashboard 按脚本安全重启。
- 主引擎只在远端脚本连续确认四资产 champion/observer 均 `WAIT`、0 pending、0 positions、`portfolio_incident_latched` 存在后重启；challenger 未重启。部署后 API `ready=true`、4/4 在线、16 lanes、0 pending、0 positions；三个 systemd user services 均 active/running，主引擎新日志无 warning。
- 重启后 HTTP 只有 22–23 个样本，未用于宣称延迟改善；本轮 P95 结论只使用部署前完整 240 样本窗口。

### 结论与下一轮唯一优先项

- `P95<100ms`：**未达成**；部署前完整窗口可控路径 P95 `146.08–158.04ms`，独立公网 warm P95 `80.05ms`。
- 收益证据：**未达标**；3050 笔费用、滑点和延迟后总体 ROI `-3.8380%`，每笔 P&L 95% CI 完全低于 0；冻结 holdout 仍仅 6 笔/26.025h 且永久锁定。
- 最新可验证教训：应用层载荷与块设备 write_bytes 不能直接按占比排除原子文件；去掉复盘空写后，四份 1 秒状态 JSON 已占主进程应用层写入约 `96.5%`，但尚未证明某个替代设计能安全降低物理写入。
- 下一轮唯一优先项：不重启、不降低 1 秒 freshness、不改变原子读取语义，在同一文件系统做受控原子写基线并量化四份状态快照的物理放大；只有同时设计好启动顺序、崩溃恢复、dashboard fallback 和回滚验收，才评估内存临时路径或合并 writer。
