#!/usr/bin/env bash
set -u

echo "==================================================="
echo "       DeepSeek + Gemini Web2API - Dependency Installer"
echo "==================================================="
echo

# Pick a Python interpreter
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "[ERROR] Python is not installed or not in PATH!"
    echo "Please install Python 3.11+ and try again."
    exit 1
fi

echo "[*] Using $($PY --version 2>&1)"

# Create virtualenv if missing
if [ ! -d "venv" ]; then
    echo "[*] Creating virtual environment..."
    "$PY" -m venv venv
    if [ $? -ne 0 ]; then
        echo
        echo "[ERROR] Failed to create virtual environment."
        echo "You may need to install python3-venv (e.g. sudo apt install python3-venv)."
        exit 1
    fi
fi

# Activate it
# shellcheck disable=SC1091
. venv/bin/activate

echo "[*] Installing required packages..."
python -m pip install --upgrade pip >/dev/null 2>&1
python -m pip install -r requirements.txt

if [ $? -ne 0 ]; then
    echo
    echo "[ERROR] Installation failed."
    exit 1
fi

echo
echo "[OK] Installation complete!"
echo "You can now run ./start.sh to launch DeepSeek + Gemini Web2API."
echo