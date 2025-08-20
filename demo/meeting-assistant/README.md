# Meeting Assistant Demo

A real-time meeting transcription system using Pipecat framework with ESP32-S3-Box-3 hardware client.

## Features

### Server Side
- Real-time speech-to-text transcription using Deepgram Nova-3
- Live web dashboard for viewing transcripts
- Meeting session management
- WebRTC audio input (no output)
- Meeting history and storage

### ESP32 Client
- Touch button to start/stop meetings
- Visual meeting states with pulsing indicator
- Meeting timer display
- WebRTC audio streaming to server
- Automatic reconnection handling

## Setup

### Server Requirements
1. Python 3.8+ with pipecat framework
2. Deepgram API key for speech-to-text
3. Required environment variables:
   ```
   DEEPGRAM_API_KEY=your_deepgram_api_key
   HOST_IP=your_server_ip_address
   ```

### ESP32 Requirements
1. ESP32-S3-Box-3 hardware
2. ESP-IDF development environment
3. WiFi credentials configured
4. Server endpoint URL configured

## Installation

### Server Setup
```bash
cd pipecat/demo/meeting-assistant
pip install -r requirements.txt
python meeting_server.py --host 0.0.0.0 --port 8765
```

### ESP32 Setup
```bash
cd pipecat-esp32/meeting-assistant-esp32-s3-box
# Set environment variables
export WIFI_SSID="your_wifi_ssid"
export WIFI_PASSWORD="your_wifi_password" 
export PIPECAT_SMALLWEBRTC_URL="http://your_server_ip:8765/api/offer"

# Build and flash
idf.py build
idf.py flash
```

## Usage

1. Start the server on your computer/server
2. Flash and boot the ESP32 device
3. Connect ESP32 to WiFi
4. Press "Start Meeting" button on ESP32
5. Speak into the ESP32 microphone
6. View live transcripts on the web dashboard at `http://server_ip:8765`
7. Press "Stop Meeting" to end the session

## Web Dashboard

The web interface provides:
- Live transcript streaming
- Meeting history and status
- Individual meeting transcript viewing
- Real-time meeting status updates

## Architecture

- **ESP32**: Captures audio and streams via WebRTC to server
- **Server**: Processes audio through Deepgram Nova-3 STT
- **Database**: SQLite storage for meeting metadata and transcripts
- **WebSocket**: Real-time transcript streaming to web clients

## Meeting Flow

1. ESP32 boots → WiFi connection → "Start Meeting" button
2. User presses start → WebRTC connection established
3. Audio streaming → Server transcription → Web dashboard updates
4. User presses stop → Connection closed → Meeting ended
5. Return to start state for next meeting