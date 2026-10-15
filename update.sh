#!/bin/bash
# UtilPortal — Deploy latest from GitHub main branch
# Usage: sudo bash update.sh

set -e

echo ""
echo "=========================================="
echo "  UtilPortal — Pulling latest from GitHub"
echo "=========================================="
echo ""

cd /opt/utilportal

echo "[1/5] Fetching all branches from origin..."
git fetch --all

echo "[2/5] Resetting to origin/main (discarding local changes)..."
git reset --hard origin/main

echo "[3/5] Stopping utilportal service, installing dependencies, and running database migration..."
systemctl stop utilportal
source venv/bin/activate
pip install -r requirements.txt
python migrate_db.py
deactivate

# ---------- Swap Memory Configuration ----------
# Many cloud servers have 0 swap by default. When memory spikes, Linux OOM-kills Gunicorn.
if [ $(swapon --show | wc -l) -le 1 ]; then
    echo "No swap detected. Creating 2G swapfile to prevent OOM kills..."
    if [ ! -f /swapfile ]; then
        fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
        chmod 600 /swapfile
        mkswap /swapfile
    fi
    swapon /swapfile 2>/dev/null || true
    if ! grep -q '/swapfile' /etc/fstab; then
        echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
    echo "2G swapfile enabled successfully."
fi

# ---------- Systemd Service & Gunicorn Tuning ----------
SERVICE_FILE="/etc/systemd/system/utilportal.service"

if [ -f "$SERVICE_FILE" ]; then
    echo "[4/5] Tuning systemd service and Gunicorn..."

    # 1. Ensure unlimited timeout (--timeout 0)
    if grep -q "\-\-timeout" "$SERVICE_FILE"; then
        sed -i 's/--timeout [0-9]*/--timeout 0/g' "$SERVICE_FILE"
    else
        sed -i 's|gunicorn |gunicorn --timeout 0 |g' "$SERVICE_FILE"
    fi

    # 2. Add max-requests to periodically recycle worker memory and avoid leaks
    if ! grep -q "\-\-max-requests" "$SERVICE_FILE"; then
        sed -i 's|gunicorn |gunicorn --max-requests 500 --max-requests-jitter 50 |g' "$SERVICE_FILE"
    fi

    # 3. Ensure automatic restart if service crashes or is killed by OOM
    if ! grep -q "Restart=" "$SERVICE_FILE"; then
        sed -i '/\[Service\]/a Restart=always\nRestartSec=3' "$SERVICE_FILE"
    elif grep -q "Restart=no" "$SERVICE_FILE"; then
        sed -i 's/Restart=no/Restart=always/g' "$SERVICE_FILE"
    fi

    systemctl daemon-reload
fi
# -------------------------------------------------------

echo "[5/5] Restarting utilportal service..."
systemctl restart utilportal

echo ""
echo "=========================================="
echo "  Done! Portal restarted successfully."
echo "  Gunicorn worker timeout: UNLIMITED"
echo "=========================================="
echo ""
