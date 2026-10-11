#!/usr/bin/env bash
# One-time setup on an Ubuntu/Debian server. Run as root:  sudo bash install.sh
set -euo pipefail
DIR=/opt/yt-agent
apt-get update -qq
apt-get install -y -qq python3 python3-venv ffmpeg curl ca-certificates docker.io
# Real Node (not Debian's "nodejs" without a `node` binary) for the JS challenge.
if ! command -v node >/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y -qq nodejs
fi
mkdir -p "$DIR"
cp agent.py requirements.txt "$DIR"/
[ -f "$DIR/agent.env" ] || cp agent.env.example "$DIR/agent.env"
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade -r "$DIR/requirements.txt"
# PO Token sidecar
docker rm -f bgutil-provider >/dev/null 2>&1 || true
docker run -d --name bgutil-provider --restart unless-stopped -p 127.0.0.1:4416:4416 --init \
  brainicism/bgutil-ytdlp-pot-provider
cat > /etc/systemd/system/yt-agent.service <<UNIT
[Unit]
Description=YouTube tracker download agent
After=network-online.target docker.service

[Service]
WorkingDirectory=$DIR
EnvironmentFile=$DIR/agent.env
Environment=PATH=$DIR/venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$DIR/venv/bin/python $DIR/agent.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
UNIT
# Keep yt-dlp fresh — YouTube changes often.
echo "0 4 * * * root $DIR/venv/bin/pip install -q -U 'yt-dlp[default]' && systemctl restart yt-agent" > /etc/cron.d/yt-agent-update
systemctl daemon-reload
systemctl enable yt-agent
echo
echo "Done. Edit $DIR/agent.env (TRACKER_URL, AGENT_TOKEN), put cookies in $DIR/cookies.txt, then:"
echo "  systemctl restart yt-agent && journalctl -u yt-agent -f"
