# Fast Market 工作记忆

最后更新：2026-07-22 20:02 JST

这份文件是当前快市场系统的唯一快速交接入口。旧 `HANDOFF.md` 主要描述已退休的慢市场，而且存在编码损坏；继续工作时先读本文件。

## 当前结论

- 系统正在 AWS Lightsail VPS 上持续运行，关闭 Codex 或浏览器不会停止 VPS 服务。
- 当前完全是 **shadow / 模拟盘**，没有钱包私钥，没有真实下单。
- 慢市场已退休，不再开发或展示。
- 原严格资金冠军账本从 `$30.00` 亏至 `$7.790189`，已被 `portfolio_incident_latched` 永久风控锁定，不再产生订单。
- 独立 observer 使用归一化 `$5` 假设订单持续收集样本，不占用也不扣减冠军账本；它是研究统计器，不是严格资金模拟账户。
- 如果后续需要“每笔当真、余额耗尽就停止”的实验，应新建独立 `$30` 有限现金模拟账本，不能把 observer P&L 当成账户权益。

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
systemctl --user status polyvote-fast-dashboard.service
journalctl --user -u polyvote-fast.service -n 100 --no-pager
```

## 当前运行快照

快照时间：2026-07-22 11:02 UTC

- API `ready: true`
- 资产执行引擎：4/4 在线（BTC、ETH、SOL、XRP）
- 逻辑信号通道：16（每资产 4 条）
- observer fills / settled：`218 / 217`
- observer normalized P&L：`+$27.69961`
- observer accuracy：`64.977%`
- champion equity：`$7.790189`
- champion halt：`portfolio_incident_latched`

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
- `polyvote/fastobserver.py`：零资金独立观察账本
- `polyvote/fastreview.py`：小时/日/周复盘
- `fastdash_server.py`：组合聚合、16 条逻辑通道与只读 API
- `fast_dashboard.html`：单屏作战台
- `tests/test_fastdash_server.py`：策略矩阵与聚合测试
- `tests/validate_fast_dashboard.js`：前端语法、ID、非全屏告警检查

## 已验证测试

- `tests/test_fastdash_server.py`：2/2 通过
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

1. 建立真正有限现金的 `$30` challenger 账本：扣现金、锁仓、收费、余额不足拒单、亏损触线停机。
2. 把四条策略通道分别建立独立账本；在此之前不要把 16 lanes 描述成 16 个已交易 bot。
3. 先修延迟：当前历史 arrival 平均仍为数百毫秒；拆分 HTTP、event-loop、VPS 路由和模拟执行耗时。
4. 若接入 CVD/清算，使用一个共享 Binance Futures collector，再广播给四资产引擎；不要每个策略重复开连接。
5. observer 推广门槛保持至少 500 笔官方结算、336 小时运行和冻结 holdout；不得根据短期胜率自动改实盘代码。

## 安全边界

- 不添加或上传真实钱包私钥。
- 不把 dashboard 直接监听到公网；继续通过 SSH 隧道访问。
- 不自动解除冠军事故锁。
- 不把宣传截图中的收益、胜率或复杂图形当成策略证据。
