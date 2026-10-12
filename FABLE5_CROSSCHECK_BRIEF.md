# Polyvote Fast Market：Fable 5 独立交叉复核说明

生成时间：2026-08-01 JST

## 给 Fable 5 的任务

你是独立审计人，不是当前实现的辩护者。请从原始账本、源码和时间戳快照重新计算关键结果，逐项给出：

1. **确认**：数据和实现支持该结论；
2. **反驳**：给出可复现的相反证据；
3. **无法验证**：明确缺少什么原始数据，不得用推测补齐；
4. **潜在偏差或漏洞**：说明严重度、影响方向和最小复现实验。

不要只复述 `FAST_MARKET_MEMORY.md` 或 dashboard。优先检查 SQLite 原始行、当前源码、冻结配置和 `git diff`。所有建议必须保持 shadow、fail-closed 和自然时间 holdout；不得建议解除事故锁、回改冻结参数、启用实盘、添加钱包私钥，或仅凭一次 P95/CPU steal 建议购买机器。

## 当前待复核结论

以下是 Codex 的结论，不应被当作已证明事实：

- 系统完全为 `SHADOW`，没有真实订单或钱包私钥。
- champion 已永久被 `portfolio_incident_latched` 锁定；favorite challenger 已永久被 `challenger_portfolio_daily_loss_gate` 锁定。
- observer 在 `2026-07-31T14:48:40Z` 的只读快照中有 8179 笔官方结算，净 P&L `-$2,220.137485`、费用 `$1,009.390778`、滑点 `$62.221634`、总成本 `$38,709.467685`、ROI `-5.7354%`、Brier `0.239158`、accuracy `59.2982%`，每笔 P&L 普通近似 95% CI `[-$0.354483,-$0.188404]`。
- 平均 selected probability 为 `70.9623%`，比实际 accuracy 高约 `11.6640pp`；标准 intended-ask、方向、selected-probability 和 latency 分桶的 ROI 点估计均为负。
- 可控路径口径是 `CLOB warm HTTP P95 + event-loop lag P95 + decision P95`；官方 platform delay 单列。该口径曾在完整窗口达到 `83–89ms`，但多数近期窗口明显高于 100ms，因此“偶尔可达”不等于“稳定达标”。
- 高 CPU steal、event-loop 尾部和 freshness 失败多次共现，但既有 240 点研究的 5 秒相关仅约 `0.202`，且存在反向变化；现有证据不足以证明 steal 是唯一根因。
- 本地已实现但尚未部署 `calibrated-probability-holdout-v1`。它用冻结前 8114 笔结算生成四个固定 reliability bins，并在校准后重算净 edge；其正期望尚无 holdout 证据。

## 必须先区分的三个状态

### 1. VPS 正在运行的版本

远端目录：`/home/ubuntu/polyvote`

它是当前真实运行的 shadow 采集器。远端部署目录没有 Git 历史，不能用远端 `git status` 判断部署一致性。

### 2. 本地 `HEAD`

本地仓库根目录：`C:\Users\<user>\code\polyvote`

用以下命令查看已提交基线：

```powershell
git rev-parse HEAD
git show HEAD:fastbot.py
git show HEAD:fastchallenger.py
```

### 3. 本地未提交工作树

当前工作树包含尚未部署的 timing/bucket 诊断和 calibrated challenger 改动。请先运行：

```powershell
git status --short --branch
git diff -- fastbot.py tests/test_fastbot.py
git diff -- fastchallenger.py tests/test_fastchallenger.py
git diff -- deploy/polyvote-fast-challenger.service deploy/push.sh
Get-Content -Raw fast_calibration_challenger.yaml
```

不得把本地未提交实现当成 VPS 已运行功能。特别是远端目前预期不存在：

- `fast_challenger_calibrated_v1_{asset}.db`
- `fast_calibration_challenger_state.json`
- `fast_calibration_challenger_incident.json`

## 数据在哪里

### A. 当前聚合状态 API

本机已通过 SSH 隧道访问：

```text
http://127.0.0.1:8920/api/fast/state
```

PowerShell 读取：

```powershell
$state = Invoke-RestMethod http://127.0.0.1:8920/api/fast/state
$state.generated_at
$state.ready
$state.portfolio
$state.observer
$state.challenger
$state.bots | Select-Object asset,online,state_age_seconds,network
```

注意：

- API 是滚动聚合状态，不是不可变历史数据集。
- engine freshness 应以每个 bot 的 `online` 和 `state_age_seconds` 为准。
- `generated_at` 可用于识别 dashboard/隧道延迟。
- `observer.average_latency_ms` 是历史信号到模拟到达盘口延迟，不是当前公网 RTT。
- `estimated_taker_ack_floor_ms` 是估算下限，不是真实订单 ACK。

### B. 项目内研究与口径文档

本地根目录下：

- `FAST_MARKET_RESEARCH_LOG.md`：逐轮原始指标、失败、假设和差异；最完整的历史聚合记录。
- `FAST_MARKET_MEMORY.md`：经过压缩的当前结论；适合导航，不应替代原始复算。
- `AGENTS.md`：安全边界与 memory 同步副本。
- `README.md`、`HANDOFF.md`：旧慢市场背景；不是当前 Fast Market 统计的权威来源。

最新详细段落可在本地搜索：

```powershell
rg -n "23:55 JST|22:06 JST|8179|calibrated-probability" FAST_MARKET_RESEARCH_LOG.md FAST_MARKET_MEMORY.md
```

### C. VPS 原始 SQLite 账本

SSH 坐标：

```text
ubuntu@<vps-ip>
```

本机密钥路径：

```text
C:\Users\<user>\.ssh\<ssh-key>.pem
```

只可使用该密钥建立既有只读检查；不得读取、打印或复制密钥内容。

```powershell
ssh -i C:\Users\<user>\.ssh\<ssh-key>.pem ubuntu@<vps-ip>
```

进入远端后：

```bash
cd /home/ubuntu/polyvote
```

原始账本：

| 目的 | 文件模式 |
|---|---|
| observer 假设订单 | `fast_observer_btc.db`, `fast_observer_eth.db`, `fast_observer_sol.db`, `fast_observer_xrp.db` |
| champion 严格资金账本 | `fast_shadow_v5_btc.db`, `fast_shadow_v5_eth.db`, `fast_shadow_v5_sol.db`, `fast_shadow_v5_xrp.db` |
| favorite challenger | `fast_challenger_favorite_v1_btc.db`, `fast_challenger_favorite_v1_eth.db`, `fast_challenger_favorite_v1_sol.db`, `fast_challenger_favorite_v1_xrp.db` |

远端没有 `sqlite3` CLI；使用 Python 标准库并强制 `mode=ro`。最小 observer 复算示例：

```bash
python3 - <<'PY'
import sqlite3

assets = ("btc", "eth", "sol", "xrp")
totals = dict(rows=0, wins=0, pnl=0.0, fees=0.0, slippage=0.0, cost=0.0)

for asset in assets:
    path = f"fast_observer_{asset}.db"
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    row = db.execute("""
        SELECT COUNT(*),
               COALESCE(SUM(CASE WHEN outcome=official_outcome THEN 1 ELSE 0 END),0),
               COALESCE(SUM(pnl_usd),0),
               COALESCE(SUM(fee_usd),0),
               COALESCE(SUM(slippage_usd),0),
               COALESCE(SUM(total_cost_usd),0)
        FROM observations
        WHERE settled_ts IS NOT NULL
    """).fetchone()
    db.close()
    for key, value in zip(totals, row):
        totals[key] += value

print(totals)
print("accuracy", totals["wins"] / totals["rows"])
print("roi", totals["pnl"] / totals["cost"])
PY
```

如果需要可复现快照，应在输出中记录 UTC 时间、每个 DB 的文件大小和最大 `settled_ts`。数据库持续增长，因此不同时间读取的样本数不同是正常的。

### D. VPS 当前状态文件与日志

远端目录 `/home/ubuntu/polyvote`：

- `fast_state_btc.json`, `fast_state_eth.json`, `fast_state_sol.json`, `fast_state_xrp.json`：四引擎当前滚动指标。
- `fast_decisions_v5_{asset}.jsonl`：决策流水。
- `fast_reviews_v5_{asset}.json`：最近复盘 bundle。
- `fast_portfolio_incident.json`：champion 永久事故锁证据。
- `fast_challenger_state.json`、`fast_challenger_incident.json`：favorite challenger 状态和事故锁。
- `fast-challenger.log`：challenger 服务日志。

systemd 只读检查：

```bash
systemctl --user show polyvote-fast.service \
  polyvote-fast-challenger.service \
  polyvote-fast-dashboard.service \
  -p Id -p ActiveState -p SubState -p MainPID -p NRestarts

journalctl --user -u polyvote-fast.service -n 500 --no-pager
journalctl --user -u polyvote-fast-challenger.service -n 500 --no-pager
```

### E. 关键源码、配置与测试

都在本地仓库根目录：

| 问题 | 文件 |
|---|---|
| 策略概率、盘口、探针、模拟执行 | `fastbot.py` |
| 四资产组合和事故锁 | `fastmulti.py`, `fast_portfolio.yaml` |
| observer 表结构与结算 | `polyvote/fastobserver.py` |
| 有限现金 challenger 与校准 | `fastchallenger.py` |
| favorite 冻结配置 | `fast_challenger.yaml` |
| calibrated 冻结配置 | `fast_calibration_challenger.yaml` |
| dashboard 聚合口径 | `fastdash_server.py` |
| 账本实现 | `polyvote/fastledger.py` |
| 策略 gates | `polyvote/faststrategy.py` |
| 回归测试 | `tests/test_fastbot.py`, `tests/test_fastchallenger.py`, `tests/test_fastobserver.py`, `tests/test_fastdash_server.py` |

本地验证命令：

```powershell
$py = (Resolve-Path .\.venv\Scripts\python.exe).Path
& $py -m unittest discover -s tests -p 'test_fast*.py'
node tests/validate_fast_dashboard.js
git diff --check
```

当前工作树最近记录为 44 项 Fast tests 通过，但请独立重跑并报告实际数量。

## 统计口径必须独立验证

### Observer

- 样本：`observations.settled_ts IS NOT NULL`。
- 胜负：`outcome = official_outcome`。
- selected probability：`fair_probability` 是已选择方向的概率，不是固定 UP 概率。
- ROI：`SUM(pnl_usd) / SUM(total_cost_usd)`。
- Brier：`AVG((fair_probability - I[outcome=official_outcome])^2)`。
- calibration gap：`accuracy - AVG(fair_probability)`；负值表示整体过度自信。
- 每笔 P&L 95% CI：当前实现使用样本标准差和 `mean ± 1.96*SE`。请同时指出该普通近似在厚尾、相关窗口和非独立样本下的局限；如做 bootstrap，必须按自然时间/市场窗口考虑相关性，不能把相关订单当完全独立样本。
- `FILLED` 表示模拟 FAK 在到达盘口后可成交，不是 Polymarket 真实成交。
- observer P&L 是全部归一化 `$5` 假设订单的累计盈亏，不是账户余额，不能与 champion equity 相加。

### 延迟

- 当前“可控路径 P95”是三个组件各自 P95 的和，不是逐请求端到端 latency 的经验 P95。请评价这个工程指标是否适合目标跟踪，但不要悄悄换口径。
- `clob_http` 计时在 worker 内开始；executor queue-wait 另列，不能从既定路径直接相减。
- socket TTFB 和 sidecar 的基础 P50 都约 78–80ms；差异主要在约 158ms 次级模式进入 P95 的频率。
- platform taker delay 必须单列，不得混入可控路径。
- 历史研究多数只有滚动聚合快照；没有保存的逐请求原始序列不得声称已经完全复算。

### 冻结校准实验

检查 `fast_calibration_challenger.yaml` 和 `fastchallenger.py`，至少验证：

1. 四个 bin 的 `samples` 合计是否为 8114；
2. `calibrated_probability == (wins + 1) / (samples + 2)` 是否成立；
3. training cutoff 是否严格早于 freeze；
4. bins 是否连续、无重叠、覆盖声明区间；
5. 覆盖外是否 fail-closed；
6. 校准后净 edge `<4pp` 是否拒绝；
7. 无校准的 favorite 配置是否保持旧语义；
8. 多配置调度是否保持账本、状态和事故锁独立；
9. 只允许 `created_ts >= frozen_at_utc` 的 observer 记录进入新账本；
10. 是否存在训练数据泄漏、历史回填越过冻结点、重复消费或重启非幂等风险。

不要用训练集拒绝率、训练集 P&L 或校准后的历史回放来声称新策略有正期望。唯一有效推广证据是冻结点之后、独立账本、至少 500 笔官方结算和 336 小时自然时间，并且费用/滑点/延迟后净收益置信区间为正。

## 请重点寻找的反例

- observer 的费用或 `total_cost_usd` 是否被重复计入或漏计；
- `slippage_usd` 是否已经包含在成交成本/P&L 中，统计汇总是否又重复扣除；
- 官方结算与方向映射是否在 UP/DOWN 某一侧有系统性错误；
- 同一 condition 是否跨资产或重启重复记录；
- Brier 使用 selected-side 概率是否与胜负标签严格匹配；
- 账本行是否存在 `settled_ts` 有值但 `pnl_usd`、`official_outcome` 缺失的异常；
- 价格、概率或延迟分桶边界是否重叠/漏样本；
- 95% CI 是否因为市场窗口内相关性而过窄；
- API 的 rolling P95 与短 `/proc` 资源窗是否被错误当成同步因果证据；
- 本地未部署代码是否被文档误写成远端正在运行；
- calibrated challenger 的冻结点和训练截止是否真的不可变且不会被配置默认值覆盖。

## 要求的交付格式

请输出一份中文审计报告，按以下顺序：

1. **执行摘要**：哪些核心结论确认、反驳或无法验证；
2. **数据完整性**：样本数、空值、重复、结算一致性；
3. **独立复算表**：observer/champion/favorite challenger 的 fills、settled、wins、P&L、fees、slippage、cost、ROI、Brier、CI；
4. **校准审计**：四个冻结 bin 和实现语义；
5. **延迟审计**：指标定义、可复现部分和因果限制；
6. **代码问题**：按 P0–P3 排序，提供文件和行号；
7. **结论**：是否继续 shadow、是否允许部署 calibrated holdout、是否有任何理由改 freshness/事故锁/路由/VPS；
8. **下一项唯一实验**：必须是可证伪、自然时间、无回看调参、低成本的方案。

如果没有足够证据，请明确写“证据不足”，不要为了给出行动而制造行动。
