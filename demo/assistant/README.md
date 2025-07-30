# Pocket Voice Assistant Demo

A comprehensive voice assistant demonstration featuring real-time conversation, transcript logging, and web-based transcript viewing.

## Features

- **Real-time Voice Conversation**: Talk naturally with an AI assistant using ESP32 hardware
- **Automatic Transcript Logging**: All conversations are automatically recorded and stored
- **Live Transcript Viewing**: Watch conversations in real-time via web interface
- **Conversation History**: Browse and search through past conversations
- **Memory & Context**: Assistant remembers and references previous conversations
- **Transcript Correction**: AI-powered correction of transcription errors

## Architecture

### Server Components
- **assistant_server.py** - Main server handling WebRTC + web interface
- **transcript_manager.py** - Conversation logging and storage
- **conversation_memory.py** - Memory management and context retrieval
- **storage/** - SQLite database and JSON transcript files

### Client Components  
- **assistant-esp32-s3-box** - ESP32-S3-Box hardware client for voice I/O

## Quick Start

### 1. Install Dependencies

```bash
cd pipecat/demo/assistant
pip install -r requirements.txt

# Also ensure you have the main pipecat dependencies:
cd ../../
pip install -e ".[daily,deepgram,cartesia,openai,silero]"
```

### 2. Set Environment Variables

Create a `.env` file with your API keys:

```env
DEEPGRAM_API_KEY=your_deepgram_key
CARTESIA_API_KEY=your_cartesia_key  
OPENAI_API_KEY=your_openai_key
```

### 3. Start the Server

```bash
cd pipecat/demo/assistant
python assistant_server.py --host 0.0.0.0 --port 8765
```

The server provides:
- **WebRTC endpoint**: `ws://localhost:8765/ws` (for ESP32)
- **Web interface**: `http://localhost:8765` (for transcript viewing)

### 4. Connect ESP32 Client

Set environment variables for ESP32:
```bash
export WIFI_SSID="your_wifi_network"
export WIFI_PASSWORD="your_wifi_password"
export PIPECAT_SMALLWEBRTC_URL="ws://YOUR_SERVER_IP:8765/ws"
```

Flash and run the ESP32-S3-Box client:
```bash
cd ../../../pipecat-esp32/assistant-esp32-s3-box
idf.py build
idf.py flash
```

## Usage

### Voice Conversations
1. Connect ESP32-S3-Box to the server
2. Press the talk button and speak naturally
3. The assistant will respond with context from previous conversations
4. All dialogue is automatically logged

### Web Interface
1. Open `http://localhost:8765` in your browser
2. View live transcripts during conversations
3. Browse conversation history
4. Search through past conversations
5. Export transcripts as needed

## Data Storage

### Database Schema
- **conversations** table: Session metadata, timestamps, summaries
- **messages** table: Individual messages with original/corrected text

### File Structure
```
storage/
├── conversations.db          # SQLite database
└── transcripts/             # JSON transcript files
    ├── session_20240127_143022.json
    └── session_20240127_151534.json
```

## Configuration

### Server Options
```bash
python assistant_server.py --help

Options:
  --host TEXT        Host to bind to (default: 0.0.0.0)
  --port INTEGER     Port to bind to (default: 8765)  
  --reload           Enable auto-reload for development
```

### Memory Settings
Edit `conversation_memory.py` to adjust:
- `context_window`: Number of recent messages for LLM context
- `days_back`: How far to search in conversation history

## Development

### Adding New Features
1. Extend `TranscriptManager` for new storage capabilities
2. Modify `ConversationMemory` for enhanced context retrieval  
3. Update web interface in `assistant_server.py`
4. Add new API endpoints as needed

### Testing
```bash
# Test transcript manager
python -m pytest tests/test_transcript_manager.py

# Test conversation memory  
python -m pytest tests/test_conversation_memory.py
```

## Troubleshooting

### Common Issues
1. **ESP32-S3-Box connection fails**: Check Wi-Fi credentials and server IP
2. **No transcriptions**: Verify Deepgram API key
3. **Assistant not responding**: Check OpenAI API key
4. **Audio issues**: Verify Cartesia API key

### Debugging
- Check server logs: `python assistant_server.py --reload`
- Monitor ESP32-S3-Box: `idf.py monitor`  
- Inspect database: Use any SQLite browser on `storage/conversations.db`

## API Reference

### REST Endpoints
- `GET /` - Main web interface
- `GET /api/conversations` - List all conversations
- `GET /api/conversations/{session_id}` - Get specific conversation
- `GET /conversation/{session_id}` - View conversation in browser

### WebSocket Endpoints  
- `ws://host:port/ws` - WebRTC for ESP32-S3-Box clients
- `ws://host:port/transcript-stream` - Live transcript updates for web interface

## License

Same as parent Pipecat project - BSD 2-Clause License