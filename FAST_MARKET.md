# Polyvote Fast V2 运行方案

更新：2026-07-22。当前唯一正式实验是 BTC 5 分钟快市场、`$10` 模拟本金、FAK 吃单基线。旧慢市场和 V1 的“信号价即成交”数据不计入绩效。

## 已验证的延迟结论

- VPS：AWS Lightsail `eu-west-1`（Dublin），`$5/月`、512 MB、2 vCPU；低内存机器已配置 swap。
- 常态 CLOB HTTP P50 约 `21–24ms`；P95 会受 TLS 冷启动和网络尖峰影响，面板持续保留原始滚动值，不能只看最好值。
- 本地决策计算 P95 约 `0.06–0.08ms`。
- `itode=true` 只表示 taker delay 功能开关，不包含延迟时长；旧版将它硬编码为 `250ms` 是错误的。数值只读取官方 `seconds_delay` 字段。当前 BTC 5 分钟市场官方返回 `seconds_delay=0`。
- 旧版 `264.6ms` 模拟成交主要是程序人为等待 250ms 的结果，不能作为平台实测。新版本记录实际连接复用路径，并在真实凭据可用后单独测量 signal→POST→ack。

系统将延迟拆成四项展示：决策计算、Dublin→CLOB HTTP、官方 `seconds_delay`、估算确认下限。`itode` 不再被换算成猜测时长。

## 热路径

```text
RTDS Chainlink/Binance ─┐
                        ├─> 时间戳校验 ─> FastSignalModel ─> hard gates
CLOB Market WebSocket ─> 本地双边完整账本 ────────────────┘
                                                        │
                                                        ├─> SQLite intent + 冻结现金
                                                        ├─> 官方 seconds_delay + 实测出站延迟
                                                        ├─> 到达时盘口逐层 FAK
                                                        └─> fill / partial / reject

开盘前 20 秒：预取下一市场 metadata + 建立下一市场 WebSocket
收盘后：Gamma 官方 outcome 对账，官方结果出现前只记 provisional，不释放盈亏
```

关键实现：

- `fastbot.py`：事件驱动决策、双市场预热、延迟成交、官方结算、任务监督。
- `polyvote/fastbook.py`：全量 snapshot + 增量更新；拒绝旧事件、交叉盘口和失同步账本。
- `polyvote/faststrategy.py`：保存开盘前后的 Chainlink 环形缓存；拒绝重复/倒序价格；强制两路源时间对齐。
- `polyvote/fastledger.py`：SQLite WAL 现金账本。intent 时冻结现金；成交后只退未使用部分；只有 fill 才创建仓位；结算事务幂等。
- `polyvote/fastreview.py`：启动、每小时、每日、每周复盘。参数不足样本时冻结，只生成 challenger 评估。
- `fast_dashboard.html`：账户、订单生命周期、成交账本、延迟拆解、自动复盘、2 秒 stale 全屏告警。

## `$10` 资金规则

- 初始现金严格 `$10.00`，重启不重置。
- 每个市场最多一个 intent；单笔总预算上限 `$5`；至少保留 `$1` 可用现金。
- Polymarket 当前最小单为 5 shares，因此小本金可能经常因资金不足而拒单；这是实验结果，不绕过也不伪造小单。
- 日内已实现亏损达到 `$3.50` 后停止新 intent。
- 费用逐档计入，公式为 `shares × feeRate × p × (1-p)`；见 [Fees](https://docs.polymarket.com/trading/fees)。
- 价格上限为信号 ask 加 1 tick；到达盘口不足 5 shares 时 FAK 拒单；足够 5 shares 但不足目标时记 partial。
- 现金、冻结资金、持仓市值、手续费、已实现盈亏、回撤全部单独展示。

## 数据与复盘

每个窗口记录 metadata、开盘参考、数据质量和 no-trade gate 聚合；每笔记录：

`signal → intent → locked cash → due time → arrival depth → fill/partial/reject → position → provisional → official settlement`

固定节奏：

- 每小时：数据断流、重连、延迟、拒单、账本守恒。
- 每日：P&L、费用、滑点、fill rate、Brier score、回撤和方向分层。
- 每周：只产生 challenger 提案，不原地改 champion。
- 异常：账本为负、过期 intent 或关键协程退出时立即停止，由 systemd 重启并留下事件。

自动调参门槛：至少 `14` 个完整自然日、`500` 个真实化 fills，并保留最终 20% 时间序列 holdout。费用、平台延迟、本金、现金保留、日损和 live mode 永远不属于可调参数。

## 运行与查看

VPS 服务：

- `polyvote-fast.service`
- `polyvote-fast-dashboard.service`

面板仅监听 VPS 的 `127.0.0.1:8920`。本机 SSH 隧道建立后打开 `http://127.0.0.1:8920/`；根路径已经直接指向 Fast V2，慢市场 UI 返回 410。

当前仍为 `SHADOW`，不读取私钥、不签名、不发送真钱订单。任何自动复盘都不能把模式改为 live。

## 正式评估门槛

2–3 周期满不等于自动上真钱。至少同时满足：

- ≥14 完整日、≥500 fills、最终 20% 冻结 holdout；
- CLOB 可控路径延迟按小时报告，不能用一次最好值替代 P95/P99；
- 现金守恒、无重复 intent/fill/settlement、无缺失官方结算；
- 扣除真实费用、延迟与滑点后，净收益置信区间仍可接受；
- 最大回撤、最长连败、资金不足率在 `$10` 约束内可承受；
- 外部交叉评估完成并解决所有 P0/P1 问题。

旧慢市场代码和数据库保留以便恢复，但不再作为主页或正式实验来源。停服务时只 `disable --now`，不删除数据库与日志。
