# Fast Market 工作记忆

最后更新：2026-07-27 02:30 JST

这份文件是当前快市场系统的唯一快速交接入口。旧 `HANDOFF.md` 主要描述已退休的慢市场，而且存在编码损坏；继续工作时先读本文件。

## 当前结论

- 系统正在 AWS Lightsail VPS 上持续运行，关闭 Codex 或浏览器不会停止 VPS 服务。
- 当前完全是 **shadow / 模拟盘**，没有钱包私钥，没有真实下单。
- 慢市场已退休，不再开发或展示。
- 原严格资金冠军账本从 `$30.00` 亏至 `$7.790189`，已被 `portfolio_incident_latched` 永久风控锁定，不再产生订单。
- 独立 observer 使用归一化 `$5` 假设订单持续收集样本，不占用也不扣减冠军账本；它是研究统计器，不是严格资金模拟账户。
- observer 的早期正收益已被大样本证伪：从 217 笔时的 `+$27.69961` 变成约 2239 笔时的 `-$341.90`。高胜率没有转化为正期望。
- 独立 `$30` 有限现金实验 `favorite-price-holdout-v1` 已于 `2026-07-24T15:12:32Z` 冻结并启动。它只跟随该时间之后的 observer 到达盘口记录，额外规则只有 `intended ask >= 0.60`；这是待验证的事后假设，不是策略升级。
- `favorite-price-holdout-v1` 在 6 笔官方结算后净亏 `$6.512921`，已触发自身 `challenger_portfolio_daily_loss_gate` 并永久 `HALTED`；不得解除或重置。它未达到 500 笔/336 小时门槛。
- 2026-07-27 02:24 JST 的完整 240 样本窗口可控路径 P95 为 `239.41–255.88ms`；同一轮五分钟切换附近曾升至 `562.45–636.77ms`，仍未稳定达到 `<100ms`。
- observer 在 3707 笔官方结算的只读 SQL 快照中，费用、滑点和延迟后 ROI `-4.6399%`，每笔 P&L 95% CI `[-$0.343392,-$0.094402]`；累计净 P&L 和置信区间仍为负，不支持推广。
- 去除 champion SQLite 的无变化 window/account 刷新后，四库修改频率短窗约降 `87.5%`、主进程单核 CPU 约降 `20.2%`，但 process write_bytes 仅降约 `10.7%`；四份每秒原子状态 JSON 及文件系统元数据放大成为主要剩余写入。
- 同盘受控基线进一步修正了写入口径：三次各 20 次原子替换的 gross `write_bytes / payload` 均约 `1.089×`，不是主进程同窗的 `5.45×`；主进程 `write_bytes=5,922,816B` 同时有 `cancelled_write_bytes=4,866,048B`，净记账 `1,056,768B` 约为状态载荷 `1,086,735B` 的 `0.97×`。不得把 gross `write_bytes` 单独称为物理写入，也没有证据支持为此迁移 tmpfs 或放弃原子读取语义。
- 2026-07-26 两段低开销配对窗口中，main gross 写入约 `81–83%` 被 `cancelled_write_bytes` 抵消；取消比例在 event-loop P95 约 `59–62ms` 和约 `306–324ms` 的窗口相近，安静段写入速率反而更高。observer SQLite、decision JSONL、review bundle 均未变化；当前证据不支持把 gross 或取消写记账当成尾延迟根因。
- 2026-07-26 已让 `run_due_reviews` 仅在产生新复盘或 bundle 缺失时写文件；不改变 1 秒运行状态 freshness。远端完整 60 秒复盘周期验证 0 次无效重写。
- 2026-07-26 的 15 分钟、166 样本同窗覆盖三个自然五分钟边界：前两次 event-loop/path 峰值主要在切换前，第三次在切换后，market activation 不是一致充分解释。event-loop P95 与五秒 CPU steal 的探索性相关约 `0.516`，但时间尺度不匹配，不能作因果结论。
- 2026-07-27 的双栈只读 sidecar 各使用四条独立 keep-alive 连接、每条 24 次请求：IPv4 96/96 成功且四路 warm P95 为 `80.52–81.34ms`；IPv6 96/96 成功，三路为 `80.48–80.75ms`、一路因单个 `157.47ms` 尖峰为 `98.18ms`。响应体读取 P95 均不超过约 `0.18ms`，延迟几乎全在 TTFB；首次 DNS/TCP/TLS 的墙钟阶段受并发调度影响，总建连为 `160.95–480.08ms`。主进程现有 CLOB/TLS socket 实际走 IPv6，短窗没有复现或解释 240 样本主窗口的资产分组，不能据此强制 IPv4 或改指标口径。

## 运行位置与访问

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

快照时间：2026-07-26 17:24 UTC

- API `ready: true`
- 资产执行引擎：4/4 在线（BTC、ETH、SOL、XRP）
- 逻辑信号通道：16（每资产 4 条）
- observer fills / settled：约 `3711 / 3707`
- observer normalized P&L：约 `-$811.45`
- observer accuracy：约 `59.3%`
- champion equity：`$7.790189`
- champion halt：`portfolio_incident_latched`
- finite-cash challenger：`favorite-price-holdout-v1`
- challenger 初始资金：`$30`
- challenger fills / settled：`6 / 6`
- challenger equity：`$23.487079`
- challenger halt：`challenger_portfolio_daily_loss_gate`

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

## 重要口径

- `FILLED` 是模拟 FAK 到达盘口后可成交，不是 Polymarket 真实成交。
- `OBSERVER P&L` 是所有归一化假设订单的累计盈亏，不是账户余额，也不能和 champion equity 相加。
- `ARRIVAL AVG` 是历史累计信号到模拟到达盘口延迟。
- `BEST ACK FLOOR` 是当前四引擎中最好的估算值，没有真实订单 ACK 证明。
- `RISK HALT` 仅表示冠军有限资金账本停止；行情采集和 observer 仍继续。
- challenger 重启会从进程内 rowid 游标 0 做一次幂等历史核对，产生短时 CPU/写入峰值；评价退避效果必须排除这个一次性回放窗口。

## 下一步优先级

1. 保持 `favorite-price-holdout-v1` 参数冻结，收集至少 500 笔官方结算和 336 小时运行；不得因中途盈亏改规则或重置账本。
2. 把概率校准误差、价格区间和方向差异作为下一批并行 challenger 假设，不得回头过滤本次 holdout。
3. 把四条策略通道分别建立独立账本；在此之前不要把 16 lanes 描述成 16 个已交易 bot。
4. 保持主进程、状态 writer、1 秒 freshness、原子读取语义和 240 样本窗口不变；下一轮让只读 sidecar 完全匹配主探针的 IPv6 `/time`、5 秒 cadence 和至少 240 样本窗口，同时记录每个请求的 TTFB、连接年龄/重建和主引擎 HTTP P95。短双栈窗口已排除响应体读取和稳定 warm 路径差异，下一步只验证稀疏尾部与长期连接生命周期，不得靠强制 IPv4 美化指标。
5. 若接入 CVD/清算，使用一个共享 Binance Futures collector，再广播给四资产引擎；不要每个策略重复开连接。

## 安全边界

- 不添加或上传真实钱包私钥。
- 不把 dashboard 直接监听到公网；继续通过 SSH 隧道访问。
- 不自动解除冠军事故锁。
- 不把宣传截图中的收益、胜率或复杂图形当成策略证据。
