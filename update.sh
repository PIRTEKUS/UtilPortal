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

echo "[3/5] Running database migration..."
source venv/bin/activate
python migrate_db.py
deactivate

# ---------- Gunicorn timeout fix ----------
# Default Gunicorn timeout is 30s, which kills long-running Python modules.
# We patch the service file to set --timeout 0 (unlimited).
SERVICE_FILE="/etc/systemd/system/utilportal.service"

if grep -q "\-\-timeout" "$SERVICE_FILE"; then
    echo "[4/5] Gunicorn timeout already configured — ensuring it is 0..."
    sed -i 's/--timeout [0-9]*/--timeout 0/g' "$SERVICE_FILE"
else
    echo "[4/5] Patching Gunicorn to disable worker timeout (--timeout 0)..."
    sed -i 's|gunicorn |gunicorn --timeout 0 |g' "$SERVICE_FILE"
fi
systemctl daemon-reload
# ------------------------------------------

echo "[5/5] Restarting utilportal service..."
systemctl restart utilportal

echo ""
echo "=========================================="
echo "  Done! Portal restarted successfully."
echo "  Gunicorn worker timeout: UNLIMITED"
echo "=========================================="
echo ""
