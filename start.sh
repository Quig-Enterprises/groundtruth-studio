#!/bin/bash
# Quick start script for Video Archive System

cd "$(dirname "$0")"

echo "==================================="
echo "Video Archive System - Quick Start"
echo "==================================="
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is not installed"
    exit 1
fi

# Check dependencies
echo "Checking dependencies..."

if ! command -v ffmpeg &> /dev/null; then
    echo "Warning: FFmpeg is not installed. Thumbnail generation will not work."
    echo "Install with: sudo apt install ffmpeg"
fi

if ! command -v yt-dlp &> /dev/null; then
    echo "Warning: yt-dlp is not installed. Video downloads will not work."
    echo "Install with: pip install yt-dlp"
fi

# Install Python requirements
if [ -f requirements.txt ]; then
    echo "Installing Python dependencies..."
    pip install -q -r requirements.txt
fi

# Create directories
mkdir -p downloads thumbnails

# Make CLI executable
chmod +x download_cli.py

echo ""
echo "Starting Flask server..."
echo "Access the web interface at: http://localhost:5000"
echo ""
echo "Press Ctrl+C to stop the server"
echo ""

# Start the server
python3 app/api.py
