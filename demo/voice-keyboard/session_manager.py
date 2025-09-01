"""
Voice Keyboard Session Management

Handles voice pipeline creation, session management, and WebRTC transport configuration.
"""

import os
from datetime import datetime
from typing import Dict, Tuple

from loguru import logger
from deepgram import LiveOptions

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.transports.network.small_webrtc import SmallWebRTCTransport
from pipecat.transports.network.webrtc_connection import SmallWebRTCConnection

from text_sender import TextSenderProcessor


# Global session storage
active_sessions: Dict[str, Dict] = {}


def get_transport_params():
    """Get transport parameters for voice keyboard (audio input only)"""
    return TransportParams(
        audio_in_enabled=True,
        audio_out_enabled=False,  # No audio output for voice keyboard
        vad_analyzer=SileroVADAnalyzer(),
    )


async def create_voice_keyboard_pipeline(
    transport: BaseTransport, 
    session_id: str, 
    webrtc_connection: SmallWebRTCConnection,
    broadcast_callback=None
) -> Tuple[PipelineTask, TextSenderProcessor]:
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
    text_sender = TextSenderProcessor(session_id, webrtc_connection, broadcast_callback)
    
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


async def run_voice_session(
    transport: BaseTransport, 
    session_id: str, 
    webrtc_connection: SmallWebRTCConnection,
    broadcast_callback=None
):
    """Run a voice keyboard session"""
    logger.info(f"🚀 Starting voice keyboard session: {session_id}")
    
    # Create pipeline
    task, text_sender = await create_voice_keyboard_pipeline(transport, session_id, webrtc_connection, broadcast_callback)
    
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
        if broadcast_callback:
            await broadcast_callback(session_id, {
                "timestamp": datetime.now().isoformat(),
                "type": "session_started",
                "message": "Voice typing session started"
            })

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"❌ ESP32 client disconnected from session {session_id}")
        
        # Broadcast disconnect status to web interface
        if broadcast_callback:
            await broadcast_callback(session_id, {
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


def create_webrtc_transport(webrtc_connection: SmallWebRTCConnection) -> SmallWebRTCTransport:
    """Create WebRTC transport with voice keyboard settings"""
    transport_params_instance = get_transport_params()
    
    return SmallWebRTCTransport(
        params=transport_params_instance, 
        webrtc_connection=webrtc_connection
    )


async def cleanup_session(session_id: str, broadcast_callback=None):
    """Clean up a voice session and its resources"""
    session_data = active_sessions.get(session_id)
    if not session_data:
        return
        
    logger.info(f"🛑 Cleaning up session {session_id}")
    
    # Close WebRTC connection first
    if "webrtc_connection" in session_data:
        webrtc_connection = session_data["webrtc_connection"]
        try:
            await webrtc_connection.close()
            logger.info(f"WebRTC connection closed for {session_id}")
        except Exception as e:
            logger.warning(f"Error closing WebRTC connection: {e}")
    
    # Stop the transport
    if "transport" in session_data:
        transport = session_data["transport"]
        try:
            from pipecat.frames.frames import EndFrame
            end_frame = EndFrame()
            if hasattr(transport, 'input') and transport.input():
                await transport.input().stop(end_frame)
            if hasattr(transport, 'output') and transport.output():
                await transport.output().stop(end_frame)
            logger.info(f"Transport stopped for {session_id}")
        except Exception as e:
            logger.warning(f"Error stopping transport: {e}")
    
    # Stop the pipeline task
    if "task" in session_data:
        pipeline_task = session_data["task"]
        if pipeline_task:
            try:
                await pipeline_task.stop_when_done()
                logger.info(f"Pipeline task stopped for {session_id}")
            except Exception as e:
                logger.warning(f"Error stopping pipeline task: {e}")
    
    # Remove from active sessions
    active_sessions.pop(session_id, None)
    
    # Broadcast cleanup
    if broadcast_callback:
        await broadcast_callback(session_id, {
            "timestamp": datetime.now().isoformat(),
            "type": "session_ended",
            "message": "Session ended and cleaned up"
        })
    
    logger.info(f"Session {session_id} cleanup completed")