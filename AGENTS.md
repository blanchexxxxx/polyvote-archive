# Polyvote agent instructions

The following is a synchronized copy of `FAST_MARKET_MEMORY.md` and is authoritative project-level guidance. Read and follow it before changing or operating the system.

Local workstation override (2026-07-24): use `C:\Users\<user>\.ssh\<ssh-key>.pem`. This supersedes the old-computer path `C:\Users\<user>\.ssh\<ssh-key>.pem` inside the synchronized snapshot below.

# Fast Market 工作记忆

最后更新：2026-10-13 JST（项目中止）

这份文件是当前快市场系统的唯一快速交接入口。旧 `HANDOFF.md` 主要描述已退休的慢市场，而且存在编码损坏；继续工作时先读本文件。

## 项目状态：已中止（2026-10-13）

- 2026-08-15：按复盘结论停止并 disable 了三个快市场服务（`polyvote-fast` / `polyvote-fast-challenger` / `polyvote-fast-dashboard`），详见 `FAST_MARKET_RESEARCH_LOG.md`。
- 2026-10-13：Lightsail VPS 实例已由用户停止。复核时 SSH 22 端口连接超时，本机 dashboard 隧道未运行，`127.0.0.1:8920` 不可达。
- 项目暂时中止，仓库准备公开。下文“当前结论”及之后的内容是 2026-08-01 的运行快照，**不代表现状**，仅作历史记录。
- 全程为 shadow / 模拟盘，无钱包私钥、无真实下单，停机不留真实仓位或资金风险。
- 若要恢复：重新启动实例后公网 IP 可能已变（未绑静态 IP 时），需同步更新 `deploy/keep-dashboard-tunnel.ps1` 的 `$RemoteHost` 与本文件的访问信息，然后逐个 `systemctl --user enable --now` 服务并核对状态。

## 当前结论（2026-08-01 快照）

- （2026-08-01 时）系统在 AWS Lightsail VPS 上持续运行。现已停机，见上方项目状态。
- 当前完全是 **shadow / 模拟盘**，没有钱包私钥，没有真实下单。
- 慢市场已退休，不再开发或展示。
- 原严格资金冠军账本从 `$30.00` 亏至 `$7.790189`，已被 `portfolio_incident_latched` 永久风控锁定，不再产生订单。
- 独立 observer 使用归一化 `$5` 假设订单持续收集样本，不占用也不扣减冠军账本；它是研究统计器，不是严格资金模拟账户。
- observer 的早期正收益已被大样本证伪：从 217 笔时的 `+$27.69961` 变成约 2239 笔时的 `-$341.90`。高胜率没有转化为正期望。
- 独立 `$30` 有限现金实验 `favorite-price-holdout-v1` 已于 `2026-07-24T15:12:32Z` 冻结并启动。它只跟随该时间之后的 observer 到达盘口记录，额外规则只有 `intended ask >= 0.60`；这是待验证的事后假设，不是策略升级。
- `favorite-price-holdout-v1` 在 6 笔官方结算后净亏 `$6.512921`，已触发自身 `challenger_portfolio_daily_loss_gate` 并永久 `HALTED`；不得解除或重置。它未达到 500 笔/336 小时门槛。
- `calibrated-probability-holdout-v1` 冻结于 `2026-07-31T13:03:00Z`，使用冻结前 8114 笔官方结算的四个固定 reliability bins，把原始 selected probability 校准后按同一费用/滑点缓冲重算净 edge；覆盖外或校准后 edge `<4pp` 一律 fail-closed。它在 9 笔官方结算、2 胜时净亏 `-$12.429296`、ROI `-44.8784%`、Brier `0.338608`，已于 `2026-08-01T02:30:41Z` 触发自身 `challenger_portfolio_daily_loss_gate` 并永久 `HALTED`；CI `[-$3.676451,+$0.914386]` 跨 0 不能推翻事故锁，更不得解除、重置或回改冻结桶。它未达到 500 笔/336 小时门槛。
- 2026-07-28 08:22 JST 的完整 240 样本快照中，可控路径 P95 为 `455.21–460.91ms`：HTTP P95 `155.60–159.16ms`、event-loop P95 `299.50–301.66ms`、decision P95 `0.07–0.12ms`。相对上一完整同窗，恶化主要来自 event-loop；三服务 PID/restart count 未变，随后 10 秒 aggregate steal 为 `41.69%`，单点和不同时窗不能支持调度因果或付费扩容。
- 2026-07-28 14:24 JST 的另一完整 240 样本快照在同 PID、零重启、零代码变更下回落到 `83.07–88.65ms`：HTTP P95 `81.29–86.96ms`、event-loop P95 `1.62–1.73ms`、decision P95 `0.06–0.08ms`；随后一分钟 12 次滑动快照持续 `82.77–88.86ms`。同步末端 10 秒 steal/主进程单核 CPU 为 `0.05%/4.80%`。现有套餐实证可达目标，但同日 `455–461ms` 与 `83–89ms` 的巨大跨时段波动说明尚未稳定；短资源窗只能记录同相，不能证明唯一因果或支持付费扩容。
- 2026-07-28 20:29 JST 已捕获同 PID、零重启、零部署下的反向转换：完整路径先回到 `123.99–198.19ms`，20 秒高压同窗达到 `415.36–551.14ms`，随后 10 次快照虽恢复 4/4 online，路径仍为 `193.80–316.87ms`，最终为 `161.97–234.61ms`。高压窗 main 单核 CPU/aggregate steal/iowait 为 `72.35%/50.12%/2.90%`，event-loop P95 `319.69–394.67ms`，后端 freshness 曾短时降至 3/4、再到 2/4；短资源窗与 20 分钟滑动指标不等尺度，只支持继续配对观测，不能证明唯一因果或支持付费扩容。
- 2026-07-30 06:02 JST 完成同 PID、零重启、零部署的 240 样本五秒 resource/API 配对窗。四资产路径滑动 P95 的跨时点 P95 为 `848.38–862.69ms`，240/240 个快照全路高于 100ms；aggregate steal P50/P95 `64.25%/84.49%`、main 单核 CPU P50/P95 `90.00%/104.98%`。路径与 steal 的 5 秒相关仅 `0.202`，60 秒平滑相关 `0.631`，同窗多次出现反向变化；46/240 个快照 `ready=false`、最差 0/4 online。高调度压力与 freshness 失败的共现比其与滑动路径 P95 的关系更一致，但仍不能证明唯一因果或支持付费扩容。
- 2026-07-30 11:43 JST 的后续只读快照在同 PID、零重启下恢复 4/4 online，但完整 240 样本可控路径仍为 `379.80–381.27ms`：HTTP P95 `158.81–160.65ms`、event-loop P95 `220.54–221.95ms`、decision P95 `0.06–0.10ms`。同步 10 秒 aggregate steal/main 单核 CPU/iowait 为 `64.75%/95.06%/5.88%`；11:46 JST 的无资源配对终点又自然回落到 `301.43–303.54ms`。这些变化不改变上一完整配对窗关于弱瞬时相关和非唯一因果的结论。
- 2026-07-30 17:45 JST 的又一同 PID、零重启只读快照先为 4/4 online，完整 240 样本可控路径 `294.59–296.16ms`：HTTP P95 `159.55–160.70ms`、event-loop P95 `134.96–135.40ms`、decision P95 `0.08–0.14ms`。同步 10 秒 aggregate steal/main 单核 CPU/iowait 为 `63.17%/94.50%/1.71%`。17:48 JST 又短暂出现一次 3/4 strict freshness 失败（最大 state age `3.121s`），随后 10/10 跟踪快照自然恢复 4/4，但路径升到 `304.82–376.82ms`、终点 `335.34–338.94ms`。累计 reconnect 增至 ETH RTDS 2、SOL market 4、XRP market 1，三服务 warning 日志仍为 0；这支持继续观察高压力与 freshness 共现，不支持放宽 freshness、改重连或作唯一因果结论。
- 2026-07-30 23:45 JST 的同 PID、零重启只读复查先为 4/4 online、完整路径 `380.44–381.80ms`，同步 10 秒 aggregate steal/main 单核 CPU/iowait 为 `69.42%/107.82%/1.67%`。随后 strict freshness 连续降至 3/4、3/4、2/4（最大 state age `4.957s`），再无重启恢复 4/4；跟踪路径最高 `632.72ms`，23:46 JST 为 `540.52–548.38ms`，23:49 JST 又自然回落到 `383.07–390.37ms`。三服务 PID/restart count 和 warning 日志不变，reconnect 也未增加。重复的高压力/freshness 共现值得继续记录，但 10 秒资源窗与 240 样本滑动路径仍不等尺度，不证明唯一因果，也不支持付费扩容。
- 2026-07-31 05:45 JST 的同 PID、零重启只读复查仍为 4/4 online。初始完整路径 `393.47–418.13ms`，其中 HTTP P95 `135.33–159.95ms`、event-loop P95 `257.94–258.05ms`、decision P95 `0.09–0.18ms`；同步 10 秒末端路径自然降至 `300.19–319.13ms`，aggregate steal/main 单核 CPU/iowait 为 `58.29%/89.80%/1.20%`。随后 8/8 跟踪均保持 4/4 online，但路径在 `298.66–515.70ms` 间快速变化；05:49 JST 最终又升到 `461.77–480.82ms`，不能称为稳定恢复。三服务 PID/restart count 仍为 `170952/113705/172845` 和 0，最近各 500 行 warning 均为 0；本轮第一次 SSH 配对尝试超时且未产生数据，第二次成功。
- 2026-07-31 11:48 JST 的同 PID、零重启只读复查仍为 4/4 online。初始完整路径已自然回落到 `299.87–302.27ms`：HTTP P95 `159.16–161.49ms`、event-loop P95 `139.07–140.91ms`、decision P95 `0.08–0.11ms`。同步 10 秒 aggregate steal/main 单核 CPU/iowait 为 `60.18%/94.20%/1.65%`；随后路径先升到 `458.64–478.77ms`，8 次跟踪又回到 `302.53–382.55ms` 并始终 4/4 online。三服务 PID/restart count 与 warning 计数不变；相同服务状态下的分钟级反向换挡再次说明单个 10 秒资源点不能解释 240 样本滑动路径。
- 2026-07-31 17:52 JST 的同 PID、零重启只读复查先捕获 `ready=false`、3/4 online，XRP state age `3.201s`；四路完整路径 `718.30–721.21ms`，HTTP P95 `156.04–158.91ms`、event-loop P95 `562.20–562.21ms`、decision P95 `0.05–0.10ms`，event-loop max `7583.15ms`。随后无需重启恢复 4/4，10/10 跟踪路径仍在 `278.05–397.95ms`，08:52 为 `376.74–378.23ms`，08:57 又自然降至 `301.24–304.07ms`。邻近 10 秒资源窗 aggregate steal/main 单核 CPU/iowait 为 `60.18%/95.05%/1.26%`；三服务 PID/restart count 与 warning 计数不变。累计 reconnect 增至 BTC `1/0`、ETH `3/1`、SOL `2/5`、XRP `2/2`，但自动恢复和空 warning 不支持立即修改 freshness 或重连。
- 2026-07-31 23:55 JST 的只读复查初始为 4/4 online、完整路径 `453.82–490.24ms`；同步 10.351 秒窗 aggregate steal/main 单核 CPU/iowait 为 `64.82%/108.50%/3.14%`，窗后路径升至 `624.14–666.22ms`。随后 8 次跟踪有一次短暂 3/4 strict freshness 失败（最大 state age `3.231s`）并无重启恢复，路径一度 `538.85–546.03ms`，最终健康核对又自然降至 `240.77–243.83ms`。三服务 PID/restart count 继续为 `170952/113705/172845` 和 0，最近各 500 行告警为 0，reconnect 保持 BTC `1/0`、ETH `3/1`、SOL `2/5`、XRP `2/2`。这再次记录高调度压力、event-loop 尾部和 freshness 失败共现，但短资源窗与滑动 P95 不等尺度，不能证明唯一因果或支持付费扩容。
- 2026-08-01 06:14 JST 本轮先捕获 `ready=false`、3/4 online，ETH state age `3.514s`，四路完整路径 `461.29–481.65ms`；邻近 8 秒资源窗 aggregate steal/main 单核 CPU/iowait 为 `59.19%/85.87%/1.12%`。修复并重启 challenger 后的全历史幂等核对耗时约 8 分 51 秒，最终 API 恢复 4/4，但该压力仍在 240 样本窗口内，路径先为 `581.95–661.18ms`、06:14 又升至 `643.11–744.29ms`。这不是改善证据；也不得通过等待窗口清空、缩短窗口或重启主进程来隐藏该段。
- 2026-08-01 11:53 JST 通过既有 SSH 隧道完成只读 API 复查，最终为 `ready=true`、4/4 online，四路完整 240 样本可控路径 `320.76–328.68ms`：HTTP P95 `159.04–166.89ms`、event-loop P95 `161.62–161.63ms`、decision P95 `0.10–0.17ms`。此前 10 次短跟踪中有一次真实 3/4 strict freshness 失败（最大 state age `3.119s`），随后无重启恢复，路径在 `314.14–459.14ms` 间换挡。四次新的 SSH 握手均在执行命令前超时，既有隧道仍健康，因此本轮没有刷新 steal/iowait、内存/swap、systemd/日志、calibrated 独立状态或直读 SQL，也没有冒险中断隧道、重启或部署；缺失数据不得用旧值冒充。
- 2026-08-01 18:22 JST 在两次 0 pending/0 position 且 champion+observer 全 WAIT 的安全检查后，只重启主服务一次加载已部署的 `clob_http_timing`/TTFB buckets；新 PID `239611`。四路各满 240 个全新样本后，可控路径 `383.13–385.38ms`：HTTP P95 `158.90–161.16ms`、event-loop P95 `223.66–224.13ms`、decision P95 `0.05–0.10ms`。基础 TTFB P50 约 `78ms`，但每路 29–48/240 个样本落在 `100–500ms`，使 TTFB P95 为 `158.87–161.13ms`；body P95 均 `0.03ms`。172 次跟踪有 6 次 `ready=false`、最差短暂 0/4 online、最大 state age `4.752s`，路径范围 `215.69–550.31ms`，随后均无二次重启恢复。诊断已加载，但没有性能改善证据。该重启发生在同一工作期间并发提交的冻结期禁令可见之前；发现新禁令后已立即停止后续 latency/sidecar 工作。
- observer 在 8403 笔官方结算的只读 SQL 快照中，费用、滑点和延迟后净 P&L `-$2,207.053022`、ROI `-5.5500%`、Brier `0.239068`，每笔 P&L 95% CI `[-$0.344521,-$0.180781]`。相对 8179 笔快照新增 224 笔并回升 `$13.084462`，但平均 selected probability 仍比实际 accuracy 高 `11.5815pp`，所有标准 intended-ask、方向、selected-probability 和 latency 桶的 ROI 点估计仍为负；短期回升没有改变累计负期望或推广结论。
- 2026-08-01 11:53 JST 的聚合 API observer 已到 8579 settled / 5103 wins，净 P&L `-$2,151.663145`、fees `$1,059.025754`、staked `$40,555.717545`、accuracy `59.4825%`、ROI `-5.3054%`。相对上次 8403-settled 直读 SQL 新增 176 笔且累计 P&L 回升 `$55.389877`，但累计值仍深亏；由于本轮 SSH 阻塞，Brier、校准 gap、CI 与标准分桶没有重新直读，最近可验证的完整统计仍是上一条 8403-settled 快照，不能把短期回升解释为正期望。
- 2026-08-01 18:22 JST 的直读 observer SQL 为 8713 settled / 5177 wins，净 P&L `-$2,097.058871`、fees `$1,078.462720`、slippage `$66.202419`、ROI `-5.0960%`、Brier `0.238659`、calibration gap `-11.4551pp`，每笔 P&L 95% CI `[-$0.321605,-$0.159758]`。`<0.40` intended-ask 桶虽在 63 笔时点 ROI `+10.6028%`，CI `[-$1.379122,+$2.271335]` 跨 0；其余价格桶、两方向、四概率桶及所有 latency 桶点 ROI 均为负。最终 API 到 8717 settled、P&L `-$2,113.829590`；短期波动不改变累计负期望。
- 2026-08-01 校准 challenger 部署后暴露出一个明确的 fail-closed 状态写入故障：PyYAML 将未加引号的 `training_cutoff_utc` 解析成 `datetime`，导致每 10 秒写 JSON 时抛出 `TypeError`，状态文件缺失。现已只把配置时间戳规范化为 UTC 字符串，不改冻结点、校准桶、edge、风控或账本；45 项本地 Fast tests、前端 validator、13 模块静态编译、冻结证据检查、远端精准测试与静态编译均通过。部署后状态持续写入且新进程错误计数为 0。
- 去除 champion SQLite 的无变化 window/account 刷新后，四库修改频率短窗约降 `87.5%`、主进程单核 CPU 约降 `20.2%`，但 process write_bytes 仅降约 `10.7%`；四份每秒原子状态 JSON 及文件系统元数据放大成为主要剩余写入。
- 同盘受控基线进一步修正了写入口径：三次各 20 次原子替换的 gross `write_bytes / payload` 均约 `1.089×`，不是主进程同窗的 `5.45×`；主进程 `write_bytes=5,922,816B` 同时有 `cancelled_write_bytes=4,866,048B`，净记账 `1,056,768B` 约为状态载荷 `1,086,735B` 的 `0.97×`。不得把 gross `write_bytes` 单独称为物理写入，也没有证据支持为此迁移 tmpfs 或放弃原子读取语义。
- 2026-07-26 两段低开销配对窗口中，main gross 写入约 `81–83%` 被 `cancelled_write_bytes` 抵消；取消比例在 event-loop P95 约 `59–62ms` 和约 `306–324ms` 的窗口相近，安静段写入速率反而更高。observer SQLite、decision JSONL、review bundle 均未变化；当前证据不支持把 gross 或取消写记账当成尾延迟根因。
- 2026-07-26 已让 `run_due_reviews` 仅在产生新复盘或 bundle 缺失时写文件；不改变 1 秒运行状态 freshness。远端完整 60 秒复盘周期验证 0 次无效重写。
- 2026-07-26 的 15 分钟、166 样本同窗覆盖三个自然五分钟边界：前两次 event-loop/path 峰值主要在切换前，第三次在切换后，market activation 不是一致充分解释。event-loop P95 与五秒 CPU steal 的探索性相关约 `0.516`，但时间尺度不匹配，不能作因果结论。
- 2026-07-27 的双栈只读 sidecar 各使用四条独立 keep-alive 连接、每条 24 次请求：IPv4 96/96 成功且四路 warm P95 为 `80.52–81.34ms`；IPv6 96/96 成功，三路为 `80.48–80.75ms`、一路因单个 `157.47ms` 尖峰为 `98.18ms`。响应体读取 P95 均不超过约 `0.18ms`，延迟几乎全在 TTFB；首次 DNS/TCP/TLS 的墙钟阶段受并发调度影响，总建连为 `160.95–480.08ms`。主进程现有 CLOB/TLS socket 实际走 IPv6，短窗没有复现或解释 240 样本主窗口的资产分组，不能据此强制 IPv4 或改指标口径。
- 2026-07-27 的完整 20 分钟只读 sidecar 完全匹配 IPv6 `/time`、四连接、5 秒 cadence 和每路 240 样本：960/960 成功、0 重连，连接年龄达到约 1201 秒；四路 warm P95 为 `80.68–81.43ms`，没有随连接年龄单调退化。ETH 有两次约 `2079ms` 的稀疏 TTFB 尖峰，但不足 5%，未抬高 P95；同窗主引擎 HTTP P95 多数约 `157–159ms`。连接老化、重建和响应体读取不能解释这个更高频差异；下一步必须在保持现有 P95 原值的同时，分开观测主探针提交到 worker 的排队等待与 socket TTFB。
- 2026-07-27 已在不改变原 `clob_http` 口径的前提下完成四路各 240 样本的主探针拆分：原 HTTP 计时从 worker 内开始，executor queue 从未混入；queue-wait P95 `76.82–78.63ms`，worker 内 socket TTFB P95 `95.60–157.57ms`，body-read P95 全部 `0.02ms`。共享 executor 确有额外尾部等待，但它不解释原 HTTP 与 sidecar 的差异，也不能从既定路径 P95 中直接相减。
- 2026-07-27 修正 dashboard freshness 假告警：后端 3 秒 engine freshness 保持不变；`ENGINE STALE` 只信任后端 `bot.online`，缓存/隧道造成的聚合延迟改用 `generated_at` 单列为 `DASHBOARD DELAY`。旧逻辑在 30 次采样中误报 11 次且每次仍为 4/4 online；部署后浏览器连续 15 次观察为 0 次误报。
- 2026-07-27 的主探针/四连接 IPv6 sidecar/VPS 调度同窗各含 240 样本：sidecar 960/960 成功、0 重连，TTFB P50/P95 为约 `78/82ms`；主探针 HTTP P50 同为约 `78ms`，但约 `158ms` 次级模式在主侧进入 P95、在 sidecar 只到 P99。主侧差值中位数在 steal 低/高四分位均约 `79–81ms`，没有随 aggregate steal 同涨同落；基础路径相同，差别在次级模式出现频率，当前证据不支持改 executor、强制 IPv4 或付费扩容。
- 2026-07-28 本地工作区存在归属不明的 `fastbot.py` / `tests/test_fastbot.py` 未提交改动，内容是上一轮计划的主探针提交/完成单调时间与 240 样本 TTFB 分桶诊断。项目 `.venv` 已确认包含 `aiohttp` / `PyYAML`，当前精确工作区的 41 个 Fast tests、前端 validator、13 模块静态编译和 diff check 全部通过；测试阻塞已消除。2026-08-01 复核判定该改动为此前轮次按计划自产的诊断代码，已随 `95ad1f7` 提交并于同日部署，归属阻塞解除。

## 运行位置与访问（已停机，仅供恢复参考）

- VPS：`<vps-ip>`
- 远端目录：`/home/ubuntu/polyvote`
- SSH 用户：`ubuntu`
- 本机密钥路径：`C:\Users\<user>\.ssh\<ssh-key>.pem`
- 本地仪表盘：http://127.0.0.1:8920/
- 本地自动重连隧道：`deploy/keep-dashboard-tunnel.ps1`
- Windows 当前用户登录自启动项：`PolyvoteDashboardTunnel`

远端服务：

```bash
systemctl --user status polyvote-fast.service
systemctl --user status polyvote-fast-challenger.service
systemctl --user status polyvote-fast-dashboard.service
journalctl --user -u polyvote-fast.service -n 100 --no-pager
journalctl --user -u polyvote-fast-challenger.service -n 100 --no-pager
```

## 当前运行快照

快照时间：2026-08-01 09:18 UTC

- API `ready: true`
- 资产执行引擎：4/4 在线（BTC、ETH、SOL、XRP）
- 逻辑信号通道：16（每资产 4 条）
- observer fills / settled：本轮直接 SQL 快照 `8713 / 8713`
- observer normalized P&L：直接 SQL 快照 `-$2,097.06`
- observer accuracy：约 `59.42%`
- champion equity：`$7.790189`
- champion halt：`portfolio_incident_latched`
- finite-cash challenger：`favorite-price-holdout-v1`
- challenger 初始资金：`$30`
- challenger fills / settled：`6 / 6`
- challenger equity：`$23.487079`
- challenger halt：`challenger_portfolio_daily_loss_gate`
- calibrated challenger：`calibrated-probability-holdout-v1`，永久 `HALTED`
- calibrated fills / settled / P&L：`9 / 9 / -$12.429296`，事故锁 `challenger_portfolio_daily_loss_gate`

这些统计会继续变化。下次以 `/api/fast/state` 的实时值为准。

## 当前架构

每个资产只有一个共享行情/盘口引擎，避免复制 WebSocket；每个引擎派生四条真实数据驱动的逻辑通道：

1. `EDGE`：手续费、滑点后的模型 edge；已连接独立 observer 模拟账本。
2. `MOM`：估算结算价相对五分钟窗口开盘价的动量。
3. `BASIS`：Chainlink 与 Binance 之间的基差。
4. `MICRO`：模型 fair UP 概率与 Polymarket CLOB 中点的偏差。

只有 `EDGE` 目前记录独立模拟成交。其余 12 条是只读诊断通道，不得称为已验证盈利策略。

真实已接数据：

- Polymarket CLOB WebSocket、本地盘口校验与 REST 到达盘口
- Polymarket RTDS Binance 价格
- Polymarket RTDS Chainlink 价格
- Gamma 官方市场发现与官方结算
- HTTP p50/p95、决策耗时、event-loop lag、估算 ACK floor

未接数据，前端必须保持 `NO FEED`，不得伪造：

- Futures CVD / 主动买卖量
- 跨交易所清算流
- Open interest / funding

## 单屏前端

- 文件：`fast_dashboard.html`
- 聚合 API：`fastdash_server.py`
- 固定 `100vh`，整页不滚动。
- 当前 654×731 应用内视口实测：document height = viewport height，`pageScrolls=false`。
- 7 个面板全部使用格内滚动。
- 宽屏为 12 列密集布局；窄窗口为 2 列 × 4 行。
- 全屏 offline 遮罩已删除，只保留不阻断鼠标的顶部窄条。

## 关键文件

- `fastmulti.py`：四资产监督器与组合风控
- `fastbot.py`：行情、盘口、策略、模拟执行与结算主循环
- `fast_portfolio.yaml`：四资产 `$30` 配置和 observer 参数
- `fastchallenger.py`：读取 observer 到达盘口结果的独立有限现金 follower
- `fast_challenger.yaml`：冻结时间、单一实验变量、四资产资金与推广门槛
- `polyvote/fastobserver.py`：零资金独立观察账本
- `polyvote/fastreview.py`：小时/日/周复盘
- `fastdash_server.py`：组合聚合、16 条逻辑通道与只读 API
- `fast_dashboard.html`：单屏作战台
- `tests/test_fastdash_server.py`：策略矩阵与聚合测试
- `tests/validate_fast_dashboard.js`：前端语法、ID、非全屏告警检查

## 已验证测试

- 本机 Fast 测试：35 项通过
- VPS challenger/dashboard/ledger/portfolio/observer 子集：14 项通过
- `tests/validate_fast_dashboard.js`：通过
- `fastdash_server.py`：`py_compile` 通过
- 本地全套 fast 测试中 29 项通过，2 个测试模块因本机 bundled Python 缺少 `aiohttp` / `PyYAML` 无法导入；相关代码未失败，VPS 完整 venv 可正常启动。
- 远端 dashboard API 已验证返回 `execution_engine_count=4`、`strategy_lane_count=16`。
- 浏览器控制台无 warning/error。
- 2026-07-25 将已锁定 challenger 的轮询从 2 秒退避到 10 秒；COLLECTING 仍为 2 秒。稳态 30 秒采样中 challenger 单核 CPU 约从 `7.45%` 降至 `3.13%`，写入速率约降 `70.7%`；rowid 增量读取仍会补齐全部 observer 记录。
- 2026-07-25 进一步让 `FastLedger` 的重复 window/account 刷新在值未变化时成为 SQLite no-op；本地 39 项 Fast 测试、前端 validator 和静态编译通过，VPS 39 项 Fast 测试与静态编译通过。主引擎在 0 pending / 0 position / observer WAIT 安全窗口重启，4/4 在线；challenger 未重启。
- 2026-07-25 20:20 JST 只读归因显示四份 1 秒状态 JSON 的逻辑载荷仅解释主进程约 `17–19%` 的写入；本轮没有足够证据支持代码或运维改动。本地 39 项 Fast 测试、前端 validator 与静态编译通过；未部署、未重启。
- 2026-07-26 修正写入归因口径，并停止无新复盘时每分钟重写四份约 160KB bundle。本地与 VPS 各 39 项 Fast 测试、前端 validator（本地）和静态编译通过；只用 `deploy/push.sh` 部署，主引擎在 0 pending / 0 position / champion+observer WAIT 安全窗口重启，challenger 未重启。部署后 70.16 秒验证复盘 mtime 0 次变化、4/4 在线。
- 2026-07-26 以同一文件系统、相同四文件大小和 1 秒 cadence 做三次受控原子替换基线；60 次替换的 gross 放大稳定为约 `1.089×`，临时目录全部删除。证据否定“状态原子替换本身造成约 5.45× 物理放大”；本地 39 项 Fast 测试、前端 validator、静态编译和 diff check 通过，本轮未改代码、未部署、未重启。
- 2026-07-26 以两段高低 event-loop P95 的 `/proc` 同窗对照继续拆分取消写：gross 中约 `81–83%` 被取消，gross-cancelled 仅约为 `wchar` 的 `88–92%`；安静段写速率高于切换尖峰段，observer SQLite、decision JSONL、review bundle 均未改写。本地 39 项 Fast 测试、前端 validator、静态编译和 diff check 通过；没有证据支持代码或运维改动，未部署、未重启。
- 2026-07-26 以 166 个五秒样本覆盖三个自然五分钟边界；切换后尖峰不一致，不能把 market activation 当作充分根因。HTTP P95 出现 `~81–85ms` 与 `~157–158ms` 分组；本地 39 项 Fast 测试、前端 validator 和 13 个 Fast 模块静态编译通过，本轮未改运行代码、未部署、未重启。
- 2026-07-27 以四路 IPv6 `/time` keep-alive 完成 20 分钟、每路 240 样本的只读长窗；960/960 成功、0 重连，连接年龄桶没有单调恶化。sidecar P95 约 `81ms`，同窗主探针约 `158ms`，尚未证明差异来自默认线程池排队，因此本轮不改代码、路由、服务或指标。
- 2026-07-27 增加主探针 queue/TTFB/body 旁路诊断并部署，原 HTTP 指标不变；四路满 240 样本后 queue P95 约 `77ms`、TTFB P95 `96–158ms`、body P95 `0.02ms`。本地与 VPS 各 40 项 Fast tests、13 模块静态编译及本地前端 validator 通过；主服务仅在两次 0 pending/position 且 champion+observer WAIT 后重启，challenger 未重启。
- 2026-07-27 将 engine stale、dashboard snapshot delay 和 request failure 拆成三个真实告警；本地/VPS 各 40 项 Fast tests、静态编译及本地前端 validator 通过。只重启 dashboard，主引擎和 challenger 未重启；实际浏览器 15 个两秒样本均 4/4 online、0 假告警、console 干净。
- 2026-07-27 完成主探针、四连接 IPv6 sidecar 与 aggregate steal 的 20 分钟同窗：960/960 sidecar 和 240/240 API 样本成功，三服务 PID/restart count 不变。主/sidecar warm P50 相同但约 158ms 次级模式频率不同，且差值不随 steal 四分位扩大；本地 40 项 Fast tests、前端 validator 和 13 模块静态编译通过，本轮未改运行代码、未部署、未重启。
- 2026-07-28 只读快照中路径 P95 升至 `455.21–460.91ms`，其中 event-loop P95 为 `299.50–301.66ms`；同一短资源窗 steal/iowait 为 `41.69%/1.12%`，主进程单核 CPU `55.90%`。observer 扩大到 4770 settled、净 P&L `-$1,191.688740`，但 ROI 从 `-5.4921%` 略回升到 `-5.2830%`；累计净值和 CI 仍显著为负。本轮因两份归属不明的未提交诊断改动而只记录、不部署、不重启。
- 2026-07-28 14:24 JST 的后续完整窗口在服务 PID/restart count 不变时回落为 `83.07–88.65ms`，一分钟 12 次抽样持续低于 `88.86ms`；末端 10 秒 steal/iowait `0.05%/0.30%`、主进程单核 CPU `4.80%`。这证明现有机器可达到 `<100ms`，但不能抹掉同日早些时候 `455–461ms` 的完整窗口或称为稳定达标。observer 扩大到 5009 settled、净 P&L `-$1,237.322287`、ROI `-5.2208%`，仍不支持推广。当前工作区 41 项 Fast tests、前端 validator、13 模块静态编译和 diff check 通过；因运行代码归属不明仍未提交、部署或重启。
- 2026-07-28 20:29 JST 的只读观测捕获完整窗口从 `<100ms` 回到 `123.99–198.19ms`，随后高压窗短时达到 `415.36–551.14ms` 并触发真实后端 freshness 瞬时降级；10 次跟踪恢复 4/4 online 但路径仍高于目标，最终为 `161.97–234.61ms`。同窗 20 秒 steal `50.12%`、main 单核 CPU `72.35%`，只证明同相不证明唯一因果。observer 直连扩大到 5250 settled、净 P&L `-$1,326.269177`、ROI `-5.3368%`，仍不支持推广；未改代码、未部署、未重启。
- 2026-07-30 完成 20 分钟、240 样本 resource/API 配对窗：路径全程不达标，46 次 strict freshness 失败，服务 PID/restart count 不变；5 秒路径/steal 相关弱、60 秒相关中等，且有多段反向变化，不能把 steal 当作唯一解释。observer 直连扩大到 6580 settled、净 P&L `-$1,870.361720`、ROI `-5.9984%`。本地 41 项 Fast tests、前端 validator、13 模块静态编译和 diff check 通过；工作区归属阻塞仍在，未提交、部署或重启。
- 2026-07-30 11:43–11:46 JST 只读复查：4/4 online、同 PID/零重启，完整路径从 `379.80–381.27ms` 自然回落到 `301.43–303.54ms`，配对 10 秒 steal/main CPU `64.75%/95.06%`；observer 直连扩大到 6819 settled、净 P&L `-$1,947.371284`、ROI `-6.0252%`。timing/bucket 本地改动归属仍不明确，本轮继续不部署、不重启、不改指标或风控。
- 2026-07-30 17:39–17:49 JST 只读复查：同 PID/零重启，完整路径先为 `281.73–296.16ms`，配对 10 秒 steal/main CPU `63.17%/94.50%`；随后一次 3/4 freshness 失败立即自然恢复，10 次跟踪终点路径为 `335.34–338.94ms`。observer 直连扩大到 7063 settled、净 P&L `-$1,795.741693`、ROI `-5.3644%`，每笔 P&L CI 仍完全为负。当前精确工作区 41 项 Fast tests、前端 validator、13 模块静态编译和 diff check 通过；timing/bucket 本地改动归属仍不明确，继续不提交、不部署、不重启。
- 2026-07-30 23:40–23:49 JST 只读复查：相同 PID/零重启下，完整路径先为 `380.44–381.80ms`，同步 10 秒 steal/main CPU `69.42%/107.82%`；随后 strict freshness 连续降至 3/4、3/4、2/4，再自然恢复 4/4，跟踪路径最高 `632.72ms`，最终又自然回落到 `383.07–390.37ms`。observer 直连扩大到 7283 settled、净 P&L `-$1,812.145703`、ROI `-5.2524%`，每笔 P&L CI 仍完全为负。timing/bucket 本地改动归属仍不明确，本轮继续不提交、不部署、不重启。
- 2026-07-31 05:40–05:49 JST 只读复查：相同 PID/零重启下，初始完整路径 `393.47–418.13ms`，同步 10 秒资源窗末端自然降至 `300.19–319.13ms`，steal/main CPU 为 `58.29%/89.80%`；8 次后续均 4/4 online，但路径在 `298.66–515.70ms` 波动，最终为 `461.77–480.82ms`。observer 直连扩大到 7511 settled、净 P&L `-$1,974.788723`、ROI `-5.5524%`，所有标准分桶 ROI 点估计仍为负。精确工作区 41 项 Fast tests、前端 validator 和 13 模块静态编译通过；timing/bucket 本地改动归属仍不明确，继续不提交、不部署、不重启。
- 2026-07-31 11:44–11:48 JST 只读复查：相同 PID/零重启下，初始完整路径自然回落到 `299.87–302.27ms`；同步 10 秒 steal/main CPU 为 `60.18%/94.20%`，随后路径升到 `458.64–478.77ms`，8 次跟踪再回到 `302.53–382.55ms`，全程 4/4 online。observer 直连扩大到 7736 settled、净 P&L `-$2,066.082716`、ROI `-5.6413%`，所有标准分桶 ROI 点估计仍为负。精确工作区 41 项 Fast tests、前端 validator 和 13 模块静态编译通过；timing/bucket 本地改动归属仍不明确，继续不提交、不部署、不重启。
- 2026-07-31 17:43–17:57 JST 只读复查：相同 PID/零重启下先从 4/4 降到 3/4 freshness，完整路径升至 `718.30–721.21ms`，随后自然恢复 4/4；10 次跟踪路径 `278.05–397.95ms`，最终健康核对 `301.24–304.07ms`。邻近 10 秒 steal/main CPU 为 `60.18%/95.05%`，累计 reconnect 有新增但 warning 仍为 0。observer 直连扩大到 7962 settled、净 P&L `-$2,156.343629`、ROI `-5.7215%`、整体 calibration gap `-11.6658pp`，所有标准分桶 ROI 点估计仍为负。精确工作区 41 项 Fast tests、前端 validator 和 13 模块静态编译通过；timing/bucket 本地改动归属仍不明确，继续不提交、不部署、不重启。
- 2026-07-31 23:44–23:55 JST 只读复查：相同 PID/零重启下初始完整路径 `453.82–490.24ms`，同步 10.351 秒 steal/main CPU 为 `64.82%/108.50%`，窗后升至 `624.14–666.22ms`；8 次跟踪短暂出现一次 3/4 freshness 失败后自然恢复，路径一度 `538.85–546.03ms`，最终又自然降至 `240.77–243.83ms`。observer 直连扩大到 8179 settled、净 P&L `-$2,220.137485`、ROI `-5.7354%`、整体 calibration gap `-11.6640pp`，所有标准分桶 ROI 点估计仍为负。精确工作区 44 项 Fast tests、前端 validator、13 模块静态编译通过；工作区新增本地 calibrated challenger 整组未提交改动且旧 timing/bucket 归属仍未明确，本轮继续不提交、不部署、不重启。
- 2026-08-01 05:45–06:08 JST 发现用户/Fable 已提交并部署 timing 与 calibrated challenger。校准状态因 YAML timestamp 被解析为 `datetime` 而每 10 秒 fail-closed；只规范化时间戳字符串并部署后，favorite 历史核对约 6 分钟、两配置总回放约 8 分 51 秒，随后校准状态持续写入、错误为 0、challenger 短窗 CPU 回到 0%。本地 45 项 Fast tests、前端 validator、13 模块静态编译、冻结证据和远端精准验证通过。主 PID 未变，回放压力仍留在完整 P95 窗内；不据此声称延迟改善。

## 重要口径

- `FILLED` 是模拟 FAK 到达盘口后可成交，不是 Polymarket 真实成交。
- `OBSERVER P&L` 是所有归一化假设订单的累计盈亏，不是账户余额，也不能和 champion equity 相加。
- `ARRIVAL AVG` 是历史累计信号到模拟到达盘口延迟。
- `BEST ACK FLOOR` 是当前四引擎中最好的估算值，没有真实订单 ACK 证明。
- `RISK HALT` 仅表示冠军有限资金账本停止；行情采集和 observer 仍继续。
- challenger 重启会从进程内 rowid 游标 0 做一次幂等历史核对，产生短时 CPU/写入峰值；评价退避效果必须排除这个一次性回放窗口。

## 下一步优先级

1. 保持 `favorite-price-holdout-v1` 参数与永久事故锁不变；它在 6 笔结算时失败，不能为凑 500 笔/336 小时而重置或解除。
2. 保持 `calibrated-probability-holdout-v1` 四个校准桶、13:03 UTC 冻结点、独立账本和永久事故锁不变；它在 9 笔结算时失败。冻结期内不另立实验、不回看调仓；任何未来假设必须等待用户在既定评估点决定是否收摊或另开全新的自然时间 holdout。
3. 把四条策略通道分别建立独立账本；在此之前不要把 16 lanes 描述成 16 个已交易 bot。
4. 冻结期纪律（用户 2026-08-01 指示，有效至 `2026-08-14T13:03:00Z`）：停止延迟/steal/TTFB/sidecar 研究轮次，不再为诊断重启主进程。每天最多一次健康检查：三服务 active、4/4 online、champion/favorite/calibrated 三个事故锁文件未变，且状态持续 fail-closed；出现新的运行异常才介入修复。不改参数、不回看调仓、不做新实验、不改 executor、不强制 IPv4、不购买机器。calibrated 已在 9 笔时永久 HALTED，无法达到 ≥500 笔；既定评估点只记录失败/样本不足并由用户决定收摊评审。每轮收尾把本轮自有改动提交进 git。保持主进程、状态 writer、1 秒 freshness、原子读取语义和现有指标不变；sidecar 同相/错相对照留待冻结期后且仅在用户重新授权时再议。
5. 若接入 CVD/清算，使用一个共享 Binance Futures collector，再广播给四资产引擎；不要每个策略重复开连接。

## 安全边界

- 不添加或上传真实钱包私钥。
- 不把 dashboard 直接监听到公网；继续通过 SSH 隧道访问。
- 不自动解除冠军事故锁。
- 不把宣传截图中的收益、胜率或复杂图形当成策略证据。
