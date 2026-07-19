#!/usr/bin/env bash
# 部署到 Pi。用这个,别手打 tar。
#
# 为什么存在:手打 `tar czf - . | ssh ...` 时如果 cwd 不是项目根目录,
# 会把整个家目录打包传过去。这事真发生过 —— 6.5G 的 Windows 家目录
# (含 .ssh 私钥、.claude.json、.docker) 被传到了 Pi 的 ~/polyvote。
# 这里用绝对路径 + 白名单,cwd 是什么都不影响。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 部署坐标从 deploy/pi.env 读 (已 gitignore)。见 pi.env.example。
# 不写死在这里是因为 git 历史永久 —— 库翻公开时洗不掉。
[[ -f "$HERE/deploy/pi.env" ]] && source "$HERE/deploy/pi.env"

PI="${POLYVOTE_PI:-}"
KEY="${POLYVOTE_KEY:-$HOME/.ssh/id_ed25519}"
DEST="${POLYVOTE_DEST:-polyvote}"

if [[ -z "$PI" ]]; then
  echo "没设 POLYVOTE_PI。" >&2
  echo "  cp deploy/pi.env.example deploy/pi.env  然后填自己的值" >&2
  exit 1
fi

# 白名单。只传这些,不用 `.` —— 黑名单(--exclude)漏一个就是事故,
# 白名单漏一个只是少传个文件。
FILES=(
  config.yaml
  requirements.txt
  README.md
  run.py
  dashboard.py
  dashboard.html
  polyvote
  deploy
  tests
)

echo "从 $HERE"
echo "传到 $PI:~/$DEST"
echo

for f in "${FILES[@]}"; do
  [[ -e "$HERE/$f" ]] || { echo "!! 少了 $f —— 中止"; exit 1; }
done

ssh -i "$KEY" "$PI" "mkdir -p ~/$DEST"

tar czf - -C "$HERE" \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.db' \
  --exclude='*.log' \
  "${FILES[@]}" \
  | ssh -i "$KEY" "$PI" "cd ~/$DEST && tar xzf -"

echo "已传:"
printf '  %s\n' "${FILES[@]}"
echo
ssh -i "$KEY" "$PI" "cd ~/$DEST && du -sh . && echo && ls -a | grep -E '^\.' | grep -v -E '^\.\.?$|^\.venv$' && echo '!! 上面这些点文件不该在这 —— 可能又传串了' || echo '干净 (没有意外的点文件)'"

# 部署完自动重启观测台。它无状态(每个请求现读 DB/config),重启永远安全;
# 不重启的话进程里是旧 import 的 Python 类,读到新 config.yaml 会炸 ——
# 2026-07-19 实翻过车:Risk 加了新字段,观测台没重启,模型面板直接报
# ConfigError。bot 故意**不**在这里重启:它可能正在轮次中间,烧着钱的
# LLM 调用不该被部署打断 —— bot 重启永远手动挑安全窗口。
echo
echo "重启观测台 (无状态,安全) ..."
ssh -i "$KEY" "$PI" "systemctl --user restart polyvote-dashboard.service" \
  && echo "观测台已重启" \
  || echo "!! 观测台重启失败 —— 手动: systemctl --user restart polyvote-dashboard"
