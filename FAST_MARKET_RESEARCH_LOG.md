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
