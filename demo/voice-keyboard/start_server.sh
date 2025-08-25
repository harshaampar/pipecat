#!/bin/bash

# Meeting Assistant Server Startup Script

echo "🎙️ Starting Meeting Assistant Server..."
echo "======================================"

# Check if we're in the right directory
if [ ! -f "meeting_server.py" ]; then
    echo "❌ Error: meeting_server.py not found!"
    echo "   Make sure you're in the meeting-assistant directory"
    exit 1
fi

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "📦 Creating Python virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "🔄 Activating virtual environment..."
source venv/bin/activate

# Install requirements
echo "📥 Installing requirements..."
pip install -q -r requirements.txt

# Check environment variables
echo "🔍 Checking environment variables..."

if [ -z "$DEEPGRAM_API_KEY" ]; then
    echo "⚠️  DEEPGRAM_API_KEY not set!"
    echo "   Please set your Deepgram API key:"
    echo "   export DEEPGRAM_API_KEY=\"your_api_key_here\""
    echo ""
fi

# Get server IP for ESP32 configuration
SERVER_IP=$(hostname -I | awk '{print $1}' 2>/dev/null || ifconfig | grep -E "inet.*broadcast" | awk '{print $2}' | head -1)

if [ -n "$SERVER_IP" ]; then
    export HOST_IP="$SERVER_IP"
    echo "🌐 Server IP: $SERVER_IP"
    echo "   Configure your ESP32 with:"
    echo "   export PIPECAT_SMALLWEBRTC_URL=\"http://$SERVER_IP:8765/api/offer\""
    echo ""
fi

# Run tests first
echo "🧪 Running server tests..."
python test_server.py

if [ $? -ne 0 ]; then
    echo "❌ Tests failed! Please check the errors above."
    exit 1
fi

echo ""
echo "🚀 Starting Meeting Assistant Server..."
echo "   Web Interface: http://${SERVER_IP:-localhost}:8765"
echo "   Press Ctrl+C to stop"
echo ""

# Start the server
python meeting_server.py --host 0.0.0.0 --port 8765