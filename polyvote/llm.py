"""一次性 LLM 调用层。

刻意不复用 mory 的 xia-engine (127.0.0.1:8900/8901),原因是正确性而非性能:

  codex_engine.py:128  —— "一个 engine 生命期一个活跃 thread"
  codex_engine.py:300-305 —— 调用方不传更新的 epoch 就直接复用 self._thread_id

也就是说走 :8901 会把交易 prompt 追加进夏正在用的对话 thread,她下一轮就读到了。
cc_engine.py:64 的 _lock 还会把两边的轮次串行化 —— cc_engine.py:1402 记着上次
往共享引擎里塞后台调用的下场:"夏看着卡死"。

所以这里每次投票起一个独立的一次性子进程:无 session、无 resume、无共享 thread、
无锁,崩了只崩自己。

两个引擎的结构化输出都是实测确认过的 (2026-07-17, Pi, claude 2.1.179 / codex-cli 0.144.3):
  claude: --json-schema '<内联JSON>' → 结果在 --output-format json 信封的 structured_output 字段
          (注意:不是 result 字段,result 里是散文)
  codex:  --output-schema <文件路径> → 结果是 stdout 最后一个 JSON 行
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class LLMError(Exception):
    """引擎调用失败。调用方应当把该投票人记为弃权,而不是让整轮崩掉。"""


@dataclass
class Usage:
    """真实 token 消耗。

    订阅制下 cost_usd 只是"走 API 会花多少"的折算,规划配额时没用 ——
    真正的约束是 token 和限流窗口。所以这里把四个数原样记下来。

    cache_write 是这套一次性调用的特有开销:每次调用都是全新 session,
    写进去的缓存下一次能不能读到取决于 TTL 和调用间隔。间隔长过 TTL
    就是纯浪费 —— 写了从没人读。
    """
    input_tokens: int = 0
    cache_write: int = 0     # cache_creation_input_tokens
    cache_read: int = 0      # cache_read_input_tokens
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.cache_write + self.cache_read + self.output_tokens

    @property
    def billable_equiv(self) -> float:
        """按计费权重折算的等效 token。

        cache write 约 1.25×,cache read 约 0.1×,普通 input 1×,output 另算。
        用来横向比较不同调用式的真实开销 —— 一次 15k 的 cache write 和一次
        15k 的 cache read 差 12 倍。
        """
        return (
            self.input_tokens
            + self.cache_write * 1.25
            + self.cache_read * 0.1
            + self.output_tokens
        )


@dataclass
class LLMResult:
    data: dict[str, Any]
    model: str
    engine: str
    cost_usd: float | None = None
    duration_ms: int | None = None
    usage: Usage = field(default_factory=Usage)
    rate_limit: dict[str, Any] | None = None


def _check_bin(binary: str, engine: str) -> None:
    if shutil.which(binary) is None:
        raise LLMError(
            f"{engine} 的可执行文件 {binary!r} 不在 PATH 上。"
            f"(应在 /usr/bin/{binary};本机开发请确认已安装并登录)"
        )


def _spawn_env() -> dict[str, str]:
    """给子进程的环境。

    ENABLE_PROMPT_CACHING_1H=1 —— 把 prompt 缓存 TTL 从 5 分钟拉到 1 小时。
    没有对应的 CLI flag,claude 自己读这个变量,只能靠环境继承传进去。

    这个和 loop.interval_minutes=45 是**一对**,别单独改其中一个:
      45 分钟 < 60 分钟 TTL  →  上一轮写的缓存,这一轮还能读到
      间隔 > TTL             →  每轮都是冷启,cache_write 白付,从没人读
    (同样的约束在别处见过:某个项目的保活是 50 分钟,注释写着"务必 < 60")

    cache_read 约 0.1×,cache_write 约 1.25-2× —— 差一个数量级,所以这不是
    微优化。观测台的命中率仪表就是盯这个的。
    """
    return {**os.environ, "ENABLE_PROMPT_CACHING_1H": "1"}


def call_claude(
    prompt: str,
    *,
    model: str,
    schema: dict[str, Any],
    system: str,
    effort: str | None = None,
    timeout_s: int = 180,
    binary: str = "claude",
    tools: list[str] | None = None,
    max_turns: int = 8,
) -> LLMResult:
    """一次性调用 Claude Code,返回 schema 校验过的结构化结果。

    那几个降本参数不是可选的装饰,是实测出来的 (2026-07-17, Pi):
      默认调用                      $0.73/次  (110k cache read + 6 轮 agentic 循环)
      + --setting-sources ""        → 砍掉 110k cache read
      + --system-prompt             → 换掉 Claude Code 的 agentic 系统提示
      + --allowedTools ""           → cache_creation 15.7k → 3.5k
      = $0.17/次

    max_turns 的坑 (踩过两次):
      1. --json-schema 是靠工具调用实现的。给 1 直接 error_max_turns
         (stop_reason=tool_use) —— 结构化输出还没来得及发。
      2. 更阴的:allowedTools 为空时模型**仍会尝试**调用 WebSearch,
         每次尝试烧一轮。给 3 的时候 fable-5 在真实市场问题上必挂:
         terminal_reason=max_turns, errors=["Reached maximum number of turns (3)"]
         而同样参数的 opus-4-8 却能过 —— 别拿单个模型的通过当验证。
      所以默认给 8,留足被拒重试的余量。
    """
    _check_bin(binary, "claude")
    argv = [
        binary,
        "-p",
        prompt,
        "--model",
        model,
        "--system-prompt",
        system,
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--json-schema",
        json.dumps(schema),
        "--max-turns",
        str(max_turns),
        "--output-format",
        "json",
    ]
    # tools=None → 一个工具都不给 (最省,但模型会盲猜)
    # tools=[...] → 只给这些
    argv += ["--allowedTools", ",".join(tools) if tools else ""]
    if effort:
        argv += ["--effort", effort]

    try:
        p = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
            env=_spawn_env(),
        )
    except subprocess.TimeoutExpired as e:
        raise LLMError(f"claude {model} 超时 ({timeout_s}s)") from e

    if p.returncode != 0:
        # 失败时 stdout 常常比 stderr 有用得多 (CLI 会把错误信封打到 stdout,
        # stderr 是空的)。两个都带上,否则排查时只能看到一个光秃秃的 "退出码 1"。
        raise LLMError(
            f"claude {model} 退出码 {p.returncode}"
            f" | stderr: {p.stderr[-300:].strip() or '(空)'}"
            f" | stdout: {p.stdout[-300:].strip() or '(空)'}"
        )

    try:
        env = json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise LLMError(f"claude {model} 信封不是 JSON: {p.stdout[:300]!r}") from e

    if env.get("is_error"):
        raise LLMError(
            f"claude {model} 报错: subtype={env.get('subtype')} "
            f"stop={env.get('stop_reason')} result={str(env.get('result'))[:200]}"
        )

    data = env.get("structured_output")
    if data is None:
        raise LLMError(
            f"claude {model} 没返回 structured_output "
            f"(stop_reason={env.get('stop_reason')}, turns={env.get('num_turns')}) "
            f"—— 通常是 --max-turns 太小把 schema 工具调用掐断了"
        )

    u = env.get("usage") or {}
    return LLMResult(
        data=data,
        model=model,
        engine="claude",
        cost_usd=env.get("total_cost_usd"),
        duration_ms=env.get("duration_ms"),
        usage=Usage(
            input_tokens=int(u.get("input_tokens") or 0),
            cache_write=int(u.get("cache_creation_input_tokens") or 0),
            cache_read=int(u.get("cache_read_input_tokens") or 0),
            output_tokens=int(u.get("output_tokens") or 0),
        ),
        # 订阅限流信息。CLI 有时会带,带了就记 —— 这是唯一能看到
        # "还剩多少窗口"的地方。
        rate_limit={
            k: env[k]
            for k in ("rate_limit_type", "rate_limit_status", "rate_limit_reset")
            if k in env
        }
        or None,
    )


def _last_json_line(stdout: str) -> dict[str, Any] | None:
    """从后往前扫,找第一个能解析成 JSON 对象的行。

    codex exec 会在 JSON 前面打一堆 banner (workdir/model/session id/tokens used),
    结构化结果是最后一行。从后往前扫比 tail -1 稳,免得尾部多个空行就崩。
    """
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


_CODEX_TOK_RE = re.compile(r"tokens used[:\s]+([\d,]+)", re.IGNORECASE)


def _codex_tokens(stdout: str) -> Usage:
    """从 codex exec 的 banner 里抠 token 数。

    codex exec 只打一个 "tokens used  12,006" 的合计,不像 claude 那样给
    input/cache/output 的分项。所以只能记总数,归到 input_tokens 上 ——
    宁可记一个粗数,也别把 Codex 的消耗当成 0。
    """
    m = _CODEX_TOK_RE.search(stdout)
    if not m:
        return Usage()
    try:
        return Usage(input_tokens=int(m.group(1).replace(",", "")))
    except ValueError:
        return Usage()


def call_codex(
    prompt: str,
    *,
    model: str,
    schema: dict[str, Any],
    system: str,
    effort: str | None = None,
    timeout_s: int = 240,
    binary: str = "codex",
) -> LLMResult:
    """一次性调用 Codex,返回 schema 校验过的结构化结果。

    --ephemeral         不落 session 文件 (不污染 ~/.codex/sessions)
    --skip-git-repo-check  临时目录不是 git repo
    -s read-only        投票器不需要写任何东西
    -C <dir>            工作根目录,同时也是 schema 文件的落点
    """
    _check_bin(binary, "codex")
    full_prompt = f"{system}\n\n{prompt}" if system else prompt

    with tempfile.TemporaryDirectory(prefix="polyvote-codex-") as workdir:
        schema_path = Path(workdir) / "schema.json"
        schema_path.write_text(json.dumps(schema), encoding="utf-8")

        argv = [
            binary,
            "exec",
            full_prompt,
            "-m",
            model,
            "--output-schema",
            str(schema_path),
            "--ephemeral",
            "--skip-git-repo-check",
            "-s",
            "read-only",
            "-C",
            workdir,
        ]
        if effort:
            argv += ["-c", f"model_reasoning_effort={effort}"]

        try:
            p = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                stdin=subprocess.DEVNULL,
                env=_spawn_env(),
            )
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"codex {model} 超时 ({timeout_s}s)") from e

        if p.returncode != 0:
            raise LLMError(
                f"codex {model} 退出码 {p.returncode}: {p.stderr[-400:].strip()}"
            )

        data = _last_json_line(p.stdout)
        if data is None:
            raise LLMError(
                f"codex {model} 的 stdout 里找不到 JSON 结果: {p.stdout[-400:]!r}"
            )
        usage = _codex_tokens(p.stdout)

    return LLMResult(data=data, model=model, engine="codex", usage=usage)


_DISPATCH = {"claude": call_claude, "codex": call_codex}


def call(
    engine: str,
    prompt: str,
    *,
    model: str,
    schema: dict[str, Any],
    system: str,
    effort: str | None = None,
    timeout_s: int | None = None,
    binary: str | None = None,
    tools: list[str] | None = None,
    max_turns: int | None = None,
) -> LLMResult:
    """按引擎名分发。engine/model/effort 全部来自 config.yaml。

    tools 只对 claude 生效。codex 的联网能力由它自己的沙箱策略决定,
    这里给的是 -s read-only,不额外开网。
    """
    try:
        fn = _DISPATCH[engine]
    except KeyError:
        raise LLMError(f"未知引擎 {engine!r},只支持 {sorted(_DISPATCH)}") from None

    kwargs: dict[str, Any] = {
        "model": model,
        "schema": schema,
        "system": system,
        "effort": effort,
    }
    if timeout_s is not None:
        kwargs["timeout_s"] = timeout_s
    if binary is not None:
        kwargs["binary"] = binary
    if engine == "claude":
        kwargs["tools"] = tools
        if max_turns is not None:
            kwargs["max_turns"] = max_turns
    return fn(prompt, **kwargs)
