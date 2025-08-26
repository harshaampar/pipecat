import argparse
import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from deepgram import LiveOptions

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.network.fastapi_websocket import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.transports.network.small_webrtc import SmallWebRTCTransport
from pipecat.transports.network.webrtc_connection import SmallWebRTCConnection
from pipecat.transports.services.daily import DailyParams

load_dotenv(override=True)

# Global instances
app = FastAPI()
active_sessions: Dict[str, Dict] = {}
live_transcript_connections: List[WebSocket] = []

# WebRTC connections for ESP32 clients
webrtc_connections: Dict[str, SmallWebRTCConnection] = {}


def smallwebrtc_sdp_munging(sdp: str, host: str) -> str:
    """Apply SDP munging for ESP32 compatibility.
    
    Args:
        sdp: Original SDP string
        host: Host IP address
        
    Returns:
        Modified SDP string with ESP32-compatible settings
    """
    # Replace connection addresses with the host IP
    sdp = re.sub(r'c=IN IP4 \S+', f'c=IN IP4 {host}', sdp)
    
    # Ensure proper ICE candidate formatting for ESP32
    lines = sdp.split('\n')
    modified_lines = []
    
    for line in lines:
        if line.startswith('a=candidate:'):
            # Ensure host candidates come first for ESP32
            if 'host' in line:
                parts = line.split()
                if len(parts) >= 5:
                    # Replace IP with our host IP
                    parts[4] = host
                    line = ' '.join(parts)
            modified_lines.append(line)
        else:
            modified_lines.append(line)
    
    return '\n'.join(modified_lines)

# Mount static files
current_dir = Path(__file__).parent
app.mount("/static", StaticFiles(directory=current_dir / "static"), name="static")


class TextSenderProcessor(FrameProcessor):
    """Processor that sends transcribed text back to ESP32 via data channel"""
    
    def __init__(self, session_id: str, webrtc_connection: SmallWebRTCConnection):
        super().__init__()
        self.session_id = session_id
        self.webrtc_connection = webrtc_connection
        
    async def process_frame(self, frame, direction):
        """Process transcription frames and send text to ESP32"""
        # First, call the parent process_frame to handle StartFrame and other system frames
        await super().process_frame(frame, direction)
        
        # Then handle our specific transcription logic
        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            # Check if this is an interim or final transcription
            is_interim = getattr(frame, 'interim', False)
            
            if not is_interim:  # Only send final transcriptions to ESP32
                # Send text back to ESP32 via data channel with proper spacing
                text_with_space = frame.text.strip()
                if text_with_space:
                    # Always add space after each transcription segment to separate words
                    text_with_space += " "
                
                text_message = {
                    "type": "transcribed_text",
                    "text": text_with_space
                }
                
                try:
                    # Send text message via data channel - use the correct method
                    if hasattr(self.webrtc_connection, 'send_message'):
                        self.webrtc_connection.send_message(text_message)
                    elif hasattr(self.webrtc_connection, 'send_app_message'):
                        result = self.webrtc_connection.send_app_message(text_message)
                        if result is not None and hasattr(result, '__await__'):
                            await result
                    else:
                        # Fallback - try to send via data channel directly
                        import json
                        self.webrtc_connection.send_data_channel_message(json.dumps(text_message))
                    
                    logger.info(f"📤 Sent text to ESP32: {frame.text.strip()}")
                    
                    # Broadcast to web interface
                    await broadcast_session_update(self.session_id, {
                        "timestamp": datetime.now().isoformat(),
                        "text": frame.text,
                        "type": "transcription_sent",
                        "streaming": False
                    })
                    
                except Exception as e:
                    logger.error(f"❌ Error sending text to ESP32: {e}")
                    logger.debug(f"WebRTC connection type: {type(self.webrtc_connection)}")
                    logger.debug(f"Available methods: {dir(self.webrtc_connection)}")
            else:
                # For interim results, only broadcast to web interface
                await broadcast_session_update(self.session_id, {
                    "timestamp": datetime.now().isoformat(),
                    "text": frame.text,
                    "type": "transcription_interim",
                    "streaming": True
                })
        
        # Always push the frame downstream
        await self.push_frame(frame, direction)


# Transport configurations
transport_params = {
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=False,  # No audio output for voice keyboard
        vad_analyzer=SileroVADAnalyzer(),
    ),
}


async def create_voice_keyboard_pipeline(transport: BaseTransport, session_id: str, webrtc_connection: SmallWebRTCConnection) -> Tuple[PipelineTask, object]:
    """Create the voice keyboard pipeline"""
    
    # Initialize Deepgram STT with Nova-3 English model
    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        live_options=LiveOptions(
            model="nova-3",
            language="en",
            smart_format=True,
            interim_results=True,
            utterance_end_ms=1000,
            endpointing=300
        )
    )

    # Create text sender processor for this session
    text_sender = TextSenderProcessor(session_id, webrtc_connection)
    
    # Build pipeline - simplified for transcription + text sending
    pipeline = Pipeline([
        transport.input(),           # Transport user input (audio)
        stt,                        # Speech to text (Deepgram Nova-3)
        text_sender,               # Send text back to ESP32
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            allow_interruptions=True,
        ),
        cancel_on_idle_timeout=False,  # Don't cancel on idle timeout
    )
    
    return task, text_sender


async def run_voice_session(transport: BaseTransport, session_id: str, webrtc_connection: SmallWebRTCConnection):
    """Run a voice keyboard session"""
    logger.info(f"🚀 Starting voice keyboard session: {session_id}")
    
    # Create pipeline
    task, text_sender = await create_voice_keyboard_pipeline(transport, session_id, webrtc_connection)
    
    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"🔗 ESP32 client connected to session {session_id}")
        # Store transport reference for immediate stopping capability
        active_sessions[session_id] = {
            "start_time": datetime.now(),
            "client": client,
            "task": task,
            "transport": transport,
            "status": "active"
        }
        
        # Broadcast session start status
        await broadcast_session_update(session_id, {
            "timestamp": datetime.now().isoformat(),
            "type": "session_started",
            "message": "Voice typing session started"
        })

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"❌ ESP32 client disconnected from session {session_id}")
        
        # Broadcast disconnect status to web interface
        await broadcast_session_update(session_id, {
            "timestamp": datetime.now().isoformat(),
            "type": "session_ended", 
            "message": "Voice typing session ended - ESP32 disconnected"
        })
        
        # End session tracking and cleanup
        active_sessions.pop(session_id, None)
        logger.info(f"Session {session_id} cleaned up successfully")
        
        # Clean up
        if session_id in active_sessions:
            active_sessions[session_id]["status"] = "ended"
            del active_sessions[session_id]
            
        await task.cancel()

    # Run the pipeline
    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)


# WebSocket for live transcript streaming
@app.websocket("/transcript-stream")
async def transcript_stream(websocket: WebSocket):
    """WebSocket endpoint for live transcript streaming to web interface"""
    await websocket.accept()
    live_transcript_connections.append(websocket)
    
    try:
        while True:
            # Keep connection alive
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        live_transcript_connections.remove(websocket)


async def broadcast_session_update(session_id: str, message: Dict):
    """Broadcast session updates to all connected web clients"""
    if not live_transcript_connections:
        return
        
    update_data = {
        "session_id": session_id,
        "message": message
    }
    
    # Remove disconnected connections
    disconnected = []
    for websocket in live_transcript_connections:
        try:
            await websocket.send_json(update_data)
        except:
            disconnected.append(websocket)
    
    for ws in disconnected:
        live_transcript_connections.remove(ws)


# HTTP endpoint for ESP32 WebRTC negotiation
@app.post("/api/offer")
async def webrtc_offer(request: dict, background_tasks: BackgroundTasks):
    """Handle WebRTC offer requests from ESP32 clients."""
    logger.info(f"Received WebRTC offer from ESP32: {request.get('pc_id', 'unknown')}")
    
    pc_id = request.get("pc_id")
    
    # Check if we have an existing connection to reuse
    if pc_id and pc_id in webrtc_connections:
        webrtc_connection = webrtc_connections[pc_id]
        logger.info(f"Reusing existing WebRTC connection for pc_id: {pc_id}")
        
        await webrtc_connection.renegotiate(
            sdp=request["sdp"],
            type=request["type"],
            restart_pc=request.get("restart_pc", False),
        )
    else:
        # Create new WebRTC connection
        webrtc_connection = SmallWebRTCConnection()
        await webrtc_connection.initialize(
            sdp=request["sdp"], 
            type=request["type"]
        )
        
        # Generate session ID for this WebRTC connection first
        session_id = f"voice_session_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{webrtc_connection.pc_id}"
        
        # Handle ESP32 disconnect messages via data channel
        @webrtc_connection.event_handler("app-message")
        async def handle_app_message(conn: SmallWebRTCConnection, message: dict):
            """Handle application messages from ESP32 via data channel."""
            message_type = message.get("type")
            logger.info(f"📨 Received app message: {message}")
            
            if message_type == "debug":
                debug_msg = message.get("message", "Unknown debug message")
                timestamp = message.get("timestamp", 0)
                logger.info(f"🔧 ESP32 Debug: {debug_msg} (t={timestamp})")
                
                # Also broadcast debug messages to web interface for visibility
                await broadcast_session_update(session_id, {
                    "timestamp": datetime.now().isoformat(),
                    "type": "esp32_debug",
                    "message": f"ESP32: {debug_msg}",
                    "esp32_timestamp": timestamp
                })
                
            elif message_type == "session.disconnect":
                logger.info(f"🛑 Received explicit disconnect from ESP32 for session {session_id}")
                
                # Broadcast disconnect to web interface
                await broadcast_session_update(session_id, {
                    "timestamp": datetime.now().isoformat(),
                    "type": "session_ended",
                    "message": "Session ended - ESP32 disconnected gracefully"
                })
                
                # Stop the running session pipeline and transport immediately  
                session_data = active_sessions.get(session_id)
                if session_data:
                    # Close WebRTC connection first to signal disconnect to ESP32
                    if "webrtc_connection" in session_data:
                        webrtc_connection = session_data["webrtc_connection"]
                        logger.info(f"Closing WebRTC connection for {session_id}")
                        try:
                            await webrtc_connection.close()
                            logger.info(f"WebRTC connection closed for {session_id}")
                        except Exception as e:
                            logger.warning(f"Error closing WebRTC connection: {e}")
                    
                    # Stop the transport to stop audio frame processing
                    if "transport" in session_data:
                        transport = session_data["transport"]
                        logger.info(f"Stopping transport for {session_id}")
                        try:
                            from pipecat.frames.frames import EndFrame
                            # SmallWebRTCTransport requires stopping input and output separately
                            end_frame = EndFrame()
                            if hasattr(transport, 'input') and transport.input():
                                await transport.input().stop(end_frame)
                                logger.info(f"Transport input stopped for {session_id}")
                            if hasattr(transport, 'output') and transport.output():
                                await transport.output().stop(end_frame)
                                logger.info(f"Transport output stopped for {session_id}")
                            logger.info(f"Transport stopped successfully for {session_id}")
                        except Exception as e:
                            logger.warning(f"Error stopping transport: {e}")
                    
                    # Then stop the pipeline task
                    if "task" in session_data:
                        pipeline_task = session_data["task"]
                        if pipeline_task:
                            logger.info(f"Stopping pipeline task for {session_id}")
                            try:
                                await pipeline_task.stop_when_done()
                                logger.info(f"Pipeline task scheduled to stop for {session_id}")
                            except Exception as e:
                                logger.warning(f"Error stopping pipeline task: {e}")
                
                # End session tracking and cleanup
                active_sessions.pop(session_id, None)
                
                # Connection will be closed automatically by the transport
                logger.info(f"Session {session_id} ended via explicit disconnect message")
        
        # Handle connection cleanup
        @webrtc_connection.event_handler("closed")
        async def handle_webrtc_disconnected(conn: SmallWebRTCConnection):
            """Handle WebRTC connection closure and cleanup."""
            logger.info(f"🔌 WebRTC connection closed for pc_id: {conn.pc_id}")
            webrtc_connections.pop(conn.pc_id, None)
            
            # Also clean up session if it exists
            for session_id_key, session_data in list(active_sessions.items()):
                if session_data.get("webrtc_connection") == conn:
                    logger.info(f"🛑 Ending session {session_id_key} due to WebRTC connection closure")
                    
                    # Broadcast disconnect to web interface
                    await broadcast_session_update(session_id_key, {
                        "timestamp": datetime.now().isoformat(),
                        "type": "session_ended",
                        "message": "Session ended - WebRTC connection closed"
                    })
                    
                    active_sessions.pop(session_id_key, None)
                    logger.info(f"Session {session_id_key} cleaned up from WebRTC closure")
                    break
        
        # Create transport with voice keyboard settings (audio input only)
        transport_params_instance = TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=False,  # No audio output for voice keyboard
            vad_analyzer=SileroVADAnalyzer(),
        )
        
        transport = SmallWebRTCTransport(
            params=transport_params_instance, 
            webrtc_connection=webrtc_connection
        )
        
        # Store connection reference in session data (PipelineTask will be added later in run_voice_session)
        active_sessions[session_id] = {
            "webrtc_connection": webrtc_connection,
            "status": "connecting"
        }
        
        # Start voice session in background
        background_tasks.add_task(run_voice_session, transport, session_id, webrtc_connection)
        
        # Store the connection
        webrtc_connections[webrtc_connection.pc_id] = webrtc_connection
    
    # Get the answer
    answer = webrtc_connection.get_answer()
    
    # Apply ESP32 SDP munging if needed
    host_ip = os.getenv("HOST_IP", "localhost")
    if host_ip and host_ip != "localhost":
        answer["sdp"] = smallwebrtc_sdp_munging(answer["sdp"], host_ip)
        logger.info(f"Applied ESP32 SDP munging for host: {host_ip}")
    
    logger.info(f"Sending WebRTC answer for pc_id: {answer.get('pc_id')}")
    return answer


# REST API endpoints
@app.post("/debug")
async def receive_debug_message(request_data: dict):
    """Receive debug messages from ESP32 via HTTP POST"""
    try:
        message = request_data.get("message", "Unknown debug message")
        timestamp = request_data.get("timestamp", 0)
        session_id = request_data.get("session_id", "esp32_debug")
        
        logger.info(f"🔧 ESP32 Debug: {message} (t={timestamp}) [session: {session_id}]")
        
        # Also broadcast to web interface for visibility
        await broadcast_session_update(session_id, {
            "timestamp": datetime.now().isoformat(),
            "type": "esp32_debug",
            "message": f"ESP32: {message}",
            "esp32_timestamp": timestamp
        })
        
        return {"status": "success", "message": "Debug message received"}
    
    except Exception as e:
        logger.error(f"Error processing debug message: {e}")
        return {"status": "error", "message": str(e)}

@app.get("/debug")
async def get_debug_page():
    """Debug page to view ESP32 debug messages"""
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <title>ESP32 Debug Messages</title>
        <style>
            body { font-family: monospace; margin: 20px; background: #1e1e1e; color: #ffffff; }
            .debug-container { max-width: 1200px; margin: 0 auto; }
            .debug-message { padding: 8px; margin: 4px 0; background: #2d2d2d; border-left: 3px solid #007acc; }
            .timestamp { color: #888; font-size: 0.9em; }
            .message { color: #ffffff; margin-left: 10px; }
            h1 { color: #007acc; }
        </style>
        <script>
            let debugMessages = [];
            
            const ws = new WebSocket(`ws://${window.location.host}/ws`);
            
            ws.onmessage = function(event) {
                const data = JSON.parse(event.data);
                if (data.type === 'esp32_debug') {
                    addDebugMessage(data);
                }
            };
            
            function addDebugMessage(data) {
                const container = document.getElementById('debug-messages');
                const messageDiv = document.createElement('div');
                messageDiv.className = 'debug-message';
                
                const timestamp = new Date(data.timestamp).toLocaleTimeString();
                messageDiv.innerHTML = `
                    <span class="timestamp">[${timestamp}]</span>
                    <span class="message">${data.message}</span>
                `;
                
                container.insertBefore(messageDiv, container.firstChild);
                
                // Keep only last 100 messages
                while (container.children.length > 100) {
                    container.removeChild(container.lastChild);
                }
            }
        </script>
    </head>
    <body>
        <div class="debug-container">
            <h1>🔧 ESP32 Debug Messages</h1>
            <p>Real-time debug messages from ESP32 device</p>
            <div id="debug-messages"></div>
        </div>
    </body>
    </html>
    """)

@app.get("/")
async def get_index():
    """Serve the main web interface"""
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Voice Keyboard - Live Transcription</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
            .container { max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .header { border-bottom: 2px solid #2196f3; padding-bottom: 10px; margin-bottom: 20px; }
            .sessions { margin-bottom: 30px; }
            .session-item { border: 1px solid #ddd; margin: 10px 0; padding: 15px; border-radius: 5px; cursor: pointer; background: #fafafa; }
            .session-item:hover { background: #e9ecef; }
            .session-item.active { border-color: #2196f3; background: #f0f8ff; }
            .live-transcript { border: 2px solid #2196f3; padding: 15px; border-radius: 5px; background: #f0f8ff; }
            .message { margin: 10px 0; padding: 8px; border-radius: 4px; }
            .transcription-message { background: #e8f5e8; border-left: 4px solid #4caf50; }
            .typing-message { background: #e3f2fd; border-left: 4px solid #2196f3; }
            .timestamp { font-size: 0.8em; color: #666; }
            .status { display: inline-block; padding: 4px 8px; border-radius: 12px; font-size: 0.8em; }
            .status.active { background: #e8f5e8; color: #2e7d32; }
            .status.ended { background: #ffebee; color: #c62828; }
            .status.connecting { background: #fff3e0; color: #ef6c00; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>⌨️ Voice Keyboard</h1>
                <p>Real-time voice-to-keyboard transcription</p>
            </div>
            
            <div class="live-transcript">
                <h3>Live Session <span id="live-status" class="status">Waiting for ESP32...</span></h3>
                <div id="live-messages"></div>
            </div>
            
            <div class="sessions">
                <h3>Session History</h3>
                <div id="session-list">Loading...</div>
            </div>
        </div>

        <script>
            // WebSocket for live transcripts
            const ws = new WebSocket(`ws://${window.location.host}/transcript-stream`);
            const liveMessages = document.getElementById('live-messages');
            const liveStatus = document.getElementById('live-status');
            
            ws.onopen = function() {
                liveStatus.textContent = 'Connected';
                liveStatus.className = 'status active';
            };
            
            ws.onmessage = function(event) {
                const data = JSON.parse(event.data);
                
                if (data.message && data.message.type === 'session_ended') {
                    liveStatus.textContent = 'Session Ended';
                    liveStatus.className = 'status ended';
                    
                    // Clear live messages after a short delay
                    setTimeout(() => {
                        liveMessages.innerHTML = '';
                    }, 3000);
                } else if (data.message && data.message.type === 'session_started') {
                    liveStatus.textContent = 'Voice Session Active';
                    liveStatus.className = 'status active';
                    liveMessages.innerHTML = '';
                } else if (data.message && data.message.type === 'transcription_sent') {
                    addLiveMessage(data.message, 'typing');
                } else if (data.message && data.message.type === 'transcription_interim') {
                    updateInterimMessage(data.message);
                }
            };
            
            ws.onclose = function() {
                liveStatus.textContent = 'Disconnected';
                liveStatus.className = 'status ended';
            };
            
            let currentInterimMessage = null;
            
            function updateInterimMessage(message) {
                if (message.streaming) {
                    if (!currentInterimMessage) {
                        currentInterimMessage = document.createElement('div');
                        currentInterimMessage.className = 'message transcription-message interim';
                        currentInterimMessage.innerHTML = `
                            <div><strong>Listening:</strong> <span class="interim-text">${message.text}</span></div>
                            <div class="timestamp">${new Date(message.timestamp).toLocaleTimeString()}</div>
                        `;
                        liveMessages.appendChild(currentInterimMessage);
                    } else {
                        const interimText = currentInterimMessage.querySelector('.interim-text');
                        interimText.textContent = message.text;
                    }
                }
                liveMessages.scrollTop = liveMessages.scrollHeight;
            }
            
            function addLiveMessage(message, type) {
                // Remove interim message when final transcription is sent
                if (currentInterimMessage) {
                    currentInterimMessage.remove();
                    currentInterimMessage = null;
                }
                
                const messageDiv = document.createElement('div');
                const className = type === 'typing' ? 'typing-message' : 'transcription-message';
                const label = type === 'typing' ? 'Typed' : 'Transcribed';
                
                messageDiv.className = `message ${className}`;
                messageDiv.innerHTML = `
                    <div><strong>${label}:</strong> ${message.text}</div>
                    <div class="timestamp">${new Date(message.timestamp).toLocaleTimeString()}</div>
                `;
                liveMessages.appendChild(messageDiv);
                liveMessages.scrollTop = liveMessages.scrollHeight;
            }
            
            // Load session history (simplified - just show status)
            function loadSessions() {
                const listDiv = document.getElementById('session-list');
                listDiv.innerHTML = '<p>Session history will be available in future versions.</p>';
            }
            
            // Load sessions on page load
            loadSessions();
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Voice Keyboard Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind to")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")
    
    args = parser.parse_args()
    
    # Set logger level to INFO to reduce debug noise
    import sys
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    
    # Set HOST_IP environment variable for ESP32 SDP munging
    if args.host != "0.0.0.0" and args.host != "localhost":
        os.environ["HOST_IP"] = args.host
        logger.info(f"Set HOST_IP environment variable to: {args.host}")
    
    logger.info(f"Starting Voice Keyboard Server on {args.host}:{args.port}")
    logger.info(f"Web interface: http://{args.host}:{args.port}")
    logger.info(f"ESP32 HTTP endpoint: http://{args.host}:{args.port}/api/offer")
    
    uvicorn.run(
        "voice_keyboard_server:app",
        host=args.host,
        port=args.port,
        reload=args.reload
    )


if __name__ == "__main__":
    main()