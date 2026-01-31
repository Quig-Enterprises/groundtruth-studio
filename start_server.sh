#!/bin/bash
# Groundtruth Studio - Flask Server Startup Script

cd "$(dirname "$0")"

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
    echo "Installing dependencies..."
    venv/bin/pip install -r requirements.txt
fi

echo "Starting Groundtruth Studio..."
echo "Access at: http://localhost:5000"
echo "         or http://10.153.2.6:5000"
echo "         or http://10.100.2.18:5000"
echo ""
echo "Press Ctrl+C to stop"
echo ""

venv/bin/python app/api.py
