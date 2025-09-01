"""
TextSenderProcessor for Voice Keyboard

Handles sending transcribed text back to ESP32 via WebRTC data channel.
"""

from datetime import datetime
from loguru import logger

from pipecat.frames.frames import TranscriptionFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.transports.network.webrtc_connection import SmallWebRTCConnection


class TextSenderProcessor(FrameProcessor):
    """Processor that sends transcribed text back to ESP32 via data channel"""
    
    def __init__(self, session_id: str, webrtc_connection: SmallWebRTCConnection, broadcast_callback=None):
        super().__init__()
        self.session_id = session_id
        self.webrtc_connection = webrtc_connection
        self.broadcast_callback = broadcast_callback
        
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
                    if self.broadcast_callback:
                        await self.broadcast_callback(self.session_id, {
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
                if self.broadcast_callback:
                    await self.broadcast_callback(self.session_id, {
                        "timestamp": datetime.now().isoformat(),
                        "text": frame.text,
                        "type": "transcription_interim",
                        "streaming": True
                    })
        
        # Always push the frame downstream
        await self.push_frame(frame, direction)