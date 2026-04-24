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

echo "[1/4] Fetching all branches from origin..."
git fetch --all

echo "[2/4] Resetting to origin/main (discarding local changes)..."
git reset --hard origin/main

echo "[3/4] Running database migration..."
source venv/bin/activate
python migrate_db.py
deactivate

echo "[4/4] Restarting utilportal service..."
systemctl restart utilportal

echo ""
echo "=========================================="
echo "  Done! Portal restarted successfully."
echo "=========================================="
echo ""
