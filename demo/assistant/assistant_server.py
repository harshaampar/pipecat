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

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import Frame, TextFrame, TranscriptionFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.openai_llm_context import OpenAILLMContext
from pipecat.processors.aggregators.llm_response import LLMUserContextAggregator, LLMAssistantContextAggregator
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.network.fastapi_websocket import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.transports.network.small_webrtc import SmallWebRTCTransport
from pipecat.transports.network.webrtc_connection import SmallWebRTCConnection
from pipecat.transports.services.daily import DailyParams

from transcript_manager import TranscriptManager
from conversation_memory import ConversationMemory

load_dotenv(override=True)

# Global instances
app = FastAPI()
transcript_manager = TranscriptManager()
conversation_memory = ConversationMemory(transcript_manager)
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


async def log_transcript_message(session_id: str, text: str, speaker: str, message_type: str):
    """Helper function to log messages"""
    try:
        await transcript_manager.log_message(
            session_id=session_id,
            text=text,
            speaker=speaker,
            confidence=1.0,
            message_type=message_type
        )
        
        # Broadcast complete message to live transcript viewers (not streaming)
        await broadcast_transcript_update(session_id, {
            "timestamp": datetime.now().isoformat(),
            "speaker": speaker,
            "text": text,
            "type": message_type,
            "streaming": False
        })
        
    except Exception as e:
        logger.error(f"❌ Error logging {speaker} message: {e}")


class MemoryEnhancedLLMContext(OpenAILLMContext):
    """LLM Context that includes conversation memory"""
    
    def __init__(self, messages: List[Dict], session_id: str):
        super().__init__(messages)
        self.session_id = session_id
        
    async def _get_enhanced_messages(self, current_input: str) -> List[Dict]:
        """Get messages enhanced with conversation memory"""
        # Get memory context
        memory_context = await conversation_memory.get_memory_enhanced_context(current_input)
        
        # Format context for LLM
        context_text = conversation_memory.format_context_for_llm(memory_context)
        
        # Add memory context to system message if we have relevant context
        enhanced_messages = self._messages.copy()
        
        if context_text:
            memory_prompt = f"\n\nConversation Context:\n{context_text}\n\nUse this context to provide more relevant and personalized responses."
            
            # Find system message and enhance it
            for msg in enhanced_messages:
                if msg["role"] == "system":
                    msg["content"] += memory_prompt
                    break
        
        return enhanced_messages




# Transport configurations
transport_params = {
    "daily": lambda: DailyParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        vad_analyzer=SileroVADAnalyzer(),
    ),
    "twilio": lambda: FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        vad_analyzer=SileroVADAnalyzer(),
    ),
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        vad_analyzer=SileroVADAnalyzer(),
    ),
}


async def create_conversation_pipeline(transport: BaseTransport, session_id: str) -> Tuple[PipelineTask, object, List[Dict]]:
    """Create the conversation pipeline with transcript logging"""
    
    # Initialize services
    stt = DeepgramSTTService(api_key=os.getenv("DEEPGRAM_API_KEY"))
    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        voice_id="71a7ad14-091c-4e8e-a314-022ece01c121",  # British Reading Lady
    )
    llm = OpenAILLMService(api_key=os.getenv("OPENAI_API_KEY"))

    # Initial messages with enhanced system prompt
    messages = [
        {
            "role": "system",
            "content": """You are a helpful AI assistant in a pocket voice assistant device. 
            Your goal is to be helpful, conversational, and remember context from our ongoing conversation.
            
            Key capabilities:
            - You can remember and reference things we've discussed earlier
            - You can help with various tasks, questions, and conversations
            - You're designed to be a pocket meeting/conversation assistant
            
            Keep responses conversational and natural. Your output will be converted to audio, 
            so avoid special characters and formatting. Be concise but helpful.""",
        },
    ]

    # Create memory-enhanced context
    context = MemoryEnhancedLLMContext(messages, session_id)
    context_aggregator = llm.create_context_aggregator(context)

    # Custom context aggregators that will log messages
    class LoggingUserContextAggregator(LLMUserContextAggregator):
        def __init__(self, context, session_id, params=None):
            super().__init__(context, params=params)
            self.session_id = session_id
            
        async def handle_aggregation(self, aggregation: str):
            # Log when user messages are added to context
            if aggregation.strip():
                await log_transcript_message(self.session_id, aggregation, "user", "user")
            return await super().handle_aggregation(aggregation)
    
    class LoggingAssistantContextAggregator(LLMAssistantContextAggregator):
        def __init__(self, context, session_id, params=None):
            super().__init__(context, params=params)
            self.session_id = session_id
            self._current_response = ""
            
        async def process_frame(self, frame, direction):
            # Handle real-time text streaming
            if isinstance(frame, TextFrame) and hasattr(frame, 'text') and frame.text.strip():
                # Stream individual text chunks in real-time
                text_chunk = frame.text.strip()
                
                # Broadcast real-time chunk to web interface with space
                await broadcast_transcript_update(self.session_id, {
                    "timestamp": datetime.now().isoformat(),
                    "speaker": "assistant",
                    "text": text_chunk + " ",  # Add space between words
                    "type": "assistant_chunk",
                    "streaming": True
                })
                
                # Accumulate for final logging
                self._current_response += text_chunk + " "
            
            elif isinstance(frame, LLMFullResponseEndFrame):
                # Log complete response when LLM finishes (only once)
                if self._current_response.strip():
                    complete_response = self._current_response.strip()
                    await log_transcript_message(self.session_id, complete_response, "assistant", "assistant")
                    self._current_response = ""
                    
            elif isinstance(frame, LLMFullResponseStartFrame):
                # Reset accumulator when LLM starts new response
                self._current_response = ""
            
            return await super().process_frame(frame, direction)
            
        async def handle_aggregation(self, aggregation: str):
            # Skip logging here to avoid duplicates - we already logged in process_frame
            return await super().handle_aggregation(aggregation)
    
    # Create logging context aggregators directly
    logging_user_aggregator = LoggingUserContextAggregator(context, session_id)
    logging_assistant_aggregator = LoggingAssistantContextAggregator(context, session_id)
    
    # Build pipeline - use the actual context aggregators in the pipeline
    pipeline = Pipeline([
        transport.input(),           # Transport user input
        stt,                        # Speech to text
        logging_user_aggregator,    # User responses (with logging)
        llm,                       # LLM processing
        tts,                       # Text to speech
        transport.output(),        # Transport bot output
        logging_assistant_aggregator,  # Assistant spoken responses (with logging)
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )
    
    return task, context_aggregator, messages


async def run_conversation(transport: BaseTransport, session_id: str):
    """Run a conversation session"""
    logger.info(f"🚀 Starting conversation session: {session_id}")
    
    # Start conversation tracking
    await transcript_manager.start_conversation(session_id)
    await conversation_memory.set_current_session(session_id)
    
    # Create pipeline
    task, context_aggregator, messages = await create_conversation_pipeline(transport, session_id)
    
    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"🔗 Client connected to session {session_id}")
        active_sessions[session_id] = {
            "start_time": datetime.now(),
            "client": client,
            "task": task
        }
        
        # Introduce the assistant
        messages.append({
            "role": "system", 
            "content": "Please introduce yourself briefly as a pocket voice assistant."
        })
        await task.queue_frames([context_aggregator.user().get_context_frame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"❌ Client disconnected from session {session_id}")
        
        # Broadcast disconnect status to web interface
        await broadcast_transcript_update(session_id, {
            "timestamp": datetime.now().isoformat(),
            "type": "client_disconnected",
            "message": "Client disconnected"
        })
        
        # End conversation tracking
        await transcript_manager.end_conversation(session_id)
        
        # Clean up
        if session_id in active_sessions:
            del active_sessions[session_id]
            
        await task.cancel()

    # Run the pipeline
    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)


# FastAPI WebSocket endpoint for ESP32
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebRTC WebSocket endpoint for ESP32-S3-Box clients"""
    await websocket.accept()
    
    # Generate session ID
    session_id = f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    # Create FastAPI transport
    transport_params_instance = FastAPIWebsocketParams(
        audio_in_enabled=True,
        audio_out_enabled=True,
        vad_analyzer=SileroVADAnalyzer(),
    )
    
    transport = FastAPIWebsocketTransport(websocket, transport_params_instance)
    
    try:
        await run_conversation(transport, session_id)
    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for session {session_id}")
    except Exception as e:
        logger.error(f"Error in WebSocket connection: {e}")


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


async def broadcast_transcript_update(session_id: str, message: Dict):
    """Broadcast transcript updates to all connected web clients"""
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
    """Handle WebRTC offer requests from ESP32 clients.
    
    Args:
        request: WebRTC offer request containing SDP and connection details
        background_tasks: FastAPI background tasks for running conversations
        
    Returns:
        WebRTC answer with connection details
    """
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
        
        # Handle connection cleanup
        @webrtc_connection.event_handler("closed")
        async def handle_webrtc_disconnected(conn: SmallWebRTCConnection):
            """Handle WebRTC connection closure and cleanup."""
            logger.info(f"Cleaning up WebRTC connection for pc_id: {conn.pc_id}")
            webrtc_connections.pop(conn.pc_id, None)
            
            # Also clean up session if it exists
            for session_id, session_data in list(active_sessions.items()):
                if session_data.get("webrtc_connection") == conn:
                    await transcript_manager.end_conversation(session_id)
                    active_sessions.pop(session_id, None)
                    break
        
        # Create transport with transcript logging
        transport_params = TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            vad_analyzer=SileroVADAnalyzer(),
        )
        
        transport = SmallWebRTCTransport(
            params=transport_params, 
            webrtc_connection=webrtc_connection
        )
        
        # Generate session ID for this WebRTC connection
        session_id = f"webrtc_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{webrtc_connection.pc_id}"
        
        # Start conversation in background
        background_tasks.add_task(run_conversation, transport, session_id)
        
        # Store the connection
        webrtc_connections[webrtc_connection.pc_id] = webrtc_connection
    
    # Get the answer
    answer = webrtc_connection.get_answer()
    
    # Apply ESP32 SDP munging if needed
    # Note: You may need to pass the host IP here, get it from request headers or config
    host_ip = os.getenv("HOST_IP", "localhost")  # Set this in your environment
    if host_ip and host_ip != "localhost":
        answer["sdp"] = smallwebrtc_sdp_munging(answer["sdp"], host_ip)
        logger.info(f"Applied ESP32 SDP munging for host: {host_ip}")
    
    logger.info(f"Sending WebRTC answer for pc_id: {answer.get('pc_id')}")
    return answer


# REST API endpoints
@app.get("/")
async def get_index():
    """Serve the main web interface"""
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Pocket Voice Assistant - Transcripts</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
            .container { max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .header { border-bottom: 2px solid #007bff; padding-bottom: 10px; margin-bottom: 20px; }
            .conversations { margin-bottom: 30px; }
            .conversation-item { border: 1px solid #ddd; margin: 10px 0; padding: 15px; border-radius: 5px; cursor: pointer; background: #fafafa; }
            .conversation-item:hover { background: #e9ecef; }
            .live-transcript { border: 2px solid #28a745; padding: 15px; border-radius: 5px; background: #f8fff9; }
            .message { margin: 10px 0; padding: 8px; border-radius: 4px; }
            .user-message { background: #e3f2fd; border-left: 4px solid #2196f3; }
            .assistant-message { background: #f3e5f5; border-left: 4px solid #9c27b0; }
            .timestamp { font-size: 0.8em; color: #666; }
            .status { display: inline-block; padding: 4px 8px; border-radius: 12px; font-size: 0.8em; }
            .status.active { background: #d4edda; color: #155724; }
            .status.ended { background: #f8d7da; color: #721c24; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🎙️ Pocket Voice Assistant</h1>
                <p>Real-time conversation transcripts and history</p>
            </div>
            
            <div class="live-transcript">
                <h3>Live Transcript <span id="live-status" class="status">Waiting for connection...</span></h3>
                <div id="live-messages"></div>
            </div>
            
            <div class="conversations">
                <h3>Conversation History</h3>
                <div id="conversation-list">Loading...</div>
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
                
                if (data.message && data.message.type === 'client_disconnected') {
                    // Handle client disconnect - refresh conversation list
                    liveStatus.textContent = 'Client Disconnected';
                    liveStatus.className = 'status ended';
                    
                    // Clear live messages and refresh conversation list
                    setTimeout(() => {
                        liveMessages.innerHTML = '';
                        loadConversations();
                    }, 1000);
                } else if (data.message) {
                    addLiveMessage(data.message);
                }
            };
            
            ws.onclose = function() {
                liveStatus.textContent = 'Disconnected';
                liveStatus.className = 'status ended';
            };
            
            let currentStreamingMessage = null;
            
            function addLiveMessage(message) {
                if (message.streaming && message.type === 'assistant_chunk') {
                    // Handle streaming assistant response
                    if (!currentStreamingMessage) {
                        // Create new streaming message
                        currentStreamingMessage = document.createElement('div');
                        currentStreamingMessage.className = `message ${message.speaker}-message`;
                        currentStreamingMessage.innerHTML = `
                            <div><strong>${message.speaker}:</strong> <span class="streaming-text">${message.text}</span></div>
                            <div class="timestamp">${new Date(message.timestamp).toLocaleTimeString()}</div>
                        `;
                        liveMessages.appendChild(currentStreamingMessage);
                    } else {
                        // Append to existing streaming message
                        const streamingText = currentStreamingMessage.querySelector('.streaming-text');
                        streamingText.textContent += message.text;
                    }
                } else {
                    // Handle complete messages (user messages or final assistant)
                    if (currentStreamingMessage && message.speaker === 'assistant') {
                        // Finalize streaming message
                        currentStreamingMessage = null;
                    } else {
                        // Add new complete message
                        const messageDiv = document.createElement('div');
                        messageDiv.className = `message ${message.speaker}-message`;
                        messageDiv.innerHTML = `
                            <div><strong>${message.speaker}:</strong> ${message.text}</div>
                            <div class="timestamp">${new Date(message.timestamp).toLocaleTimeString()}</div>
                        `;
                        liveMessages.appendChild(messageDiv);
                    }
                }
                liveMessages.scrollTop = liveMessages.scrollHeight;
            }
            
            // Load conversation history
            async function loadConversations() {
                try {
                    const response = await fetch('/api/conversations');
                    const conversations = await response.json();
                    
                    const listDiv = document.getElementById('conversation-list');
                    listDiv.innerHTML = '';
                    
                    if (conversations.length === 0) {
                        listDiv.innerHTML = '<p>No conversations yet.</p>';
                        return;
                    }
                    
                    conversations.forEach(conv => {
                        const convDiv = document.createElement('div');
                        convDiv.className = 'conversation-item';
                        convDiv.innerHTML = `
                            <div><strong>${conv.title || 'Untitled Conversation'}</strong></div>
                            <div class="timestamp">
                                ${new Date(conv.start_time).toLocaleString()} 
                                (${conv.message_count || 0} messages)
                            </div>
                        `;
                        convDiv.onclick = () => viewConversation(conv.session_id);
                        listDiv.appendChild(convDiv);
                    });
                } catch (error) {
                    console.error('Error loading conversations:', error);
                }
            }
            
            function viewConversation(sessionId) {
                // URL encode the session ID to handle special characters like #
                const encodedSessionId = encodeURIComponent(sessionId);
                window.open(`/conversation/${encodedSessionId}`, '_blank');
            }
            
            // Load conversations on page load
            loadConversations();
            
            // Refresh conversations every 30 seconds
            setInterval(loadConversations, 30000);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.get("/api/conversations")
async def get_conversations():
    """Get list of conversations"""
    try:
        logger.info(f"📋 Fetching conversations from database...")
        conversations = await transcript_manager.get_conversations()
        logger.info(f"📊 Found {len(conversations)} conversations in database")
        for conv in conversations:
            logger.info(f"  - {conv['session_id']}: {conv.get('message_count', 0)} messages")
        return JSONResponse(content=conversations)
    except Exception as e:
        logger.error(f"❌ Error fetching conversations: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/conversation/{session_id}")
async def view_conversation(session_id: str):
    """View a specific conversation"""
    try:
        # URL decode the session ID to handle special characters like #
        from urllib.parse import unquote
        decoded_session_id = unquote(session_id)
        logger.info(f"📖 Attempting to view conversation: {decoded_session_id} (encoded: {session_id})")
        
        conversation = await transcript_manager.get_conversation(decoded_session_id)
        logger.info(f"📖 Conversation lookup result: {conversation is not None}")
        
        if not conversation:
            # List all available session IDs for debugging
            all_conversations = await transcript_manager.get_conversations()
            available_ids = [conv['session_id'] for conv in all_conversations]
            logger.error(f"❌ Session {decoded_session_id} not found. Available sessions: {available_ids}")
            raise HTTPException(status_code=404, detail="Conversation not found")
        
        # Check if this is an active session for live updates
        is_active = decoded_session_id in active_sessions
        logger.info(f"📖 Session {decoded_session_id} is {'active' if is_active else 'inactive'}")
        
        # Generate HTML for conversation view
        live_indicator = ' <span style="color: green;">🔴 LIVE</span>' if is_active else ''
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Conversation: {conversation.get('title', 'Untitled')}</title>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
                .container {{ max-width: 800px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
                .header {{ border-bottom: 2px solid #007bff; padding-bottom: 10px; margin-bottom: 20px; }}
                .message {{ margin: 15px 0; padding: 12px; border-radius: 8px; }}
                .user-message {{ background: #e3f2fd; border-left: 4px solid #2196f3; }}
                .assistant-message {{ background: #f3e5f5; border-left: 4px solid #9c27b0; }}
                .timestamp {{ font-size: 0.8em; color: #666; margin-top: 5px; }}
                .back-link {{ color: #007bff; text-decoration: none; }}
                .back-link:hover {{ text-decoration: underline; }}
                .live-indicator {{ color: green; animation: pulse 1.5s infinite; }}
                @keyframes pulse {{ 0% {{ opacity: 1; }} 50% {{ opacity: 0.5; }} 100% {{ opacity: 1; }} }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <a href="/" class="back-link">← Back to All Conversations</a>
                    <h1>{conversation.get('title', 'Untitled Conversation')}{live_indicator}</h1>
                    <p>Started: {conversation.get('start_time', 'Unknown')} | Messages: {len(conversation.get('messages', []))}</p>
                </div>
                
                <div class="messages" id="messages-container">
        """
        
        # Add messages
        for message in conversation.get('messages', []):
            text = message.get('corrected_text') or message.get('original_text', '')
            speaker = message.get('speaker', 'unknown')
            timestamp = message.get('timestamp', '')
            
            html_content += f"""
                    <div class="message {speaker}-message">
                        <strong>{speaker.title()}:</strong> {text}
                        <div class="timestamp">{timestamp}</div>
                    </div>
            """
        
        html_content += """
                </div>
            </div>
            
            <script>
                // Live updates for active conversations
                const isActive = """ + str(is_active).lower() + f""";
                let currentStreamingMessage = null;
                
                if (isActive) {{
                    const ws = new WebSocket(`ws://${{window.location.host}}/transcript-stream`);
                    const messagesContainer = document.getElementById('messages-container');
                    
                    ws.onmessage = function(event) {{
                        const data = JSON.parse(event.data);
                        
                        // Only show updates for this specific session
                        if (data.session_id === '{decoded_session_id}') {{
                            if (data.message && data.message.type === 'client_disconnected') {{
                                // Handle client disconnect
                                const headerTitle = document.querySelector('h1');
                                headerTitle.innerHTML = headerTitle.innerHTML.replace('🔴 LIVE', '❌ DISCONNECTED');
                            }} else if (data.message) {{
                                addLiveMessage(data.message);
                            }}
                        }}
                    }};
                    
                    function addLiveMessage(message) {{
                        if (message.streaming && message.type === 'assistant_chunk') {{
                            // Handle streaming assistant response
                            if (!currentStreamingMessage) {{
                                // Create new streaming message
                                currentStreamingMessage = document.createElement('div');
                                currentStreamingMessage.className = `message ${{message.speaker}}-message`;
                                currentStreamingMessage.innerHTML = `
                                    <strong>${{message.speaker.charAt(0).toUpperCase() + message.speaker.slice(1)}}:</strong> 
                                    <span class="streaming-text">${{message.text}}</span>
                                    <div class="timestamp">${{new Date(message.timestamp).toLocaleTimeString()}}</div>
                                `;
                                messagesContainer.appendChild(currentStreamingMessage);
                            }} else {{
                                // Append to existing streaming message
                                const streamingText = currentStreamingMessage.querySelector('.streaming-text');
                                streamingText.textContent += message.text;
                            }}
                        }} else {{
                            // Handle complete messages (user messages or final assistant)
                            if (currentStreamingMessage && message.speaker === 'assistant') {{
                                // Finalize streaming message
                                currentStreamingMessage = null;
                            }} else {{
                                // Add new complete message
                                const messageDiv = document.createElement('div');
                                messageDiv.className = `message ${{message.speaker}}-message`;
                                messageDiv.innerHTML = `
                                    <strong>${{message.speaker.charAt(0).toUpperCase() + message.speaker.slice(1)}}:</strong> ${{message.text}}
                                    <div class="timestamp">${{new Date(message.timestamp).toLocaleTimeString()}}</div>
                                `;
                                messagesContainer.appendChild(messageDiv);
                            }}
                        }}
                        messagesContainer.scrollTop = messagesContainer.scrollHeight;
                    }}
                }}
            </script>
        </body>
        </html>
        """
        
        return HTMLResponse(content=html_content)
        
    except Exception as e:
        logger.error(f"Error viewing conversation {session_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/conversations/{session_id}")
async def get_conversation_api(session_id: str):
    """Get a specific conversation via API"""
    try:
        # URL decode the session ID to handle special characters like #
        from urllib.parse import unquote
        decoded_session_id = unquote(session_id)
        
        conversation = await transcript_manager.get_conversation(decoded_session_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return JSONResponse(content=conversation)
    except Exception as e:
        logger.error(f"Error fetching conversation {decoded_session_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Pocket Voice Assistant Server")
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
    
    logger.info(f"Starting Pocket Voice Assistant Server on {args.host}:{args.port}")
    logger.info(f"Web interface: http://{args.host}:{args.port}")
    logger.info(f"WebSocket endpoint: ws://{args.host}:{args.port}/ws") 
    logger.info(f"ESP32 HTTP endpoint: http://{args.host}:{args.port}/api/offer")
    
    uvicorn.run(
        "assistant_server:app",
        host=args.host,
        port=args.port,
        reload=args.reload
    )


if __name__ == "__main__":
    main()