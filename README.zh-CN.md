<p align="center">
  <a href="README.md"><img alt="English" src="https://img.shields.io/badge/English-555555?style=for-the-badge"></a>
  <a href="README.zh-CN.md"><img alt="简体中文" src="https://img.shields.io/badge/%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-1f6feb?style=for-the-badge"></a>
  <a href="README.ja.md"><img alt="日本語" src="https://img.shields.io/badge/%E6%97%A5%E6%9C%AC%E8%AA%9E-555555?style=for-the-badge"></a>
</p>

# polyvote

> 本来打算试一下poly市场的，可惜似乎vps的延迟问题一直没有解决，以后有机会还会重启项目。

**状态：暂停（2026-10-13）**。所有服务都已停止，部署用的 VPS 也已关机。全程只跑**模拟盘**（shadow），没读过钱包私钥，也从没真实下单。

---

## 这是什么

polyvote 是一个在 [Polymarket](https://polymarket.com) 预测市场上做自动交易实验的项目，前后有两个阶段。

### 阶段一：慢市场，多模型投票（已退役）

面向普通事件盘（选举、体育、新闻之类），用几个大模型"开会"判断概率：

```
Haiku 查一次资料  →  三个模型各自独立估价  →  决策模型拍板  →  代码层硬风控  →  下单
  (唯一能联网)        Claude Opus     ┐
                      Claude Fable    ├─ 看不到市场价格，也不能用工具
                      GPT (codex)     ┘
```

- 投票模型对价格**致盲**，避免它们只是复读市场价。
- 校准规则改编自 [valory-xyz/mech-predict](https://github.com/valory-xyz/mech-predict) 的 superforecaster prompt。
- 不走 LLM API，用 `claude` / `codex` CLI 起一次性子进程，用订阅额度。
- `risk.py` 是硬闸门：模型说什么都不算数，任何一条不过就整单拒绝。

2026-07-22 退役，转向快市场。设计细节和踩过的坑见 [docs/slow-market-design.zh-CN.md](docs/slow-market-design.zh-CN.md)。

### 阶段二：快市场 Fast V2（主体部分）

面向 Polymarket 的 **5 分钟加密货币涨跌盘**（BTC、ETH、SOL、XRP），每个资产一个独立引擎，由事件驱动：

```
RTDS Chainlink / Binance 价格 ─┐
                              ├─> 时间戳校验 ─> 信号模型 ─> 硬性闸门
CLOB WebSocket ─> 本地订单簿 ──┘                            │
                                                           ├─> SQLite 意向单 + 冻结现金
                                                           ├─> 按实测延迟模拟到达时的盘口
                                                           ├─> 逐档 FAK 吃单（成交 / 部分成交 / 拒单）
                                                           └─> 以官方结算结果对账
```

- **本地订单簿**：全量快照加增量更新，遇到旧事件、交叉盘口或失同步一律拒绝。
- **现金账本**：SQLite WAL。下意向单时先冻结现金，只有真正成交才建仓。结算是幂等事务，重启不会重置。
- **延迟拆解**：分开记录决策计算、HTTP 往返、event loop 排队和平台的 `seconds_delay`，统一按 P95 汇报，不拿最好的那一次说事。
- **实验设计**：一个冠军策略，配一个用归一化 $5 订单持续收样本的 observer，再加上冻结参数的 holdout 挑战者。自动复盘只能提议改动，不能直接改正在跑的策略，也永远不能切换到实盘。
- **看板**：`fast_dashboard.html`，显示账户、订单生命周期、延迟拆解和复盘结果；数据过期 2 秒就全屏告警。

部署在 AWS Lightsail 都柏林节点（$5/月，2 vCPU，512 MB），离 Polymarket 的 CLOB 比较近。

## 结果与结论

数据截至 2026-08-01，最后一次完整统计：

| 指标 | 结果 |
|---|---|
| observer 模拟成交（已官方结算） | 8,713 笔 |
| 扣除费用、滑点、延迟后的净盈亏 | **−$2,097**（ROI −5.1%） |
| 每笔盈亏 95% 置信区间 | [−$0.32, −$0.16]，整个区间都为负 |
| 平均自信概率 vs 实际胜率 | 70.9% vs 59.4%，高估约 11.5 个百分点 |
| 可控路径延迟 P95（目标 < 100 ms） | 最后一次完整测量为 383–385 ms；同一进程在 83 ms 到 848 ms 之间来回跳 |
| 冠军账本（有限资金） | $30.00 → $7.79，触发风控后永久锁定 |
| 两个 holdout 挑战者 | 都在各自的亏损闸门处停机 |

**延迟**：代码层面能排查的点基本都排除了，包括线程池排队、IPv4/IPv6、写盘放大和连接老化。剩下的主要是这台 $5 共享主机的调度问题：宿主 CPU steal 常在 50–84%。安静时段能做到 83 ms 左右，说明"能达标"，但始终做不到稳定达标。

**另一半问题**：复盘还发现策略本身对胜率系统性过度自信。它看到的 4 个百分点 edge，小于约 11.5 个百分点的校准偏差。所以重启时，除了换一台延迟稳定的机器，信号模型的概率校准也要一起重做。

2026-08-15 停掉服务，2026-10-13 VPS 关机，项目暂停。

## 目录结构

| 路径 | 内容 |
|---|---|
| `fastbot.py` | 单资产快市场引擎：事件驱动决策、预热下一个市场、模拟延迟成交、官方结算 |
| `fastmulti.py` | 把四个资产的引擎作为一个组合统一监管运行 |
| `fastchallenger.py` | 冻结参数的 holdout 挑战者 |
| `fastdash_server.py`、`fast_dashboard.html` | 快市场看板 |
| `polyvote/fast*.py` | 订单簿、信号策略、现金账本、observer、复盘、状态写入等模块 |
| `fast_*.yaml` | 快市场组合与挑战者配置 |
| `run.py`、`dashboard.py`、`polyvote/{brain,desk,risk,…}.py` | 阶段一慢市场（已退役） |
| `deploy/` | systemd user unit、部署脚本、SSH 隧道脚本 |
| `tests/` | 单元测试 |

## 运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-fast.txt -r requirements.txt pytest
.venv/bin/python -m pytest tests -q

# 快市场（模拟盘）
.venv/bin/python fastmulti.py --portfolio fast_portfolio.yaml
.venv/bin/python fastdash_server.py --host 127.0.0.1 --port 8920 --state-dir .
```

快市场代码里没有实盘下单的路径。已退役的慢市场 `execute.py` 有，需要同时设 `dry_run: false` 并通过环境变量提供私钥才会启用。看板没有任何鉴权，只能监听 `127.0.0.1`，要从别处看就走 SSH 隧道。服务器部署方式见 `deploy/` 下的 unit 文件。

## 文档

文档大多是中文，是开发过程中的工作记录：

- [FAST_MARKET.md](FAST_MARKET.md)：快市场运行方案、资金规则和评估门槛
- [FAST_MARKET_MEMORY.md](FAST_MARKET_MEMORY.md)：最终状态和完整结论
- [FAST_MARKET_RESEARCH_LOG.md](FAST_MARKET_RESEARCH_LOG.md)：逐轮研究日志，包括延迟归因、校准实验和下线复盘
- [docs/slow-market-design.zh-CN.md](docs/slow-market-design.zh-CN.md)：慢市场的设计和踩坑记录

`AGENTS.md`、`CLAUDE.md`、`HANDOFF.md` 是写给开发时用的 AI 编程助手看的工作指令，原样保留。

## 免责声明

这是个人研究项目，不构成任何投资建议。Polymarket 的服务条款限制美国用户通过 API 或 agent 交易，使用前请先确认你所在地区的规定。

## 许可证

[MIT](LICENSE)，随便用。
