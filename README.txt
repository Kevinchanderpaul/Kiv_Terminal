# THE KIV TERMINAL - Build Instructions

## What's Included

  KivTerminal_BUILD/
  ├── app.py                  # Main Python application
  ├── build.bat               # Windows build script
  ├── build.sh                # Linux/macOS build script
  ├── KivTerminal.spec        # PyInstaller spec file
  ├── terminal.ico            # Application icon
  └── static/
      └── index.html          # Web interface

## What's New in This Version

  - 1M Scalp Signal Panel (EMA 9/21 cross + Volume Delta → BUY/SELL/WAIT)
  - Options Chain Panel (under News column):
      • Full calls & puts chain with strike, bid/ask, last, IV%, volume, OI
      • Greeks: delta, gamma, theta, vega (when available)
      • IV Rank and IV Percentile vs 52-week historical volatility
      • Put/Call ratio (volume + open interest)
      • Expiry dropdown — nearest by default, click to change
      • ATM strike highlighted with ◄ marker
      • ITM rows highlighted green (calls) / red (puts)

## Quick Start (Windows)

  1. Extract this zip to a folder
  2. Install Python 3.8+ from https://www.python.org/downloads/
     (check "Add Python to PATH")
  3. Double-click build.bat
  4. Exe is at: dist\KivTerminal.exe

## Quick Start (macOS/Linux)

  chmod +x build.sh && ./build.sh
  Executable at: dist/KivTerminal

## Run from Source (no build)

  pip install yfinance pandas numpy flask requests
  python app.py
  Open: http://127.0.0.1:7432

## Notes

  - Options data via Yahoo Finance (yfinance) — free, no API key needed
  - Options not available for all symbols (futures like ES=F have no options chain)
  - Greeks may be None for some symbols — panel gracefully hides those columns
