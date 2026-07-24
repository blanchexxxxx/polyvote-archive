# Fast Market 工作记忆

最后更新：2026-07-25 00:26 JST

这份文件是当前快市场系统的唯一快速交接入口。旧 `HANDOFF.md` 主要描述已退休的慢市场，而且存在编码损坏；继续工作时先读本文件。

## 当前结论

- 系统正在 AWS Lightsail VPS 上持续运行，关闭 Codex 或浏览器不会停止 VPS 服务。
- 当前完全是 **shadow / 模拟盘**，没有钱包私钥，没有真实下单。
- 慢市场已退休，不再开发或展示。
- 原严格资金冠军账本从 `$30.00` 亏至 `$7.790189`，已被 `portfolio_incident_latched` 永久风控锁定，不再产生订单。
- 独立 observer 使用归一化 `$5` 假设订单持续收集样本，不占用也不扣减冠军账本；它是研究统计器，不是严格资金模拟账户。
- observer 的早期正收益已被大样本证伪：从 217 笔时的 `+$27.69961` 变成约 2239 笔时的 `-$341.90`。高胜率没有转化为正期望。
- 独立 `$30` 有限现金实验 `favorite-price-holdout-v1` 已于 `2026-07-24T15:12:32Z` 冻结并启动。它只跟随该时间之后的 observer 到达盘口记录，额外规则只有 `intended ask >= 0.60`；这是待验证的事后假设，不是策略升级。

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

快照时间：2026-07-24 15:25 UTC

- API `ready: true`
- 资产执行引擎：4/4 在线（BTC、ETH、SOL、XRP）
- 逻辑信号通道：16（每资产 4 条）
- observer fills / settled：约 `2241 / 2239`
- observer normalized P&L：约 `-$341.90`
- observer accuracy：约 `62.44%`
- champion equity：`$7.790189`
- champion halt：`portfolio_incident_latched`
- finite-cash challenger：`favorite-price-holdout-v1`
- challenger 初始资金：`$30`
- challenger 当时 fills / settled：`1 / 0`
- challenger 当时标记权益：约 `$29.95`

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

## 重要口径

- `FILLED` 是模拟 FAK 到达盘口后可成交，不是 Polymarket 真实成交。
- `OBSERVER P&L` 是所有归一化假设订单的累计盈亏，不是账户余额，也不能和 champion equity 相加。
- `ARRIVAL AVG` 是历史累计信号到模拟到达盘口延迟。
- `BEST ACK FLOOR` 是当前四引擎中最好的估算值，没有真实订单 ACK 证明。
- `RISK HALT` 仅表示冠军有限资金账本停止；行情采集和 observer 仍继续。

## 下一步优先级

1. 保持 `favorite-price-holdout-v1` 参数冻结，收集至少 500 笔官方结算和 336 小时运行；不得因中途盈亏改规则或重置账本。
2. 把概率校准误差、价格区间和方向差异作为下一批并行 challenger 假设，不得回头过滤本次 holdout。
3. 把四条策略通道分别建立独立账本；在此之前不要把 16 lanes 描述成 16 个已交易 bot。
4. 继续拆分延迟：HTTP、event-loop、VPS 路由和模拟执行耗时；但已证实低延迟样本同样亏损，不能把延迟当作唯一根因。
5. 若接入 CVD/清算，使用一个共享 Binance Futures collector，再广播给四资产引擎；不要每个策略重复开连接。

## 安全边界

- 不添加或上传真实钱包私钥。
- 不把 dashboard 直接监听到公网；继续通过 SSH 隧道访问。
- 不自动解除冠军事故锁。
- 不把宣传截图中的收益、胜率或复杂图形当成策略证据。
