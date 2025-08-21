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

from meeting_transcript_manager import MeetingTranscriptManager

load_dotenv(override=True)

# Global instances
app = FastAPI()
transcript_manager = MeetingTranscriptManager()
active_meetings: Dict[str, Dict] = {}
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


async def log_transcript_message(meeting_id: str, text: str, speaker: str, message_type: str, trigger_ui_update: bool = True):
    """Helper function to log transcript messages"""
    try:
        await transcript_manager.log_message(
            meeting_id=meeting_id,
            text=text,
            speaker=speaker,
            confidence=1.0,
            message_type=message_type
        )
        
        # Broadcast complete message to live transcript viewers
        await broadcast_transcript_update(meeting_id, {
            "timestamp": datetime.now().isoformat(),
            "speaker": speaker,
            "text": text,
            "type": message_type,
            "streaming": False
        })
        
        # Also broadcast a meeting list update signal to refresh transcript counts (only for final transcriptions)
        if trigger_ui_update:
            await broadcast_transcript_update(meeting_id, {
                "timestamp": datetime.now().isoformat(),
                "type": "meeting_list_update",
                "message": "Transcript count updated"
            })
        
    except Exception as e:
        logger.error(f"❌ Error logging {speaker} message: {e}")


class TranscriptProcessor(FrameProcessor):
    """Processor that handles transcription frames and logs them"""
    
    def __init__(self, meeting_id: str):
        super().__init__()
        self.meeting_id = meeting_id
        
    async def process_frame(self, frame, direction):
        """Process transcription frames"""
        # First, call the parent process_frame to handle StartFrame and other system frames
        await super().process_frame(frame, direction)
        
        # Then handle our specific transcription logic
        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            # Check if this is an interim or final transcription
            is_interim = getattr(frame, 'interim', False)
            
            if is_interim:
                # For interim results, only broadcast streaming chunks
                await broadcast_transcript_update(self.meeting_id, {
                    "timestamp": datetime.now().isoformat(),
                    "speaker": "participant",
                    "text": frame.text,
                    "type": "transcription_chunk",
                    "streaming": True
                })
            else:
                # For final results, log to database and broadcast final transcription
                await log_transcript_message(
                    self.meeting_id, 
                    frame.text, 
                    "participant", 
                    "transcription"
                )
        
        # Always push the frame downstream
        await self.push_frame(frame, direction)


# Transport configurations
transport_params = {
    "webrtc": lambda: TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=False,  # No audio output for meeting assistant
        vad_analyzer=SileroVADAnalyzer(),
    ),
}


async def create_meeting_pipeline(transport: BaseTransport, meeting_id: str) -> Tuple[PipelineTask, object]:
    """Create the meeting transcription pipeline"""
    
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

    # Create transcript processor for this meeting
    transcript_processor = TranscriptProcessor(meeting_id)
    
    # Build pipeline - simplified for transcription only
    pipeline = Pipeline([
        transport.input(),           # Transport user input (audio)
        stt,                        # Speech to text (Deepgram Nova-3)
        transcript_processor,       # Log transcriptions
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            allow_interruptions=True,
        ),
        cancel_on_idle_timeout=False,  # Don't cancel on idle timeout for meetings
    )
    
    return task, transcript_processor


async def run_meeting(transport: BaseTransport, meeting_id: str):
    """Run a meeting transcription session"""
    logger.info(f"🚀 Starting meeting session: {meeting_id}")
    
    # Start meeting tracking
    await transcript_manager.start_meeting(meeting_id)
    
    # Create pipeline
    task, transcript_processor = await create_meeting_pipeline(transport, meeting_id)
    
    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"🔗 ESP32 client connected to meeting {meeting_id}")
        # Store transport reference for immediate stopping capability
        active_meetings[meeting_id] = {
            "start_time": datetime.now(),
            "client": client,
            "task": task,
            "transport": transport,
            "status": "active"
        }
        
        # Broadcast meeting start status
        await broadcast_transcript_update(meeting_id, {
            "timestamp": datetime.now().isoformat(),
            "type": "meeting_started",
            "message": "Meeting started - transcription active"
        })

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"❌ ESP32 client disconnected from meeting {meeting_id}")
        
        # Broadcast disconnect status to web interface
        await broadcast_transcript_update(meeting_id, {
            "timestamp": datetime.now().isoformat(),
            "type": "meeting_ended", 
            "message": "Meeting ended - ESP32 disconnected"
        })
        
        # End meeting tracking and cleanup
        await transcript_manager.end_meeting(meeting_id)
        active_meetings.pop(meeting_id, None)
        logger.info(f"Meeting {meeting_id} cleaned up successfully")
        
        # Clean up
        if meeting_id in active_meetings:
            active_meetings[meeting_id]["status"] = "ended"
            del active_meetings[meeting_id]
            
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


async def broadcast_transcript_update(meeting_id: str, message: Dict):
    """Broadcast transcript updates to all connected web clients"""
    if not live_transcript_connections:
        return
        
    update_data = {
        "meeting_id": meeting_id,
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
        
        # Handle ESP32 disconnect messages via data channel
        @webrtc_connection.event_handler("app-message")
        async def handle_app_message(conn: SmallWebRTCConnection, message: dict):
            """Handle application messages from ESP32 via data channel."""
            message_type = message.get("type")
            logger.info(f"📨 Received app message: {message}")
            
            if message_type == "meeting.disconnect":
                logger.info(f"🛑 Received explicit disconnect from ESP32 for meeting {meeting_id}")
                
                # Broadcast disconnect to web interface
                await broadcast_transcript_update(meeting_id, {
                    "timestamp": datetime.now().isoformat(),
                    "type": "meeting_ended",
                    "message": "Meeting ended - ESP32 disconnected gracefully"
                })
                
                # Stop the running meeting pipeline and transport immediately  
                meeting_data = active_meetings.get(meeting_id)
                if meeting_data:
                    # Stop the transport first to immediately stop audio frame reading
                    if "transport" in meeting_data:
                        transport = meeting_data["transport"]
                        logger.info(f"Stopping transport for {meeting_id}")
                        try:
                            from pipecat.frames.frames import EndFrame
                            await transport.stop(EndFrame())
                            logger.info(f"Transport stopped successfully for {meeting_id}")
                        except Exception as e:
                            logger.warning(f"Error stopping transport: {e}")
                    
                    # Then stop the pipeline task
                    if "task" in meeting_data:
                        pipeline_task = meeting_data["task"]
                        if pipeline_task:
                            logger.info(f"Stopping pipeline task for {meeting_id}")
                            try:
                                await pipeline_task.stop_when_done()
                                logger.info(f"Pipeline task scheduled to stop for {meeting_id}")
                            except Exception as e:
                                logger.warning(f"Error stopping pipeline task: {e}")
                
                # End meeting tracking and cleanup
                await transcript_manager.end_meeting(meeting_id)
                active_meetings.pop(meeting_id, None)
                
                # Connection will be closed automatically by the transport
                logger.info(f"Meeting {meeting_id} ended via explicit disconnect message")
        
        # Handle connection cleanup
        @webrtc_connection.event_handler("closed")
        async def handle_webrtc_disconnected(conn: SmallWebRTCConnection):
            """Handle WebRTC connection closure and cleanup."""
            logger.info(f"🔌 WebRTC connection closed for pc_id: {conn.pc_id}")
            webrtc_connections.pop(conn.pc_id, None)
            
            # Also clean up meeting if it exists
            for meeting_id_key, meeting_data in list(active_meetings.items()):
                if meeting_data.get("webrtc_connection") == conn:
                    logger.info(f"🛑 Ending meeting {meeting_id_key} due to WebRTC connection closure")
                    
                    # Broadcast disconnect to web interface
                    await broadcast_transcript_update(meeting_id_key, {
                        "timestamp": datetime.now().isoformat(),
                        "type": "meeting_ended",
                        "message": "Meeting ended - WebRTC connection closed"
                    })
                    
                    await transcript_manager.end_meeting(meeting_id_key)
                    active_meetings.pop(meeting_id_key, None)
                    logger.info(f"Meeting {meeting_id_key} cleaned up from WebRTC closure")
                    break
        
        # Create transport with transcript logging (audio input only)
        transport_params_instance = TransportParams(
            audio_in_enabled=True,
            audio_out_enabled=False,  # No audio output for meetings
            vad_analyzer=SileroVADAnalyzer(),
        )
        
        transport = SmallWebRTCTransport(
            params=transport_params_instance, 
            webrtc_connection=webrtc_connection
        )
        
        # Generate meeting ID for this WebRTC connection
        meeting_id = f"meeting_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{webrtc_connection.pc_id}"
        
        # Store connection reference in meeting data (PipelineTask will be added later in run_meeting)
        active_meetings[meeting_id] = {
            "webrtc_connection": webrtc_connection,
            "status": "connecting"
        }
        
        # Start meeting in background
        background_tasks.add_task(run_meeting, transport, meeting_id)
        
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
@app.get("/")
async def get_index():
    """Serve the main web interface"""
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Meeting Assistant - Live Transcription</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
            .container { max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            .header { border-bottom: 2px solid #007bff; padding-bottom: 10px; margin-bottom: 20px; }
            .meetings { margin-bottom: 30px; }
            .meeting-item { border: 1px solid #ddd; margin: 10px 0; padding: 15px; border-radius: 5px; cursor: pointer; background: #fafafa; }
            .meeting-item:hover { background: #e9ecef; }
            .meeting-item.active { border-color: #28a745; background: #f8fff9; }
            .live-transcript { border: 2px solid #28a745; padding: 15px; border-radius: 5px; background: #f8fff9; }
            .message { margin: 10px 0; padding: 8px; border-radius: 4px; }
            .participant-message { background: #e3f2fd; border-left: 4px solid #2196f3; }
            .timestamp { font-size: 0.8em; color: #666; }
            .status { display: inline-block; padding: 4px 8px; border-radius: 12px; font-size: 0.8em; }
            .status.active { background: #d4edda; color: #155724; }
            .status.ended { background: #f8d7da; color: #721c24; }
            .status.connecting { background: #fff3cd; color: #856404; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🎙️ Meeting Assistant</h1>
                <p>Real-time meeting transcription and history</p>
            </div>
            
            <div class="live-transcript">
                <h3>Live Transcript <span id="live-status" class="status">Waiting for meeting...</span></h3>
                <div id="live-messages"></div>
            </div>
            
            <div class="meetings">
                <h3>Meeting History</h3>
                <div id="meeting-list">Loading...</div>
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
                
                if (data.message && data.message.type === 'meeting_ended') {
                    liveStatus.textContent = 'Meeting Ended';
                    liveStatus.className = 'status ended';
                    
                    // Clear live messages and refresh meeting list immediately
                    setTimeout(() => {
                        liveMessages.innerHTML = '';
                        loadMeetings();
                    }, 100);
                } else if (data.message && data.message.type === 'meeting_started') {
                    liveStatus.textContent = 'Meeting Active';
                    liveStatus.className = 'status active';
                    liveMessages.innerHTML = '';
                    // Refresh meeting list to show active status
                    loadMeetings();
                } else if (data.message && data.message.type === 'meeting_list_update') {
                    // Refresh meeting list when transcript counts change (throttled)
                    throttledLoadMeetings();
                } else if (data.message) {
                    addLiveMessage(data.message);
                }
            };
            
            ws.onclose = function() {
                liveStatus.textContent = 'Disconnected';
                liveStatus.className = 'status ended';
            };
            
            let currentStreamingMessage = null;
            let lastMeetingListUpdate = 0;
            const MEETING_LIST_UPDATE_THROTTLE = 2000; // Throttle updates to max once per 2 seconds
            
            function throttledLoadMeetings() {
                const now = Date.now();
                if (now - lastMeetingListUpdate > MEETING_LIST_UPDATE_THROTTLE) {
                    lastMeetingListUpdate = now;
                    loadMeetings();
                }
            }
            
            function addLiveMessage(message) {
                if (message.streaming && message.type === 'transcription_chunk') {
                    // Handle streaming transcription - update or create streaming message
                    if (!currentStreamingMessage) {
                        // Create new streaming message
                        currentStreamingMessage = document.createElement('div');
                        currentStreamingMessage.className = `message ${message.speaker}-message streaming`;
                        currentStreamingMessage.innerHTML = `
                            <div><strong>${message.speaker}:</strong> <span class="streaming-text">${message.text}</span></div>
                            <div class="timestamp">${new Date(message.timestamp).toLocaleTimeString()}</div>
                        `;
                        liveMessages.appendChild(currentStreamingMessage);
                    } else {
                        // Replace content with latest streaming text (not append)
                        const streamingText = currentStreamingMessage.querySelector('.streaming-text');
                        streamingText.textContent = message.text;
                    }
                } else if (message.type === 'transcription' && !message.streaming) {
                    // Handle final transcription - only show final messages, skip if we have streaming version
                    if (currentStreamingMessage) {
                        // Remove the streaming message and replace with final
                        currentStreamingMessage.remove();
                        currentStreamingMessage = null;
                    }
                    
                    // Create final message
                    const messageDiv = document.createElement('div');
                    messageDiv.className = `message ${message.speaker}-message`;
                    messageDiv.innerHTML = `
                        <div><strong>${message.speaker}:</strong> ${message.text}</div>
                        <div class="timestamp">${new Date(message.timestamp).toLocaleTimeString()}</div>
                    `;
                    liveMessages.appendChild(messageDiv);
                }
                liveMessages.scrollTop = liveMessages.scrollHeight;
            }
            
            // Load meeting history
            async function loadMeetings() {
                try {
                    const response = await fetch('/api/meetings');
                    const meetings = await response.json();
                    
                    const listDiv = document.getElementById('meeting-list');
                    listDiv.innerHTML = '';
                    
                    if (meetings.length === 0) {
                        listDiv.innerHTML = '<p>No meetings yet.</p>';
                        return;
                    }
                    
                    meetings.forEach(meeting => {
                        const meetingDiv = document.createElement('div');
                        meetingDiv.className = `meeting-item ${meeting.status === 'active' ? 'active' : ''}`;
                        meetingDiv.innerHTML = `
                            <div><strong>${meeting.title || 'Untitled Meeting'}</strong> 
                                <span class="status ${meeting.status}">${meeting.status}</span>
                            </div>
                            <div class="timestamp">
                                ${new Date(meeting.start_time).toLocaleString()} 
                                (${meeting.message_count || 0} transcripts)
                            </div>
                        `;
                        meetingDiv.onclick = () => viewMeeting(meeting.meeting_id);
                        listDiv.appendChild(meetingDiv);
                    });
                } catch (error) {
                    console.error('Error loading meetings:', error);
                }
            }
            
            function viewMeeting(meetingId) {
                const encodedMeetingId = encodeURIComponent(meetingId);
                window.open(`/meeting/${encodedMeetingId}`, '_blank');
            }
            
            // Load meetings on page load
            loadMeetings();
            
            // Refresh meetings every 30 seconds
            setInterval(loadMeetings, 30000);
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.get("/api/meetings")
async def get_meetings():
    """Get list of meetings"""
    try:
        logger.info(f"📋 Fetching meetings from database...")
        meetings = await transcript_manager.get_meetings()
        
        # Add active status for currently running meetings and fix field names
        for meeting in meetings:
            if meeting['meeting_id'] in active_meetings:
                meeting['status'] = 'active'
            else:
                meeting['status'] = 'ended'
            
            # Map database field to frontend expected field
            meeting['message_count'] = meeting.get('transcript_count', 0)
                
        logger.info(f"📊 Found {len(meetings)} meetings in database")
        return JSONResponse(content=meetings)
    except Exception as e:
        logger.error(f"❌ Error fetching meetings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/meeting/{meeting_id}")
async def view_meeting(meeting_id: str):
    """View a specific meeting"""
    try:
        from urllib.parse import unquote
        decoded_meeting_id = unquote(meeting_id)
        logger.info(f"📖 Attempting to view meeting: {decoded_meeting_id}")
        
        meeting = await transcript_manager.get_meeting(decoded_meeting_id)
        
        if not meeting:
            raise HTTPException(status_code=404, detail="Meeting not found")
        
        # Check if this is an active meeting
        is_active = decoded_meeting_id in active_meetings
        
        # Generate HTML for meeting view
        live_indicator = ' <span style="color: green;">🔴 LIVE</span>' if is_active else ''
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Meeting: {meeting.get('title', 'Untitled')}</title>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
                .container {{ max-width: 800px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }}
                .header {{ border-bottom: 2px solid #007bff; padding-bottom: 10px; margin-bottom: 20px; }}
                .message {{ margin: 15px 0; padding: 12px; border-radius: 8px; }}
                .participant-message {{ background: #e3f2fd; border-left: 4px solid #2196f3; }}
                .timestamp {{ font-size: 0.8em; color: #666; margin-top: 5px; }}
                .back-link {{ color: #007bff; text-decoration: none; }}
                .back-link:hover {{ text-decoration: underline; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <a href="/" class="back-link">← Back to All Meetings</a>
                    <h1>{meeting.get('title', 'Untitled Meeting')}{live_indicator}</h1>
                    <p>Started: {meeting.get('start_time', 'Unknown')} | Transcripts: {len(meeting.get('transcripts', []))}</p>
                </div>
                
                <div class="transcripts" id="transcripts-container">
        """
        
        # Add transcripts
        for transcript in meeting.get('transcripts', []):
            text = transcript.get('text', '')
            speaker = transcript.get('speaker', 'participant')
            timestamp = transcript.get('timestamp', '')
            
            html_content += f"""
                    <div class="message {speaker}-message">
                        <strong>{speaker.title()}:</strong> {text}
                        <div class="timestamp">{timestamp}</div>
                    </div>
            """
        
        html_content += f"""
                </div>
            </div>
            
            <script>
                // Live updates for active meetings
                const isActive = {str(is_active).lower()};
                
                if (isActive) {{
                    const ws = new WebSocket(`ws://${{window.location.host}}/transcript-stream`);
                    const transcriptsContainer = document.getElementById('transcripts-container');
                    
                    ws.onmessage = function(event) {{
                        const data = JSON.parse(event.data);
                        
                        // Only show updates for this specific meeting
                        if (data.meeting_id === '{decoded_meeting_id}') {{
                            if (data.message && data.message.type === 'meeting_ended') {{
                                const headerTitle = document.querySelector('h1');
                                headerTitle.innerHTML = headerTitle.innerHTML.replace('🔴 LIVE', '❌ ENDED');
                            }} else if (data.message && data.message.type === 'transcription') {{
                                addLiveTranscript(data.message);
                            }}
                        }}
                    }};
                    
                    function addLiveTranscript(message) {{
                        const messageDiv = document.createElement('div');
                        messageDiv.className = `message ${{message.speaker}}-message`;
                        messageDiv.innerHTML = `
                            <strong>${{message.speaker.charAt(0).toUpperCase() + message.speaker.slice(1)}}:</strong> ${{message.text}}
                            <div class="timestamp">${{new Date(message.timestamp).toLocaleTimeString()}}</div>
                        `;
                        transcriptsContainer.appendChild(messageDiv);
                        transcriptsContainer.scrollTop = transcriptsContainer.scrollHeight;
                    }}
                }}
            </script>
        </body>
        </html>
        """
        
        return HTMLResponse(content=html_content)
        
    except Exception as e:
        logger.error(f"Error viewing meeting {meeting_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description="Meeting Assistant Server")
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
    
    logger.info(f"Starting Meeting Assistant Server on {args.host}:{args.port}")
    logger.info(f"Web interface: http://{args.host}:{args.port}")
    logger.info(f"ESP32 HTTP endpoint: http://{args.host}:{args.port}/api/offer")
    
    uvicorn.run(
        "meeting_server:app",
        host=args.host,
        port=args.port,
        reload=args.reload
    )


if __name__ == "__main__":
    main()