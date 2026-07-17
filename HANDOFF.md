# 交接文档

给接手这个项目的 agent。README 讲**是什么**，这里讲**当前状态**和**别踩什么**。

---

## 模型怎么改（两种方式）

模型**不写死**。两个层次:

1. **config.yaml** —— 基线。`researcher` / `voters` / `decider` 各自的 engine+model+effort。
   改这个是永久的,进 git。
2. **观测台的「模型设置」面板** —— 运行时覆盖。在前端点下拉改模型/effort,写进
   `overrides.json`(gitignore),**bot 下一轮 load 时叠加生效,不用重启**。
   config.yaml 一个字不动(注释、结构全在)。

叠加逻辑在 `config.py::_apply_overrides`。run.py 的常驻循环**每轮开头重新 load**
(run.py:271 附近),所以前端改完等下一轮就生效。overrides.json 坏了不会让 bot 崩
(读失败当没覆盖)。

engine 不让在前端改 —— 换 engine 会连带改 effort 语义和工具能力(codex 没
WebSearch),不是一个下拉能安全表达的。要换 engine 改 config.yaml。

实测成本档(2026-07-18,同样负载):**fable-5 比 opus-4-8 贵约一倍**,主要贵在
output 计价。known_models 的 note 里标了,前端下拉能看到。默认把 fable 放在决策者
是用户的设计意图(可能有质量理由),没动 —— 但知道这个成本差,想省钱可以在前端
把决策者换 opus。

## 你需要但 git 里没有的东西

**`deploy/pi.env`** —— 部署坐标（被 .gitignore 挡掉，因为 git 历史永久，这个库
可能会公开）。照 `deploy/pi.env.example` 填：

```bash
cp deploy/pi.env.example deploy/pi.env
# 填 POLYVOTE_PI / POLYVOTE_KEY —— 值找用户要
```

**SSH 私钥** —— 访问目标机器的 key。用户手上有，新机器得自己配。

**`polyvote.db`** —— 跑出来的数据在目标机器上，不在 git 里。

---

## 当前状态（截至交接时）

| 东西 | 状态 |
|---|---|
| 研究 → 投票 → 决策 → 风控 → dry-run 记账 | ✅ 端到端跑通，多轮实测 |
| 结算追踪 | ✅ 代码 + 18 个单元测试全过（`tests/test_settle.py`） |
| 观测台 | ✅ systemd `polyvote-dashboard.service`，跑在目标机 8920 |
| **bot 常驻运行** | ✅ **systemd `polyvote-bot.service` 已 enable + 运行，45 分钟一轮，dry-run** |
| 1h prompt 缓存 | ✅ 实测生效，稳态省 65%（见下） |
| Token 命中率仪表 | ✅ 观测台面板 + `tests/test_cache_meter.py` 13 用例全过 |
| **实盘下单** | ⚠️ **代码写了，从没真跑过。`execute.py::_execute_live` 只做了语法和逻辑验证，没有真实成交过一单。** |
| **有没有 edge** | ⚠️ **完全未知。零结算记录。所有轮次都是 SKIP。** |

### bot 正在运行 —— 怎么看 / 怎么停

```bash
systemctl --user status polyvote-bot        # 状态
tail -f ~/polyvote/bot.log                  # 实时日志
systemctl --user stop polyvote-bot          # 停(临时)
systemctl --user disable --now polyvote-bot # 停(永久,不随开机起)
```

`Linger=yes`，所以登出/重启后 bot 和观测台都会自动起来。**它会一直吃 Max 20x
的配额窗口,直到你停它。** 目前没有夜间静默(夏那边的 keepalive 有
quiet_start_hour,polyvote 还没加)—— 如果要加,在 `run.py` 的常驻循环里判断
当前小时即可。

### 1h 缓存实测数据（2026-07-18，fable-5，间隔 400s）

```
第 1 次(冷启)  cache_w=20,714  cache_r=17,432  等效 30,655  $0.484
第 2 次(命中)  cache_w= 3,268  cache_r=34,864  等效 10,561  $0.151
第 3 次(命中)  cache_w= 3,446  cache_r=34,864  等效 10,913  $0.161
```

400s > 300s(默认 5min TTL)还命中 → `ENABLE_PROMPT_CACHING_1H` 确实生效。
**稳态省 65%。** 原始 token 几乎不变(都 41k),差别全在读/写比例 —— 所以只看
"总 token"会误判,要看加权等效。

含义:早先算的 "$24/5小时窗口" 是**冷启价**,错了。稳态下 haiku/opus/fable-投票/
fable-决策 四份缓存各自热着,实际低得多。真数看观测台的命中率面板。

**别把"跑通了"当成"能赚钱"。** 到目前为止每一轮都正确地弃权了——三家模型都认为
主流盘定价合理，edge 够不着 8 个点的阈值。这是对的行为，但也意味着这套投票机制
**从未被真实盈亏检验过**。`polyvote.db` 攒够已结算数据之前，别开实盘。

---

## 四个实测踩出来的坑（别想当然，别"优化"掉）

### 1. 投票人必须对市场价致盲

第一版把价格喂给投票人，三个模型独立报出 `0.410 / 0.410 / 0.410`，市价 `0.411`。
**三票是市场价的回声，投票机制形同虚设。**

现在 `_blind_brief()` 不含任何价格，方向由 `derive_sides()` 拿 fair_price 和市价
相比推导。**如果你想"顺便把盘口也给投票人看"——那就是在重新引入锚定。**

研究员也一样：`RESEARCHER_SYSTEM` 里的赔率过滤器不是啰嗦，是致盲的最后一道门。
研究员会联网，搜到 Polymarket 赔率再写进简报的话，后门就破了。

### 2. 模型的"无知"会被当成 edge

致盲后模型老实报 `fair=0.500 conf=1`（"我不知道"），但 vs 市价 0.411 = edge
`+0.089` > 阈值 `0.08` → **被推导成 YES 票**。均匀先验在任何非 0.5 的盘上都像机会。

`min_voter_confidence`（默认 5）是专门堵这个的，**别调到 4 以下**。

### 3. `--max-turns` 给小了模型会静默挂掉

- 给 1 → `--json-schema` 是靠工具调用实现的，结构化输出还没发就 `error_max_turns`
- 给 3 → 模型**尝试** WebSearch 被拒**也烧轮次**。fable-5 在真实市场问题上必挂，
  而 **opus-4-8 同参数却能过**

**这条最值钱：别拿单个模型的通过当验证。** 失败时 `stderr` 是空的、真相在 `stdout` 里
（`llm.py` 的报错两个都带上，就是因为踩过）。

### 4. 部署只用 `deploy/push.sh`

手打 `tar czf - . | ssh ...` 时 cwd 不对，**6.5G 的家目录（含 `.ssh` 私钥和
`.claude.json`）被传到了目标机器**。更糟的是那个 tar 解压进程活了很久，第一次
清理时它还在往回写——删了又长。

`push.sh` 用白名单不用 `--exclude`（黑名单漏一个是事故，白名单漏一个只是少传文件），
路径全绝对，传完自动检查意外的点文件。

---

## 已验证的事实（别重新猜，也别重新烧配额去测）

实测于 2026-07-17，目标机 `claude` 2.1.179 / `codex-cli` 0.144.3：

```bash
# Claude —— 结构化输出在信封的 structured_output 字段,不是 result(那里是散文)
claude -p "$PROMPT" --model claude-fable-5 \
  --system-prompt "$SYS" --setting-sources "" --strict-mcp-config \
  --json-schema "$INLINE_JSON" --allowedTools "" --max-turns 10 --output-format json
# --json-schema 只吃内联 JSON,不吃文件路径

# Codex —— 结构化输出是 stdout 最后一个 JSON 行
codex exec "$PROMPT" -m gpt-5.6-sol --output-schema schema.json \
  --ephemeral --skip-git-repo-check -s read-only -C "$WORKDIR"
# --output-schema 吃文件路径,不吃内联
```

**成本（实测，单市场一轮）：**

| | 值 |
|---|---|
| 裸 `claude -p` | $0.73/次 |
| 加 `--setting-sources ""`（砍 110k cache read）+ `--system-prompt` + `--allowedTools ""` | **$0.17/次** |
| haiku 研究员 | $0.13–0.27 |
| 带推理链的投票 | $0.26–0.53 |
| **整轮（研究+3票+决策）** | **约 $1.19** |

模型串：`claude-haiku-4-5` / `claude-fable-5` / `claude-opus-4-8` / `gpt-5.6-sol`
全部实测可用。**注意 `total_cost_usd` 是"走 API 会花多少"的折算——走订阅它不是
真实支出**，别拿它去减交易盈亏（那是虚拟单位减真实单位）。

---

## 为什么不复用共享的 CC/Codex 引擎

**是正确性问题，不是性能。** 用户另有一套 headless CC/Codex 服务（`xia-engine`，
端口 8900/8901）。不走它是因为：

- 那种引擎一个生命期一个活跃 thread，调用方不传更新的 epoch 就**复用别人正在用的
  对话** —— 交易 prompt 会被追加进去，对方下一轮就读到了
- 引擎的锁会把两边的轮次串行化（Codex 那边 turn 最长 230s）
- 他们**已经踩过一次**：往共享引擎塞低成本后台调用，结果把用户的助手冻住了

所以 `llm.py` 每次投票起独立的一次性子进程：无 session、无 resume、无共享 thread、
无锁，崩了只崩自己。**别为了"省一次进程启动"把它改回走共享引擎。**

---

## 待用户决定（别自己拍板）

1. **LICENSE** —— 没加。协议是用户的选择。
2. **默认分支是 `master`** —— GitHub 惯例是 `main`，要不要改。
3. **公开与否** —— 目前是私库。公开前必读下一节。
4. **目标机上一个 `:3000` 的遗留服务** —— 用户曾让清，核实后发现是**活着的 web
   服务**（不是死进程），已上报，用户还没发话。**别自作主张杀它。**

---

## 公开前必须处理的

这个库现在是**私有**的。翻公开之前：

1. **25 处引用泄露用户的私有项目。** 代码注释里到处是 `夏`、`mory`、
   `cc_engine.py:1101`、`xia-engine` —— 不是密钥，但它们把一个私有项目的架构、
   一个私人助手的存在、具体的 bug 史讲给全世界。而这些恰恰是最有价值的注释
   （设计决策的依据）。**取舍**：删掉则代码变弱；泛化成"某个共享 LLM 引擎"
   可保住理由、丢掉专有名词。后者更好。

   ```bash
   grep -rniE "夏|mory|xia-engine|cc_engine|codex_engine|guardian\.py" \
     --include="*.md" --include="*.py" --include="*.yaml" .
   ```

2. **"吃订阅不走 API" 是这个项目的标题卖点。** `claude -p` 是文档化功能，
   CI/自动化很常见；但"7×24 无人值守跑交易 bot"和"个人使用"之间是灰区。
   **把这个技巧当卖点公开发布，跟私下自己用不是一回事** —— 它会让这条路被注意到、
   可能被堵，而署名的是用户。这是用户自己要权衡的，不是你能替他决定的。

3. **观测台无鉴权。** 已改成默认 `127.0.0.1`。systemd unit 里显式传 `--host 0.0.0.0`
   （用户在 tailscale 里）。**别把默认改回 0.0.0.0。**

4. **它从没真正交易过。** 公开一个叫"交易 bot"但零结算记录的东西，会让人以为
   它经过验证。README 里写实了，别在别处吹。

5. **Polymarket ToS 限制美国用户**通过 API/agent 交易。工具本身中立，但 README
   里那条提示别删。

---

## 快速上手

```bash
git clone <repo> && cd polyvote
cp deploy/pi.env.example deploy/pi.env   # 填坐标,值找用户要
bash deploy/push.sh                       # 部署到目标机

# 目标机上:
cd ~/polyvote
.venv/bin/python tests/test_settle.py     # 应该 18 个用例全过
.venv/bin/python run.py --once            # 跑一轮 (烧约 $1.19×3 配额)
.venv/bin/python run.py --status          # 看数据
```

想省配额调试，先把 `max_markets_per_round` 压到 1。

**默认 `dry_run: true`，不会碰钱。开实盘要同时改 `config.yaml` 并设
`POLYVOTE_PRIVATE_KEY` —— 少一个都会被 `config.py` 拒绝启动。**
