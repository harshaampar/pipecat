import argparse
import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from deepgram import LiveOptions
from openai import AsyncOpenAI

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.network.small_webrtc import SmallWebRTCTransport
from pipecat.transports.network.webrtc_connection import SmallWebRTCConnection

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


class LatencyTracker:
    """Track and calculate latency statistics"""
    
    def __init__(self, window_size: int = 10):
        self.window_size = window_size
        self.llm_latencies: List[float] = []
        self.total_latencies: List[float] = []
        
    def record_latency(self, llm_ms: float, total_ms: float) -> Dict[str, float]:
        """Record latency and return current stats"""
        self.llm_latencies.append(llm_ms)
        self.total_latencies.append(total_ms)
        
        # Keep only recent measurements
        if len(self.llm_latencies) > self.window_size:
            self.llm_latencies = self.llm_latencies[-self.window_size:]
            self.total_latencies = self.total_latencies[-self.window_size:]
        
        return {
            "llm_avg": sum(self.llm_latencies) / len(self.llm_latencies),
            "total_avg": sum(self.total_latencies) / len(self.total_latencies),
            "count": len(self.llm_latencies)
        }


class VoiceCommandProcessor(FrameProcessor):
    """Processor that routes transcribed text through LLM for command analysis and sends results to ESP32"""
    
    def __init__(self, session_id: str, webrtc_connection: SmallWebRTCConnection):
        super().__init__()
        self.session_id = session_id
        self.webrtc_connection = webrtc_connection
        self.latency_tracker = LatencyTracker()
        
        # Initialize OpenAI client for LLM analysis
        self.openai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        
    async def analyze_command_with_llm(self, text: str) -> dict:
        """Analyze text using OpenAI to determine if it contains commands"""
        
        command_list = """
        Available keyboard commands (after "ESP" trigger):
        - select all/everything → Cmd+A
        - copy → Cmd+C  
        - paste → Cmd+V
        - cut → Cmd+X
        - undo → Cmd+Z
        - redo → Cmd+Shift+Z
        - save → Cmd+S
        - find → Cmd+F
        - new → Cmd+N
        - open → Cmd+O
        - close → Cmd+W
        - quit → Cmd+Q
        - enter/return/new line/next line/go to next line → Enter key
        - tab → Tab key
        - backspace/delete last/delete previous character → Backspace key
        - delete/delete next/delete character → Delete key
        - delete word/delete previous word → Option+Backspace (Alt+Backspace)
        - delete line/delete current line/clear line/delete the line → Cmd+Backspace
        - clear/clear all → Cmd+A then Delete
        - escape → Escape key
        - space → Space key
        - up/up arrow/scroll up → Up arrow
        - down/down arrow/scroll down → Down arrow
        - left/left arrow → Left arrow
        - right/right arrow → Right arrow
        - home → Home key
        - end → End key
        - page up → Page Up key
        - page down → Page Down key
        """
        
        prompt = f"""
        You are a voice keyboard command parser. Analyze voice input for ESP keyboard commands.
        
        Input: "{text}"
        
        STRICT RULES:
        1. ONLY trigger commands if input contains ESP variations: "ESP", "E S P", "E s p", "esp", "Esp", "ASP", "asp", "A S P", "A s p" (STT variations)
        2. "ASAP", "ISP", etc. are NOT triggers - return as regular text
        3. If no ESP trigger, return as regular text
        4. If command after ESP is unclear or not in list below, return NO ACTION (not text)
        5. IGNORE punctuation and trailing characters (commas, periods, etc.) when matching commands
        6. Only use these EXACT command mappings:
        
        EXACT WORD MATCHING (case insensitive):
        - "copy" → Cmd+C
        - "paste" → Cmd+V  
        - "cut" → Cmd+X
        - "select all" OR "select everything" → Cmd+A
        - "undo" → Cmd+Z
        - "save" → Cmd+S
        - "enter" OR "new line" OR "next line" → Enter key
        - "backspace" → Backspace key
        - "delete" → Delete key
        - "tab" → Tab key
        - "up" → Up arrow
        - "down" → Down arrow
        
        IMPORTANT: Words like "done", "finished", "complete", "stop" are NOT commands!
        
        EXAMPLES:
        - "ESP copy this" → {{"type": "command", "actions": [{{"type": "shortcut", "keys": ["cmd", "c"]}}]}}
        - "E S P enter" → {{"type": "command", "actions": [{{"type": "key", "key": "enter"}}]}}
        - "ESP backspace" → {{"type": "command", "actions": [{{"type": "key", "key": "backspace"}}]}}
        - "ESP paste" → {{"type": "command", "actions": [{{"type": "shortcut", "keys": ["cmd", "v"]}}]}}
        - "ESP select all" → {{"type": "command", "actions": [{{"type": "shortcut", "keys": ["cmd", "a"]}}]}}
        - "Esp undo," → {{"type": "command", "actions": [{{"type": "shortcut", "keys": ["cmd", "z"]}}]}}
        - "asp select all." → {{"type": "command", "actions": [{{"type": "shortcut", "keys": ["cmd", "a"]}}]}}
        - "A s p select all," → {{"type": "command", "actions": [{{"type": "shortcut", "keys": ["cmd", "a"]}}]}}
        - "Asp backspace," → {{"type": "command", "actions": [{{"type": "key", "key": "backspace"}}]}}
        - "ESP copy." → {{"type": "command", "actions": [{{"type": "shortcut", "keys": ["cmd", "c"]}}]}}
        - "ESP paste!" → {{"type": "command", "actions": [{{"type": "shortcut", "keys": ["cmd", "v"]}}]}}
        - "ESP undo?" → {{"type": "command", "actions": [{{"type": "shortcut", "keys": ["cmd", "z"]}}]}}
        
        NEGATIVE EXAMPLES (return as regular text):
        - "ASAP clear" → {{"type": "text"}}
        - "Please copy this" → {{"type": "text"}}
        - "The ESP board" → {{"type": "text"}}
        
        UNCLEAR COMMANDS (return no action):
        - "ESP done" → {{"type": "no_action"}}
        - "ESP finished" → {{"type": "no_action"}}
        - "ESP stop" → {{"type": "no_action"}}
        - "ESP complete" → {{"type": "no_action"}}
        - "ESP delete line" → {{"type": "no_action"}}
        - "ESP clear screen" → {{"type": "no_action"}}
        - "ESP some unclear command" → {{"type": "no_action"}}
        
        RESPONSE FORMAT:
        - Clear command: {{"type": "command", "actions": [...]}}
        - Regular text: {{"type": "text"}}  
        - Unclear/unsupported command: {{"type": "no_action"}}
        
        Respond with JSON only. When in doubt, use "no_action".
        """
        
        try:
            response = await self.openai_client.chat.completions.create(
                # model="gpt-3.5-turbo",
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1
            )
            
            response_text = response.choices[0].message.content.strip()
            logger.debug(f"🤖 Raw LLM response for '{text}': {response_text}")
            
            # Parse JSON response
            if response_text.startswith('```json'):
                response_text = response_text[7:-3].strip()
            elif response_text.startswith('```'):
                response_text = response_text[3:-3].strip()
                
            result = json.loads(response_text)
            logger.info(f"🎯 Parsed LLM result for '{text}': {result}")
            return result
            
        except json.JSONDecodeError as e:
            logger.error(f"❌ JSON parsing error for '{text}': {e}")
            logger.error(f"❌ Raw response was: {response_text}")
            # Fallback: treat as regular text
            return {"type": "text"}
        except Exception as e:
            logger.error(f"❌ Error analyzing command with LLM for '{text}': {e}")
            # Fallback: treat as regular text
            return {"type": "text"}
        
    async def send_to_esp32(self, message: Dict) -> None:
        """Send message to ESP32 via WebRTC data channel"""
        try:
            if hasattr(self.webrtc_connection, 'send_message'):
                self.webrtc_connection.send_message(message)
            elif hasattr(self.webrtc_connection, 'send_app_message'):
                result = self.webrtc_connection.send_app_message(message)
                if result is not None and hasattr(result, '__await__'):
                    await result
            else:
                # Fallback - try to send via data channel directly
                self.webrtc_connection.send_data_channel_message(json.dumps(message))
                
        except Exception as e:
            logger.error(f"❌ Error sending message to ESP32: {e}")
            raise
        
    async def process_frame(self, frame, direction):
        """Process transcription frames and send to ESP32"""
        # Handle system frames
        await super().process_frame(frame, direction)
        
        # Handle transcription frames
        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            is_interim = getattr(frame, 'interim', False)
            
            if not is_interim:  # Only process final transcriptions
                start_time = time.time()
                logger.info(f"📝 [{self.session_id}] Transcription received: '{frame.text}'")
                
                # LLM analysis for command detection
                process_start = time.time()
                llm_start = time.time()
                
                # Use LLM to analyze the transcription
                llm_result = await self.analyze_command_with_llm(frame.text)
                
                llm_end = time.time()
                llm_latency = (llm_end - llm_start) * 1000
                
                if llm_result.get("type") == "command" and "actions" in llm_result:
                    # LLM detected a command
                    actions = llm_result["actions"]
                    
                    message = {
                        "type": "keyboard_command", 
                        "actions": actions
                    }
                    
                    send_start = time.time()
                    await self.send_to_esp32(message)
                    send_end = time.time()
                    send_latency = (send_end - send_start) * 1000
                    
                    logger.info(f"📤 [{self.session_id}] Sent command to ESP32: {actions}")
                    
                    # Broadcast to web interface
                    await broadcast_session_update(self.session_id, {
                        "timestamp": datetime.now().isoformat(),
                        "text": frame.text,
                        "type": "command_sent",
                        "actions": actions,
                        "streaming": False
                    })
                elif llm_result.get("type") == "text":
                    # LLM determined it's regular text
                    send_start = time.time()
                    await self._send_as_text(frame.text, process_start)
                    send_end = time.time()
                    send_latency = (send_end - send_start) * 1000
                elif llm_result.get("type") == "no_action":
                    # LLM determined command was unclear - do nothing
                    send_start = time.time()
                    send_end = time.time()
                    send_latency = 0.0
                    
                    logger.info(f"🚫 [{self.session_id}] LLM detected unclear ESP command - no action taken: '{frame.text}'")
                    
                    # Broadcast to web interface
                    await broadcast_session_update(self.session_id, {
                        "timestamp": datetime.now().isoformat(),
                        "text": frame.text,
                        "type": "no_action",
                        "message": "Unclear command - no action taken",
                        "streaming": False
                    })
                else:
                    # Fallback to regular text if LLM response is unexpected
                    send_start = time.time()
                    await self._send_as_text(frame.text, process_start)
                    send_end = time.time()
                    send_latency = (send_end - send_start) * 1000
                
                # Calculate and log latency
                total_latency = (time.time() - start_time) * 1000
                process_latency = (time.time() - process_start) * 1000
                
                stats = self.latency_tracker.record_latency(llm_latency, total_latency)
                
                logger.info(f"⏱️ [{self.session_id}] LATENCY: LLM: {llm_latency:.1f}ms | Total: {total_latency:.1f}ms | Avg: {stats['total_avg']:.1f}ms")
                
                # Broadcast latency metrics
                await broadcast_session_update(self.session_id, {
                    "timestamp": datetime.now().isoformat(),
                    "type": "latency_metrics",
                    "llm_latency": llm_latency,
                    "process_latency": process_latency,
                    "send_latency": send_latency,
                    "total_latency": total_latency,
                    "avg_latency": stats["total_avg"],
                    "count": stats["count"]
                })
            else:
                # For interim results, only broadcast to web interface
                await broadcast_session_update(self.session_id, {
                    "timestamp": datetime.now().isoformat(),
                    "text": frame.text,
                    "type": "transcription_interim",
                    "streaming": True
                })
        
        # Always push frame downstream
        await self.push_frame(frame, direction)
    
    async def _send_as_text(self, text: str, process_start_time: float):
        """Helper to send text to ESP32"""
        text_with_space = text.strip() + " "
        
        message = {
            "type": "transcribed_text",
            "text": text_with_space
        }
        
        send_start = time.time()
        await self.send_to_esp32(message)
        send_end = time.time()
        
        logger.info(f"📤 [{self.session_id}] Sent text to ESP32: {text.strip()}")
        
        # Broadcast to web interface  
        await broadcast_session_update(self.session_id, {
            "timestamp": datetime.now().isoformat(),
            "text": text,
            "type": "transcription_sent", 
            "streaming": False
        })
    


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

    # Create voice command processor for this session
    voice_processor = VoiceCommandProcessor(session_id, webrtc_connection)
    
    # Build pipeline - transcription + LLM analysis + command/text handling
    pipeline = Pipeline([
        transport.input(),           # Transport user input (audio)
        stt,                        # Speech to text (Deepgram Nova-3)
        voice_processor,            # LLM analysis + send commands/text to ESP32
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
    
    return task, voice_processor


async def run_voice_session(transport: BaseTransport, session_id: str, webrtc_connection: SmallWebRTCConnection):
    """Run a voice keyboard session"""
    logger.info(f"🚀 Starting voice keyboard session: {session_id}")
    
    # Create pipeline
    task, voice_processor = await create_voice_keyboard_pipeline(transport, session_id, webrtc_connection)
    
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
            .container { max-width: 1400px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .header { border-bottom: 2px solid #2196f3; padding-bottom: 10px; margin-bottom: 20px; }
            
            .main-content { display: flex; gap: 20px; }
            .left-panel { flex: 1; min-width: 400px; }
            .right-panel { flex: 1; min-width: 400px; }
            
            .sessions { margin-bottom: 30px; }
            .session-item { border: 1px solid #ddd; margin: 10px 0; padding: 15px; border-radius: 5px; cursor: pointer; background: #fafafa; }
            .session-item:hover { background: #e9ecef; }
            .session-item.active { border-color: #2196f3; background: #f0f8ff; }
            
            .test-section { margin-bottom: 20px; padding: 20px; background: #f8f9fa; border-radius: 8px; border: 1px solid #dee2e6; }
            .live-transcript { border: 2px solid #2196f3; padding: 15px; border-radius: 5px; background: #f0f8ff; height: 600px; overflow: hidden; }
            #live-messages { height: 400px; overflow-y: auto; border: 1px solid #ddd; border-radius: 4px; padding: 10px; background: #fff; }
            
            .message { margin: 8px 0; padding: 10px; border-radius: 6px; border-left: 5px solid; font-family: 'Segoe UI', sans-serif; }
            .transcription-message { background: #e8f5e8; border-left-color: #4caf50; }
            .transcription-interim { background: #f0f8e8; border-left-color: #8bc34a; font-style: italic; opacity: 0.8; }
            .typing-message { background: #e3f2fd; border-left-color: #2196f3; }
            .command-message { background: #fff3e0; border-left-color: #ff9800; }
            .no-action-message { background: #fff3e0; border-left-color: #f57c00; }
            .error-message { background: #ffebee; border-left-color: #f44336; }
            .debug-message { background: #f3e5f5; border-left-color: #9c27b0; }
            .timestamp { font-size: 0.8em; color: #666; }
            .status { display: inline-block; padding: 4px 8px; border-radius: 12px; font-size: 0.8em; }
            .status.active { background: #e8f5e8; color: #2e7d32; }
            .status.ended { background: #ffebee; color: #c62828; }
            .status.connecting { background: #fff3e0; color: #ef6c00; }
            
            .latency-metrics { 
                background: #f8f9fa; 
                border: 1px solid #dee2e6; 
                border-radius: 4px; 
                padding: 10px; 
                margin: 10px 0; 
            }
            .latency-row { 
                display: flex; 
                gap: 15px; 
                font-family: monospace; 
                flex-wrap: wrap;
            }
            .metric { 
                background: #e9ecef; 
                padding: 4px 8px; 
                border-radius: 3px; 
                font-size: 0.9em; 
                white-space: nowrap;
            }
            .metric.total { background: #d1ecf1; border: 1px solid #bee5eb; }
            .metric.avg { background: #d4edda; border: 1px solid #c3e6cb; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>⌨️ Voice Keyboard</h1>
                <p>Real-time voice-to-keyboard transcription</p>
            </div>
            
            <div class="main-content">
                <div class="left-panel">
                    <!-- Test Text Box -->
                    <div class="test-section">
                        <h3>📝 Test Text Area</h3>
                        <p>Use this text area to test voice keyboard commands. Try saying "ESP copy", "ESP paste", etc.</p>
                        <textarea id="test-textarea" placeholder="Start typing here, or use voice commands to control this text area..." 
                                 rows="10" cols="80" style="width: 100%; font-family: monospace; font-size: 14px; padding: 10px; border: 2px solid #2196f3; border-radius: 4px; resize: vertical;" autofocus></textarea>
                    </div>
                </div>
                
                <div class="right-panel">
                    <div class="live-transcript">
                        <h3>Live Session <span id="live-status" class="status">Waiting for ESP32...</span></h3>
                        
                        <!-- Latency Metrics Section -->
                        <div class="latency-metrics">
                            <h4>⏱️ Performance Metrics</h4>
                            <div class="latency-row">
                                <span class="metric">LLM: <span id="llm-latency">--</span>ms</span>
                                <span class="metric">Processing: <span id="process-latency">--</span>ms</span>
                                <span class="metric">Send: <span id="send-latency">--</span>ms</span>
                                <span class="metric total">Total: <span id="total-latency">--</span>ms</span>
                                <span class="metric avg">Avg: <span id="avg-latency">--</span>ms</span>
                                <span class="metric">Count: <span id="request-count">0</span></span>
                            </div>
                        </div>
                        
                        <h4>🔍 Live Activity Log</h4>
                        <div id="live-messages"></div>
                    </div>
                </div>
            </div>
            
            <div class="sessions">
                <h3>Session History</h3>
                <div id="session-list">Loading...</div>
            </div>
        </div>

        <script>
            // Multiple approaches to ensure textarea gets focused
            window.onload = function() {
                setTimeout(() => {
                    const textarea = document.getElementById('test-textarea');
                    if (textarea) {
                        textarea.focus();
                        textarea.setSelectionRange(0, 0);
                    }
                }, 100);
            };
            
            // Store messages for proper ordering
            let messageHistory = [];
            
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
                    addLiveMessage(data.message, 'debug');
                } else if (data.message && data.message.type === 'session_started') {
                    liveStatus.textContent = 'Voice Session Active';
                    liveStatus.className = 'status active';
                    addLiveMessage(data.message, 'debug');
                } else if (data.message && data.message.type === 'transcription_sent') {
                    addLiveMessage(data.message, 'typing');
                } else if (data.message && data.message.type === 'command_sent') {
                    addLiveMessage(data.message, 'command');
                } else if (data.message && data.message.type === 'no_action') {
                    addLiveMessage(data.message, 'no_action');
                } else if (data.message && data.message.type === 'transcription_interim') {
                    updateInterimMessage(data.message);
                } else if (data.message && data.message.type === 'latency_metrics') {
                    updateLatencyMetrics(data.message);
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
                        liveMessages.prepend(currentInterimMessage);
                    } else {
                        const interimText = currentInterimMessage.querySelector('.interim-text');
                        interimText.textContent = message.text;
                    }
                }
                liveMessages.scrollTop = liveMessages.scrollHeight;
            }
            
            function addLiveMessage(message, type) {
                // Remove interim message when final transcription is sent
                if (currentInterimMessage && (type === 'typing' || type === 'command' || type === 'no_action')) {
                    currentInterimMessage.remove();
                    currentInterimMessage = null;
                }
                
                const messageDiv = document.createElement('div');
                let className, label, content, icon;
                
                if (type === 'typing') {
                    className = 'typing-message';
                    label = 'Text Typed';
                    icon = '⌨️';
                    content = `"${message.text}"`;
                } else if (type === 'command') {
                    className = 'command-message';
                    label = 'Command Executed';
                    icon = '⚡';
                    // Format the actions nicely
                    const actionStrings = message.actions.map(action => {
                        if (action.type === 'shortcut') {
                            return action.keys.join('+');
                        } else if (action.type === 'key') {
                            return action.repeat ? `${action.key} (${action.repeat}x)` : action.key;
                        }
                        return JSON.stringify(action);
                    });
                    content = `"${message.text}" → ${actionStrings.join(', ')}`;
                } else if (type === 'no_action') {
                    className = 'no-action-message';
                    label = 'Command Ignored';
                    icon = '🚫';
                    content = `"${message.text}" → ${message.message || 'No action taken'}`;
                } else if (type === 'debug') {
                    className = 'debug-message';
                    label = 'System';
                    icon = '🔧';
                    content = message.message || message.text || 'Debug message';
                } else {
                    className = 'transcription-message';
                    label = 'Transcribed';
                    icon = '🎤';
                    content = `"${message.text}"`;
                }
                
                messageDiv.className = `message ${className}`;
                const timestamp = message.timestamp ? new Date(message.timestamp).toLocaleTimeString() : new Date().toLocaleTimeString();
                messageDiv.innerHTML = `
                    <div><strong>${icon} ${label}:</strong> ${content}</div>
                    <div class="timestamp">${timestamp}</div>
                `;
                // Add to beginning of message history
                messageHistory.unshift({
                    className: className,
                    icon: icon,
                    label: label,
                    content: content,
                    timestamp: timestamp
                });
                
                // Keep only last 50 messages
                if (messageHistory.length > 50) {
                    messageHistory = messageHistory.slice(0, 50);
                }
                
                // Rebuild entire message display to ensure correct ordering
                liveMessages.innerHTML = messageHistory.map(msg => `
                    <div class="message ${msg.className}">
                        <div><strong>${msg.icon} ${msg.label}:</strong> ${msg.content}</div>
                        <div class="timestamp">${msg.timestamp}</div>
                    </div>
                `).join('');
            }
            
            function updateLatencyMetrics(metrics) {
                document.getElementById('llm-latency').textContent = metrics.llm_latency.toFixed(1);
                document.getElementById('process-latency').textContent = metrics.process_latency.toFixed(1);
                document.getElementById('send-latency').textContent = metrics.send_latency.toFixed(1);
                document.getElementById('total-latency').textContent = metrics.total_latency.toFixed(1);
                document.getElementById('avg-latency').textContent = metrics.avg_latency.toFixed(1);
                document.getElementById('request-count').textContent = metrics.count;
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