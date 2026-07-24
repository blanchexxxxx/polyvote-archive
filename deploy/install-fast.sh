#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ "$(awk '/MemTotal/ {print $2}' /proc/meminfo)" -lt 700000 ]] \
  && ! swapon --show=NAME --noheadings | grep -qx '/swapfile'; then
  if [[ ! -f /swapfile ]]; then
    sudo fallocate -l 1G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
  fi
  sudo swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab \
    || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

sudo apt-get update
sudo apt-get install -y python3-venv

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements-fast.txt

mkdir -p "$HOME/.config/systemd/user"
cp deploy/polyvote-fast.service "$HOME/.config/systemd/user/"
cp deploy/polyvote-fast-challenger.service "$HOME/.config/systemd/user/"
cp deploy/polyvote-fast-dashboard.service "$HOME/.config/systemd/user/"
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now \
  polyvote-fast.service \
  polyvote-fast-challenger.service \
  polyvote-fast-dashboard.service

echo "Fast shadow engine, finite-cash challenger and dashboard are running."
echo "Check: systemctl --user status polyvote-fast polyvote-fast-challenger polyvote-fast-dashboard"
echo "Tunnel: ssh -L 8920:127.0.0.1:8920 <server>"
echo "Open: http://127.0.0.1:8920/"
