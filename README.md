# polyvote

> **状态：已中止（2026-10-13）。** 所有服务已停止，部署机器已关闭。本仓库作为研究记录公开，全程为模拟盘（shadow），从未真实下单。最后的状态与结论见 [`FAST_MARKET_MEMORY.md`](FAST_MARKET_MEMORY.md)。

多模型投票的 Polymarket 交易 agent。**不走 LLM API**，走 `claude` / `codex` CLI 的
一次性子进程，吃订阅配额。

```
haiku 查一次资料  →  三家在同一份事实上独立判断  →  Fable 5 拍板  →  代码层风控  →  下单
  (唯一能联网)        claude-opus-4-8   ┐
                      claude-fable-5    ├─ 对价格致盲, 无工具
                      gpt-5.6-sol       ┘
```

## 快速开始

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run.py --once      # 跑一轮看看
.venv/bin/python run.py --status    # 看攒了什么数据
.venv/bin/python run.py             # 常驻,45 分钟一轮

.venv/bin/python dashboard.py       # 观测台 → http://<主机名>:8920
```

默认 `dry_run: true`，不会碰你的钱。

## 观测台

`dashboard.py` 只读 SQLite，跟交易逻辑完全解耦——观测台挂了 bot 照跑。
用 stdlib `http.server`，不引入 FastAPI（Pi 上已经跑着好几个 uvicorn 了）。

每轮可展开，看得到：**行情** → **研究简报**（事实逐条列出带来源，研究员明说
查不到的点单独标黄）→ **三个 agent 的完整推理**（估值/信心/理由）→ **决策 + 理由**
→ **风控判定**（被拦就逐条列出拦截原因）。最新一轮默认展开。

配色取自 dataviz 参考调色板的 dark 列，过了验证器：

```
node validate_palette.js "#3987e5,#008300,#d55181,#c98500" --mode dark --surface "#12120f"
→ ALL CHECKS PASS · 最差相邻 CVD ΔE 13.0 · 对比度全部 ≥3:1
```

别改成霓虹色——那些在色盲模拟下会糊成一团。方向也不靠颜色单独承载，
chip 里同时印着文字。

### 关于那些美元数字，别搞混单位

「交易盈亏」是**真美元**。「配额当量」是 `claude` CLI 报的 `total_cost_usd`——
那是**「走 API 会花多少钱」的折算**，你走的是订阅，**它不是真实支出**。

这两个数不能相减。（本来有个「净额（扣 LLM）」瓦片就是这么干的，已经拿掉——
按那个算法 $1.19/轮 × 3 市场 × 32 轮/天 = $114/天 对上 $50/天 的交易上限，
永远深度负，但那个负数没有任何意义。）

订阅制下真正的约束是**5 小时滚动窗口**，`cc_engine.py` 里夏的 `rate_limit_type`
就是 `five_hour`——你、夏、这个 bot 吃的是同一个池子。所以面板上那个瓦片盯的是
窗口占用，不是"利润"。

## 换模型

两种方式，模型**不写死**：

**1. 观测台前端**（运行时，不用重启）—— 「模型设置」面板里每个角色一个下拉，
改完写进 `overrides.json`，bot 下一轮 load 时生效。`config.yaml` 不动。

**2. `config.yaml`**（永久基线）：

```yaml
voters:
  - id: opus
    engine: claude          # claude | codex
    model: claude-opus-4-8  # 直接写模型名
    effort: medium
decider:
  engine: claude
  model: claude-fable-5
```

叠加逻辑：`config.py::_apply_overrides`，run.py 的常驻循环每轮重新 load。
engine 不让在前端改（换 engine 会改 effort 语义和工具能力，codex 没 WebSearch）。

`known_models` 只做**软校验**——填了不在表里的模型只 WARNING，照常执行。
硬 allowlist 会在新模型上线时把合法模型挡在门外（某项目 Fable 5 上线时前端有按钮、
后端 allowlist 没加，点了直接 400）。这张表同时喂前端下拉，note 里标了实测成本档。

**实测成本（2026-07-18，同样负载）：fable-5 比 opus-4-8 贵约一倍**，主要贵在
output 计价。默认决策者用 fable 是设计意图，但想省钱可在前端换 opus。

## 为什么不复用 xia-engine 的 :8900 / :8901

**是正确性问题，不是性能问题。**

`xia-engine/codex/codex_engine.py:128` —— 一个 engine 生命期一个活跃 thread。
`:300-305` 的分支逻辑：调用方不传更新的 `epoch` 就直接复用 `self._thread_id`。
也就是说走 `:8901` 会把交易 prompt **追加进夏正在用的对话 thread**，她下一轮就读到了。

`cc_engine.py:64` 的 `_lock` 还会把两边的轮次串行化（Codex 那边 turn 最长 230s）。
`cc_engine.py:1402` 记着上次往共享引擎里塞低成本后台调用的下场：
「ping 握 `_lock` 最长 60s → 并发的真实用户轮阻塞 60s 无心跳 → **夏看着卡死**」。

所以这里每次投票起一个独立的一次性子进程：无 session、无 resume、无共享 thread、
无锁，崩了只崩自己。

## Prompt 的出处：抄的 valory-xyz/mech-predict

`brain.py` 里那套校准规则**不是我编的**，是从 [valory-xyz/mech-predict](https://github.com/valory-xyz/mech-predict)
的 `superforcaster_polymarket_v4` 和 `superforcaster_calibrated_full_search` 搬来的。
那是 Olas Predict 的线上 mech，每条规则都从**实测 Brier score 归因**推出来，
不是拍脑袋。他们拿 87 个真实市场（50 Omen + 37 Polymarket）回放做 A/B：

| 版本 | Polymarket Brier 改善 |
|---|---|
| V1（只加校准规则） | −14.3% |
| V2（对称校准） | −7.5%（**反而更差**） |
| V3（显式 base-rate 先验） | **−15.4%** ← 上线版本 |

搬过来的，按重要性：

- **赔率过滤器**（`RESEARCHER_SYSTEM`）——搜索结果里冒出 polymarket/metaculus/kalshi
  的百分数时**整条丢弃**。那是正在被解决的这个市场自己的价格，是循环自指证据。
  我原来只写了"别去查赔率"，没写"查到了也要丢"——那是致盲的后门。
- **TYPE A/B 时序分类**（`Fact.within_window`）——每条事实标注是"窗口内有日期的证据"
  还是"常青标准页"。全是 B 就退回 base rate。修的是"X 本周上头条"这类盘靠标准页
  给出 p_yes=0.99 然后结算成 NO，**占了他们 W-1 Brier 质量的 20.9%**。
- **意向语言折价**——`"is set to"` / `"plans to"` / `"scheduled to"` 是**宣布的意图
  不是已完成的事实**，兑现率只有 40-60%。
- **判准特异性**——话题热度 ≠ 判准被满足（另外 32.2% 的 Brier 质量）。
- **`reasons_no` 排在 `reasons_yes` 前面**（`VOTE_SCHEMA` 字段顺序）——schema 顺序
  就是生成顺序，等于强制先想反面。他们所有变体都是这个顺序，针对实测出来的
  系统性 over-predict YES。
- **赔率辨析开场白**——"别把 0.5%（1:199）和 5%（1:19）当成差不多小"。
- **信心-区间耦合**——`confidence < 3` → `fair_price` 夹在 0.30-0.70。这条直接
  治我那个"无知被当成 edge"的病，比只在投票层设闸门更靠前。
- **反谦逊守则**——"永远不要仅仅为了显得谦虚或平衡就把预测往 0.5 挪"。
  没有这条，上面所有校准规则会把模型压成永远弃权。

**V2 变差的教训也照搬了**：他们诊断出 V2 加的对称低端检查让模型"把'新闻在讨论
这件事的可能性'当成了'来源确认了这件事'"，反而更自信。V3 的修法是把证据门槛
藏在字面词 `Otherwise:` 后面，对已确认的事件不触发——`VOTER_SYSTEM` 里保留了
这个结构，别手贱去掉那两个 "Otherwise:"。

**没抄的**：[Polymarket/agents](https://github.com/Polymarket/agents) 官方那版
（已归档）。它的 `superforecaster()` prompt 有 `{float}`/`{str}` 的 f-string bug
（实际发出去渲染成 `<class 'float'>`）、输出是自然语言句子再用**另一次 LLM 调用**
去抽数字、RAG 的 "filter" prompt 其实是被拿去做向量相似度检索的查询串（语义上毫无意义）。
它的历史价值是"superforecaster"这个框架命名，别的都是反面教材。

## 为什么研究和判断要拆开

第一版是三个投票人各自 WebSearch。改成一个 haiku 查一次、三家共用，实测（同一个盘）：

| | 各查各的 | haiku 查一次 |
|---|---|---|
| 研究 | — | $0.265 |
| opus 票 | $0.350 | **$0.083** |
| fable 票 | $0.377 | **$0.163** |
| 决策 | $0.176 | $0.203 |
| **合计** | **$0.90** | **$0.71** |
| 三家估值分歧 | 0.030 | **0.020** |

省钱只是次要的。**主要是一致性**：三家查到的事实不一样时，那三票的分歧到底是
"判断不同"还是"看到的东西不同"根本分不清——共识投票测的就成了搜索引擎的随机性。
现在三家看同一份简报，分歧才真正代表判断差异。

副作用是投票人的边际成本塌了（$0.35 → $0.08），**再加投票人几乎不要钱**。

代价：研究员漏掉的东西三个投票人一起瞎。这是拿多样性换一致性，换得值。

## 四个踩过的坑（都是实测，别想当然）

### 1. 锚定效应会让投票机制彻底失效

第一版把市场价喂给投票人，结果（阿根廷夺冠盘，市价 0.411）：

```
opus → fair=0.410    fable → fair=0.410    sol → fair=0.410
```

三个"独立"判断全是市场价的回声。**三票零信息量。**

修法：投票人对价格**致盲**——只给问题、结算规则、成交量、剩余天数，
看不到任何报价，也看不到订单簿（盘口会泄露价格）。方向由 `derive_sides()`
拿 `fair_price` 和真实价格相比算出来，模型不参与。

投票人能联网，所以 `VOTER_SYSTEM` 里明令**禁止搜索赔率 / Polymarket / 博彩盘口**——
搜到赔率再报回来的话致盲就白做了，那一票是在拿市场衡量市场。

### 2. 模型的"无知"会被当成 edge

致盲之后模型老实报 `fair=0.500 conf=1`（"我不知道，五五开"）。
但市价 0.411 → `edge = +0.089 > min_edge 0.08` → **被推导成 YES 票**。

均匀先验在任何非 0.5 的盘上都长得像机会。这比锚定更危险：锚定只是让 bot 不动手，
这个会让它照着纯粹的无知下注。

修法：`min_voter_confidence`（默认 5）——低信心票强制弃权，**不看 edge**。
外加给投票人开 WebSearch，让 0.5 变成有依据的估计。开了之后同一个盘：

```
opus → fair=0.450 conf=7    fable → fair=0.450 conf=7    sol → fair=0.420 conf=7
（三家都查到了"阿根廷7月19日对阵西班牙决赛"，各自给出实质推理）
```

### 3. `--max-turns` 给小了模型会静默挂掉

- 给 1 → `--json-schema` 是靠工具调用实现的，结构化输出还没发就 `error_max_turns`
- 给 3 → 模型**尝试** WebSearch 被拒**也烧轮次**（`--allowedTools ""` 只挡执行、
  不删工具定义）。fable-5 在真实市场问题上必挂，而 **opus-4-8 同参数却能过**——
  别拿单个模型的通过当验证。

现默认 10。失败时 `stderr` 是空的、真相在 `stdout` 里，所以 `llm.py` 的错误
两个都带上。

## 成本（实测，Pi，2026-07-17）

| 调用方式 | 成本/次 |
|---|---|
| 裸 `claude -p` | $0.73 |
| `+ --setting-sources ""` （砍掉 110k cache read） | ↓ |
| `+ --system-prompt` （换掉 agentic 系统提示） | ↓ |
| `+ --allowedTools ""` （cache_creation 15.7k→3.5k） | **$0.17** |
| 开了 WebSearch 的投票 | $0.35 |

**一个市场一轮 ≈ $0.90**（2 次 claude 投票 + 1 次 codex 投票 + 1 次决策；
codex 走另一份订阅）。`max_markets_per_round: 3` → **$2.7/轮**，45 分钟一轮
→ 约 **$18 / 5 小时窗口**的配额当量。

夏用的是同一份订阅配额。调低 `interval_minutes` 或调高 `max_markets_per_round`
之前先想清楚这件事。

## 开实盘

```bash
export POLYVOTE_PRIVATE_KEY=0x...      # 只从环境变量读,不进 config/日志/DB
# config.yaml: dry_run: false
```

`dry_run: false` 但没设私钥 → `config.py` 直接拒绝启动。

**先跑几天 dry-run，用 `--status` 和 `polyvote.db` 看投票质量再开实盘。**
别靠感觉决定——那张表就是为这个存在的。

## 风控

`risk.py` 是硬闸门：**LLM 说什么都不算数，这里说了算**。
决策者 prompt 里也写了约束，但 prompt 是建议，这里是强制。

任何一条不过 → **整单拒绝**，不做"砍到上限后放行"——砍到上限只是把一个明显
失控的决策变成一个小额失控的决策，真正该做的是拒绝并记录，让人看到模型在乱来。

| 闸门 | 默认 |
|---|---|
| `dry_run` | `true` |
| 单笔上限 | $5 |
| 日上限 | $50 |
| 单市场仓位上限 | $20 |
| 最多未平仓市场 | 5 |
| 决策者信心下限 | 6 |
| 同向票下限 | 2 |
| 单票信心下限 | 5 |
| 单票 min_edge | 0.08 |
| 投票人分歧上限 | 0.25 |

决策者调用失败 → 按 SKIP 处理。**绝不 fail-open 成下单。**

## 市场筛选

按 24h 成交量排序的榜首全是 `0.0085 / 0.9915` 这种**已定盘**的市场
（埃塞俄比亚总理之类），成交量巨大但没有任何 edge。
所以**价格区间才是最重要的筛子**，不是成交量——实测 100 个市场里 60 个是被
`price_min/price_max` 挡掉的。

## 部署

```bash
cp deploy/pi.env.example deploy/pi.env   # 填你自己的 SSH 坐标
bash deploy/push.sh
```

**用这个脚本，别手打 `tar czf - . | ssh ...`。** 我手打过，cwd 不对，
把整个 Windows 家目录（6.5G，含 `.ssh` 私钥和 `.claude.json`）传到了目标机器上。
更糟的是那个 tar 解压进程活了很久，第一次清理时它还在往回写——删了又长。

`push.sh` 用**白名单**而不是 `--exclude` 黑名单（黑名单漏一个是事故，白名单漏一个
只是少传个文件），路径全绝对不依赖 cwd，传完自动检查有没有意外的点文件。

目标机器上要有 `claude` 和 `codex` 且已登录（吃订阅，不走 API）。

投票是**串行**跑的（不是并行）：每个 claude 子进程峰值几百 MB。如果目标机器上还跑着
别的 LLM 服务，并行会把 event loop 压住——串行慢 1-2 分钟，在 45 分钟一轮的节奏下
毫无影响。

`deploy/` 下有两个 systemd user unit（bot + 观测台）。bot 那个带
`Nice=10 CPUWeight=30`，就是为了不跟同机的其他服务抢。

## 已知限制

- **相关市场会造成隐性集中，代码没处理。** 实测撞见过：同一轮里阿根廷盘三家平均
  估值 0.477（市价 0.411）、西班牙盘 0.530（市价 0.586）——两个独立分析的盘，
  模型概率加起来 1.007，内部一致性几乎完美，共同观点是"市场高估了西班牙"。
  但那是**同一注下两遍**：买阿根廷 YES 和买西班牙 NO 是同一个赌。真下单的话敞口翻倍，
  而 `max_open_positions` 会把它们算成两个不同仓位。世界杯这种一个事件裂成 N 个盘的
  场景尤其危险。暂时靠 `exclude_keywords` 手动规避。
- **只做二元市场**。多结果市场的 token 映射和仓位逻辑不一样，`parse_market()` 直接跳过。
- **仓位是按成交额记的粗口径**，不是从链上读真实持仓。dry-run 够用，实盘跑久了
  会和链上状态漂移。
- **没有平仓策略**。决策者可以出 SELL，但没有主动的止盈止损巡检。
- **dry-run 的模拟成交价**优先用真实卖一；拿不到盘口时退回中间价，会让成绩
  偏乐观（忽略了点差），日志里会标出来。
- **Polymarket ToS 限制美国用户**通过 API/agent 交易。Pi 出口 IP 在日本
  （实测 `x.x.x.x / JP / SoftBank`），但 "US person" 是身份概念不是地理概念。
