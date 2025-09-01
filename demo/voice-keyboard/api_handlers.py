"""
API Handlers for Voice Keyboard Server

Handles WebRTC negotiation, health checks, debug endpoints, and WebSocket connections.
"""

import asyncio
import os
import re
from datetime import datetime
from typing import Dict, List

from fastapi import BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from loguru import logger

from pipecat.transports.network.webrtc_connection import SmallWebRTCConnection

from session_manager import active_sessions, run_voice_session, create_webrtc_transport, cleanup_session


# WebRTC connections for ESP32 clients
webrtc_connections: Dict[str, SmallWebRTCConnection] = {}

# Live transcript connections
live_transcript_connections: List[WebSocket] = []


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


async def handle_webrtc_offer(request: dict, background_tasks: BackgroundTasks, broadcast_callback=None):
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
                if broadcast_callback:
                    await broadcast_callback(session_id, {
                        "timestamp": datetime.now().isoformat(),
                        "type": "esp32_debug",
                        "message": f"ESP32: {debug_msg}",
                        "esp32_timestamp": timestamp
                    })
                
            elif message_type == "session.disconnect":
                logger.info(f"🛑 Received explicit disconnect from ESP32 for session {session_id}")
                
                # Broadcast disconnect to web interface
                if broadcast_callback:
                    await broadcast_callback(session_id, {
                        "timestamp": datetime.now().isoformat(),
                        "type": "session_ended",
                        "message": "Session ended - ESP32 disconnected gracefully"
                    })
                
                # Clean up session
                await cleanup_session(session_id, broadcast_callback)
                
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
                    if broadcast_callback:
                        await broadcast_callback(session_id_key, {
                            "timestamp": datetime.now().isoformat(),
                            "type": "session_ended",
                            "message": "Session ended - WebRTC connection closed"
                        })
                    
                    active_sessions.pop(session_id_key, None)
                    logger.info(f"Session {session_id_key} cleaned up from WebRTC closure")
                    break
        
        # Create transport with voice keyboard settings
        transport = create_webrtc_transport(webrtc_connection)
        
        # Store connection reference in session data (PipelineTask will be added later in run_voice_session)
        active_sessions[session_id] = {
            "webrtc_connection": webrtc_connection,
            "status": "connecting"
        }
        
        # Start voice session in background
        background_tasks.add_task(run_voice_session, transport, session_id, webrtc_connection, broadcast_callback)
        
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


async def health_check():
    """Simple health check endpoint for ESP32 to verify server availability"""
    return {
        "status": "healthy",
        "service": "voice-keyboard-server",
        "timestamp": datetime.now().isoformat(),
        "active_sessions": len(active_sessions),
        "webrtc_connections": len(webrtc_connections)
    }


async def receive_debug_message(request_data: dict, broadcast_callback=None):
    """Receive debug messages from ESP32 via HTTP POST"""
    try:
        message = request_data.get("message", "Unknown debug message")
        timestamp = request_data.get("timestamp", 0)
        session_id = request_data.get("session_id", "esp32_debug")
        
        logger.info(f"🔧 ESP32 Debug: {message} (t={timestamp}) [session: {session_id}]")
        
        # Also broadcast to web interface for visibility
        if broadcast_callback:
            await broadcast_callback(session_id, {
                "timestamp": datetime.now().isoformat(),
                "type": "esp32_debug",
                "message": f"ESP32: {message}",
                "esp32_timestamp": timestamp
            })
        
        return {"status": "success", "message": "Debug message received"}
    
    except Exception as e:
        logger.error(f"Error processing debug message: {e}")
        return {"status": "error", "message": str(e)}


async def handle_transcript_websocket(websocket: WebSocket):
    """Handle WebSocket connections for live transcript streaming"""
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