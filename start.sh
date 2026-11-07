#!/usr/bin/env bash
set -u

cd "$(dirname "$0")" || exit 1

# Activate virtualenv if present
if [ -f "venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    . venv/bin/activate
fi

python main.py
status=$?

if [ $status -ne 0 ]; then
    echo
    echo "An error occurred (exit code $status). Press Enter to exit."
    read -r _
    exit $status
fi