#!/usr/bin/env bash
# Installs deps and a systemd service that starts on boot. Debian/Ubuntu.
set -e
cd "$(dirname "$0")"
sudo apt-get install -y snmp python3-venv
python3 -m venv .venv && .venv/bin/pip install -q fastapi uvicorn
sudo tee /etc/systemd/system/data-tracker.service >/dev/null <<UNIT
[Unit]
Description=Router data tracker
After=network-online.target
Wants=network-online.target
[Service]
User=$USER
WorkingDirectory=$PWD
ExecStart=$PWD/.venv/bin/python app.py
Restart=always
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now data-tracker
echo "Running: http://localhost:8181"
