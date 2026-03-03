#!/bin/bash
# ─────────────────────────────────────────────────────────────────
# build.sh  —  One-click builder for Kiv's RSI Live (macOS / Linux)
# Run from the folder containing app.py and kiv_rsi_live.spec
# ─────────────────────────────────────────────────────────────────

set -e   # stop on any error

echo ""
echo "  ========================================="
echo "   KIV RSI LIVE  —  Binary Builder"
echo "  ========================================="
echo ""

# 1. PyInstaller
echo "[1/3] Installing PyInstaller..."
pip install --upgrade pyinstaller

# 2. App deps
echo ""
echo "[2/3] Installing app dependencies..."
pip install yfinance pandas flask requests

# 3. Build
echo ""
echo "[3/3] Building binary with PyInstaller..."
pyinstaller kiv_rsi_live.spec --clean --noconfirm

echo ""
echo "  ========================================="
echo "   BUILD COMPLETE"
echo "   Output: dist/KivRSILive"
echo "  ========================================="
echo ""
