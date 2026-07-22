# Fast V2 交叉评估包

请把这份文件连同仓库交给另一位评估者。目标不是确认作者结论，而是主动寻找会让 2–3 周模拟结果失真的问题。

## 冻结事实

- 环境：AWS Lightsail Dublin，公网地址由项目所有者管理。
- 当前 BTC 5m metadata：`itode=true` 但官方市场响应 `seconds_delay=0`；布尔开关不能推导出 250ms。
- VPS 实测常态 HTTP P50 约 21–24ms；旧版 264.6ms 模拟到达包含错误硬编码的 250ms，已从评估口径剔除。
- 初始本金：`$10`；单笔上限 `$5`；现金保留 `$1`；市场最小 5 shares。
- 模式强制 `shadow`，无私钥、无真钱下单代码路径。
- V1 的 11 笔结果无现金限制且信号价即成交，标记为无效，不得并入 V2。

## 请重点挑战

1. `seconds_delay` + 出站延迟的模拟时点是否正确，是否存在未来信息或信号价成交偏差。
2. 本地订单簿能否在乱序、漏包、相同时间戳、交叉盘口时 fail closed。
3. SQLite 事务能否在 intent 后、fill 中、settlement 前强杀恢复且不多/少一分钱。
4. 5-share 最小单、费用、tick rounding、partial/FAK reject 是否与当前 API 语义一致。
5. Chainlink 开盘价、provisional 与 Gamma official outcome 是否可能错配窗口。
6. no-trade 样本、成功交易和失败交易是否都有记录，是否存在选择偏差。
7. 自动复盘是否会在不足 14 天/500 fills、账本不健康或 holdout 未冻结时误改 champion。
8. 前端 stale、equity、locked、market value、drawdown 是否可能显示旧值或错误绿色状态。

## 必须满足的不变量

- `cash >= 0`、`locked >= 0`。
- 一个 campaign + condition 最多一个 intent；一个 intent 最多一个 fill 和一个 position。
- `intent submitted` 时资金从 cash 转 locked；reject 全额退回；fill 只退未使用余额。
- 未官方结算的仓位不能计入 realized P&L。
- 重启不能重置本金、统计或 run id。
- 关键协程退出必须结束主进程，让 systemd 重启。
- 自动化永远不能自行进入 live。

## 代码入口

- `fastbot.py`
- `polyvote/fastbook.py`
- `polyvote/fastmarket.py`
- `polyvote/faststrategy.py`
- `polyvote/fastledger.py`
- `polyvote/fastreview.py`
- `fast_dashboard.html`
- `tests/test_fast*.py`

## 希望返回的格式

按 P0/P1/P2 分组；每项包含：证据（文件/行/可复现步骤）、影响、最小修复、验收测试。另给出：

- 是否允许继续收集正式 V2 样本；
- 是否需要重置 campaign；
- 哪些指标仍不可信；
- champion/challenger 方案的反例；
- 是否达到“可讨论极小实盘 canary”的门槛（默认答案应为否，除非证据充分）。
