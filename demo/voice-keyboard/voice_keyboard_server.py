import argparse
import asyncio
import getpass
import json
import os
import platform
import re
import socket
import subprocess
import threading
import time
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

# Configuration and provisioning system
provisioning_enabled = False
provisioning_thread = None
provisioning_requests: List[Dict] = []  # Queue for provisioning requests from ESP32

# Debug mode flag
debug_mode = False


# USB JTAG Provisioning System
class ESP32USBProvisioningManager:
    """Manages ESP32 device provisioning via USB JTAG communication"""
    
    def __init__(self):
        self.active = False
        self.usb_devices = []
        self.serial_connections = {}  # Keep persistent connections {port: serial_obj}
        # Initialize with empty WiFi credentials - will be prompted when needed
        self.device_configs = {
            "wifi_ssid": "",
            "wifi_password": "",
            "server_ip": os.getenv("HOST_IP", "localhost"),
            "server_port": os.getenv("SERVER_PORT", "8765")
        }
        
        # Note: server_ip will be set properly in main() function based on actual network detection
            
        self.wifi_credentials_set = False
        self.usb_devices = []  # Track connected USB devices
        # Simplified provisioning without WiFi network matching
        self.reset_on_startup = False  # Whether to reset ESP32 on startup
        
    def start_monitoring(self):
        """Start monitoring thread for ESP32 USB JTAG provisioning"""
        if not self.active:
            self.active = True
            thread = threading.Thread(target=self._monitor_usb_provisioning, daemon=True)
            thread.start()
            logger.info("📡 ESP32 USB JTAG provisioning monitor started")
            
    def stop_monitoring(self):
        """Stop the USB provisioning monitor"""
        self.active = False
        # Close all serial connections
        for port, ser in self.serial_connections.items():
            try:
                ser.close()
            except Exception:
                pass
        self.serial_connections.clear()
        
    def _monitor_usb_provisioning(self):
        """Background thread to monitor for ESP32 USB JTAG provisioning requests"""
        logger.info("🔍 Starting ESP32 USB JTAG provisioning monitor thread")
        
        loop_count = 0
        while self.active:
            try:
                loop_count += 1
                
                # Debug heartbeat every 30 seconds
                if debug_mode and loop_count % 15 == 0:
                    print(f"🔄 [USB MONITOR] Heartbeat #{loop_count}, devices: {len(self.usb_devices)}")
                
                # Scan for new USB devices
                self._scan_usb_devices()
                
                # Monitor active USB connections for provisioning messages
                self._monitor_usb_messages()
                    
            except Exception as e:
                logger.error(f"Error in USB provisioning monitor: {e}")
                if debug_mode:
                    print(f"❌ [USB MONITOR ERROR] {e}")
                
            time.sleep(2)  # Check every 2 seconds
            
        logger.info("🔍 USB monitoring thread stopped")
            
    def _monitor_usb_messages(self):
        """Monitor USB JTAG devices for provisioning messages"""
        try:
            import serial
            import json
            
            # Update persistent connections
            current_ports = {device['port'] for device in self.usb_devices}
            
            # Close connections for removed devices
            for port in list(self.serial_connections.keys()):
                if port not in current_ports:
                    try:
                        self.serial_connections[port].close()
                        del self.serial_connections[port]
                        if debug_mode:
                            print(f"🔌 [USB] Closed connection to removed device: {port}")
                    except Exception:
                        pass
            
            # Monitor each device with persistent connections
            for device in self.usb_devices:
                port = device['port']
                
                # Open persistent connection if not exists
                if port not in self.serial_connections:
                    try:
                        # Try to open with a longer timeout for initial connection
                        ser = serial.Serial(port, 115200, timeout=0.1, write_timeout=1.0)
                        # Give device time to initialize
                        time.sleep(0.1)
                        ser.reset_input_buffer()
                        ser.reset_output_buffer()
                        self.serial_connections[port] = ser
                        if debug_mode:
                            print(f"🔌 [USB] Opened persistent connection to: {port}")
                    except (serial.SerialException, OSError) as e:
                        if debug_mode:
                            print(f"❌ [USB] Failed to open {port}: {e} - Will retry next cycle")
                        continue
                
                ser = self.serial_connections[port]
                try:
                    if ser.in_waiting > 0:
                        if debug_mode:
                            print(f"📨 [USB DATA] {port}: {ser.in_waiting} bytes waiting")
                        
                        # Read available data line by line
                        lines_read = 0
                        while ser.in_waiting > 0 and lines_read < 15:
                            try:
                                line = ser.readline().decode('utf-8', errors='ignore').strip()
                                lines_read += 1
                                
                                if not line:
                                    continue
                                
                                # Look for provisioning messages (now single-line JSON)
                                if line.startswith('PROVISIONING_REQUEST:'):
                                    json_data = line.replace('PROVISIONING_REQUEST:', '').strip()
                                    logger.info(f"📡 Received USB provisioning request from {port}")
                                    if debug_mode:
                                        print(f"📡 [PROVISIONING REQUEST] {port}: {json_data}")
                                    self._handle_usb_provisioning_request(port, json_data)
                                
                                # Look for provisioning status updates
                                elif line.startswith('PROVISIONING_STATUS:'):
                                    json_data = line.replace('PROVISIONING_STATUS:', '').strip()
                                    if debug_mode:
                                        print(f"📱 [PROVISIONING STATUS] {port}: {json_data}")
                                    self._handle_usb_provisioning_status(port, json_data)
                                    
                                else:
                                    # Regular log messages - show all lines in debug mode
                                    if debug_mode and line.strip():
                                        print(f"💬 [USB DEBUG {port}] {line}")
                                        
                            except UnicodeDecodeError:
                                continue
                                
                except serial.SerialException as e:
                    # Connection lost - remove it and retry
                    if debug_mode:
                        print(f"❌ [SERIAL ERROR] {port}: {e} - Will retry connection")
                    try:
                        ser.close()
                    except Exception:
                        pass
                    del self.serial_connections[port]
                    continue
                except OSError as e:
                    # Device not configured or similar OS-level error
                    if debug_mode:
                        print(f"❌ [USB DEVICE ERROR] {port}: {e} - Removing connection")
                    try:
                        ser.close()
                    except Exception:
                        pass
                    del self.serial_connections[port]
                    continue
                except Exception as e:
                    if debug_mode:
                        print(f"❌ [USB ERROR] {port}: {e} - Removing connection")
                    logger.debug(f"USB message monitor error on {port}: {e}")
                    try:
                        ser.close()
                    except Exception:
                        pass
                    del self.serial_connections[port]
                    continue
                    
        except ImportError:
            # pyserial not available
            pass
    
    # WiFi network detection removed - simplified provisioning approach
    
    def _reset_esp32_device(self, port):
        """Reset ESP32 device via serial control lines (DTR/RTS)"""
        try:
            import time
            import serial
            
            logger.info(f"🔄 Resetting ESP32 on {port}")
            
            # Open serial connection briefly to send reset signal
            ser = serial.Serial(port, 115200, timeout=1)
            
            # ESP32 reset sequence using DTR and RTS lines
            ser.setDTR(False)  # DTR = False
            ser.setRTS(True)   # RTS = True (EN = False, boot mode)
            time.sleep(0.1)
            
            ser.setDTR(True)   # DTR = True  (IO0 = False, boot mode)
            ser.setRTS(True)   # RTS = True  (EN = False, reset)
            time.sleep(0.1)
            
            ser.setRTS(False)  # RTS = False (EN = True, release reset)
            time.sleep(0.1)
            
            ser.setDTR(False)  # DTR = False (IO0 = True, normal mode)
            time.sleep(0.5)    # Give ESP32 time to boot
            
            ser.close()
            logger.info(f"✅ ESP32 reset completed on {port}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to reset ESP32 on {port}: {e}")
            return False
    
    def reset_all_esp32_devices(self):
        """Reset all detected ESP32 devices"""
        if not self.usb_devices:
            logger.warning("⚠️ No ESP32 devices found to reset")
            return False
        
        success_count = 0
        for device in self.usb_devices:
            if self._reset_esp32_device(device['port']):
                success_count += 1
        
        logger.info(f"🔄 Reset {success_count}/{len(self.usb_devices)} ESP32 device(s)")
        return success_count > 0
    
    # WiFi network matching removed - simplified approach
    
    def _handle_usb_provisioning_status(self, port: str, json_data: str):
        """Handle provisioning status updates from ESP32"""
        try:
            import json
            
            status = json.loads(json_data)
            device_id = status.get('device_id', 'unknown')
            phase = status.get('phase', 'unknown')
            status_msg = status.get('status', 'unknown')
            message = status.get('message', '')
            
            logger.info(f"📱 USB JTAG status from {device_id} on {port}: {phase} - {status_msg}")
            
            # When ESP32 reports WiFi configuration, check network compatibility
            if phase == "config" and status_msg == "loaded":
                # ESP32 is reporting its stored WiFi configuration
                # We could request the SSID here or check it when it tries to connect
                pass
            
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in status update from {port}: {e}")
        except Exception as e:
            logger.error(f"Error handling status update from {port}: {e}")
            
    def _handle_usb_provisioning_request(self, port: str, json_data: str):
        """Handle provisioning request received via USB JTAG"""
        try:
            import json
            import serial
            
            request = json.loads(json_data)
            request_type = request.get('request_type')
            device_id = request.get('device_id', 'unknown')
            
            logger.info(f"📡 USB JTAG provisioning request from {device_id} on {port}: {request_type}")
            
            # Simplified provisioning - prompt for WiFi credentials only when needed
            needs_wifi_config = request_type in ["wifi_config", "full_config"]
            
            if needs_wifi_config and not self.wifi_credentials_set:
                logger.info("📶 ESP32 needs WiFi configuration")
                self._prompt_for_wifi_credentials()
            
            # Prepare response
            response = {
                "type": "provisioning_response",
                "request_type": request_type,
                "status": "success"
            }
            
            if request_type == "wifi_config":
                response.update({
                    "wifi_ssid": self.device_configs["wifi_ssid"],
                    "wifi_password": self.device_configs["wifi_password"]
                })
            elif request_type == "server_config":
                response.update({
                    "server_ip": self.device_configs["server_ip"],
                    "server_port": int(self.device_configs["server_port"]),  # Send as number
                    "server_url": f"http://{self.device_configs['server_ip']}:{self.device_configs['server_port']}/api/offer"
                })
            elif request_type == "full_config":
                config_with_port = self.device_configs.copy()
                config_with_port["server_port"] = int(self.device_configs["server_port"])  # Send as number
                response.update({
                    **config_with_port,
                    "server_url": f"http://{self.device_configs['server_ip']}:{self.device_configs['server_port']}/api/offer"
                })
            
            # Send response via USB JTAG
            response_json = json.dumps(response)
            response_message = f"PROVISIONING_RESPONSE: {response_json}\n"
            
            try:
                with serial.Serial(port, 115200, timeout=1) as ser:
                    # Clear any pending data
                    ser.reset_input_buffer()
                    ser.reset_output_buffer()
                    
                    # Send response
                    bytes_written = ser.write(response_message.encode('utf-8'))
                    ser.flush()  # Ensure data is sent
                    
                    logger.info(f"📤 Sent provisioning response to {device_id} on {port} ({bytes_written} bytes)")
                    if debug_mode:
                        print(f"📤 [PROVISIONING RESPONSE] {port}: {response_json}")
                        print(f"   Server IP being sent to ESP32: {self.device_configs['server_ip']}")
                    else:
                        logger.debug(f"Response content: {response_json[:200]}...")
                    
            except Exception as send_error:
                logger.error(f"Failed to send USB response to {port}: {send_error}")
                return
            
            # Broadcast to web interface (handle event loop gracefully)
            try:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(broadcast_session_update(f"usb_{port.replace('/', '_')}", {
                        "timestamp": datetime.now().isoformat(),
                        "type": "provisioning_request",
                        "device_id": device_id,
                        "request_type": request_type,
                        "message": f"Provisioning request: {request_type}"
                    }))
                except RuntimeError:
                    # No event loop running, skip web broadcast
                    logger.debug("No event loop for web broadcast")
            except Exception as e:
                logger.debug(f"Web broadcast error: {e}")
            
        except Exception as e:
            logger.error(f"Error handling USB provisioning request: {e}")
            
    def _handle_usb_provisioning_status(self, port: str, json_data: str):
        """Handle provisioning status update received via USB JTAG"""
        try:
            import json
            
            status = json.loads(json_data)
            device_id = status.get('device_id', 'unknown')
            phase = status.get('phase')
            status_value = status.get('status')
            message = status.get('message')
            
            logger.info(f"📱 USB JTAG status from {device_id} on {port}: {phase} - {status_value}")
            
            # Broadcast to web interface (handle event loop gracefully)
            try:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(broadcast_session_update(f"usb_{port.replace('/', '_')}", {
                        "timestamp": datetime.now().isoformat(),
                        "type": "provisioning_status",
                        "device_id": device_id,
                        "phase": phase,
                        "status": status_value,
                        "message": message
                    }))
                except RuntimeError:
                    # No event loop running, skip web broadcast
                    logger.debug("No event loop for web broadcast")
            except Exception as e:
                logger.debug(f"Web broadcast error: {e}")
            
        except Exception as e:
            logger.error(f"Error handling USB provisioning status: {e}")
            
    def _prompt_for_wifi_credentials(self):
        """Prompt user for WiFi credentials via command line"""
        try:
            print("\n" + "=" * 50)
            print("📶 ESP32 WIFI CONFIGURATION NEEDED")
            print("=" * 50)
            print("Your ESP32 is requesting WiFi credentials.")
            print("Please enter your WiFi network details:")
            print()
            
            # Prompt for SSID
            while True:
                ssid = input("📶 WiFi Network Name (SSID): ").strip()
                
                if ssid:
                    break
                print("Please enter a valid WiFi network name.")
            
            # Prompt for password (hidden input)
            while True:
                password = getpass.getpass("🔐 WiFi Password: ")
                if password:
                    # Confirm password
                    confirm = getpass.getpass("🔐 Confirm WiFi Password: ")
                    if password == confirm:
                        break
                    else:
                        print("❌ Passwords don't match. Please try again.")
                else:
                    # Allow empty password for open networks
                    confirm = input("Empty password (open network)? (y/N): ").lower()
                    if confirm in ['y', 'yes']:
                        break
            
            # Update configuration
            self.device_configs['wifi_ssid'] = ssid
            self.device_configs['wifi_password'] = password
            self.wifi_credentials_set = True
            
            print(f"\n✅ WiFi credentials set:")
            print(f"   Network: {ssid}")
            print(f"   Password: {'*' * len(password)}")
            print(f"   Server: {self.device_configs['server_ip']}:{self.device_configs['server_port']}")
            print("=" * 50 + "\n")
            
        except KeyboardInterrupt:
            print("\n\n❌ WiFi configuration cancelled by user")
            print("ESP32 provisioning cannot continue without WiFi credentials.")
            raise
        except Exception as e:
            logger.error(f"Error prompting for WiFi credentials: {e}")
            raise
            
    def _scan_usb_devices(self):
        """Scan for ESP32 USB JTAG devices that need provisioning"""
        try:
            import serial.tools.list_ports
            
            # Look for ESP32 devices (common VID/PID patterns)
            esp32_patterns = [
                (0x303A, 0x1001),  # ESP32-S3 USB JTAG
                (0x303A, 0x4001),  # ESP32-S2 USB JTAG  
                (0x10C4, 0xEA60),  # CP210x USB to UART
                (0x1A86, 0x7523),  # CH340 USB to UART
            ]
            
            current_devices = []
            for port in serial.tools.list_ports.comports():
                if port.vid and port.pid:
                    for vid, pid in esp32_patterns:
                        if port.vid == vid and port.pid == pid:
                            current_devices.append({
                                'port': port.device,
                                'description': port.description,
                                'vid': port.vid,
                                'pid': port.pid
                            })
                            
                            # Check if this is a new device
                            if port.device not in [d.get('port') for d in self.usb_devices]:
                                logger.info(f"🔌 New ESP32 device detected: {port.device} - {port.description}")
                                # Schedule the coroutine to run in the event loop
                                try:
                                    loop = asyncio.get_event_loop()
                                    loop.create_task(self._handle_new_esp32_device(port.device))
                                except RuntimeError:
                                    # No event loop running, skip async handling
                                    logger.debug(f"No event loop for async ESP32 device handling: {port.device}")
            
            # Only update device list after processing to avoid repeated detection
            old_device_ports = [d.get('port') for d in self.usb_devices]
            self.usb_devices = current_devices
            
            # Log summary of changes
            new_device_ports = [d.get('port') for d in current_devices]
            added_devices = [port for port in new_device_ports if port not in old_device_ports]
            removed_devices = [port for port in old_device_ports if port not in new_device_ports]
            
            if added_devices:
                logger.debug(f"Added ESP32 devices: {added_devices}")
                if debug_mode:
                    print(f"🔌 [USB] Added ESP32 devices: {added_devices}")
            if removed_devices:
                logger.debug(f"Removed ESP32 devices: {removed_devices}")
                if debug_mode:
                    print(f"🔌 [USB] Removed ESP32 devices: {removed_devices}")
            
        except ImportError:
            # pyserial not available, skip USB monitoring
            logger.warning("⚠️  pyserial not available - USB JTAG communication disabled")
            logger.warning("💻 Install with: pip install pyserial")
            pass
        except Exception as e:
            if debug_mode:
                print(f"❌ [USB ERROR] {e}")
            logger.debug(f"USB scan error: {e}")
            
    async def _handle_new_esp32_device(self, port: str):
        """Handle newly connected ESP32 device"""
        logger.info(f"📨 Handling new ESP32 device on {port}")
        
        # Broadcast to web interface that ESP32 was detected
        await broadcast_session_update(f"esp32_{port.replace('/', '_')}", {
            "timestamp": datetime.now().isoformat(),
            "type": "esp32_detected", 
            "port": port,
            "message": f"ESP32 device detected on {port}"
        })
        
    def send_config_via_usb(self, port: str, config_type: str):
        """Send configuration to ESP32 via USB JTAG"""
        try:
            import serial
            import json
            
            config_data = {}
            if config_type == "wifi_config":
                config_data = {
                    "type": "wifi_config",
                    "wifi_ssid": self.device_configs["wifi_ssid"],
                    "wifi_password": self.device_configs["wifi_password"]
                }
            elif config_type == "server_config":
                config_data = {
                    "type": "server_config", 
                    "server_ip": self.device_configs["server_ip"],
                    "server_port": self.device_configs["server_port"],
                    "server_url": f"http://{self.device_configs['server_ip']}:{self.device_configs['server_port']}/api/offer"
                }
            elif config_type == "full_config":
                config_data = {
                    "type": "full_config",
                    **self.device_configs,
                    "server_url": f"http://{self.device_configs['server_ip']}:{self.device_configs['server_port']}/api/offer"
                }
            
            # Send JSON config via serial
            with serial.Serial(port, 115200, timeout=1) as ser:
                config_json = json.dumps(config_data) + '\n'
                ser.write(config_json.encode('utf-8'))
                logger.info(f"📤 Sent {config_type} to ESP32 on {port}")
                
        except Exception as e:
            logger.error(f"Failed to send config via USB: {e}")
        
# Global USB provisioning manager
provisioning_manager = ESP32USBProvisioningManager()

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
@app.get("/api/health")
async def health_check():
    """Simple health check endpoint for ESP32 to verify server availability"""
    return {
        "status": "healthy",
        "service": "voice-keyboard-server",
        "timestamp": datetime.now().isoformat(),
        "active_sessions": len(active_sessions),
        "webrtc_connections": len(webrtc_connections)
    }

# Provisioning APIs removed - provisioning happens via USB JTAG communication

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
            .provisioning-status { border: 2px solid #ff9800; padding: 15px; border-radius: 5px; background: #fff8e1; }
            .provisioning-message { margin: 8px 0; padding: 8px; border-radius: 4px; }
            .provisioning-message.success { background: #e8f5e8; border-left: 4px solid #4caf50; }
            .provisioning-message.error { background: #ffebee; border-left: 4px solid #f44336; }
            .provisioning-message.info { background: #e3f2fd; border-left: 4px solid #2196f3; }
            .provisioning-message.warning { background: #fff3e0; border-left: 4px solid #ff9800; }
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
            
            <div class="provisioning-status" style="margin-top: 20px;">
                <h3>ESP32 Configuration Status</h3>
                <div id="provisioning-messages">
                    <p style="color: #666;">No ESP32 configuration activity yet...</p>
                </div>
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
                } else if (data.message && data.message.type === 'provisioning_status') {
                    addProvisioningMessage(data.message);
                } else if (data.message && data.message.type === 'esp32_debug') {
                    addProvisioningMessage({
                        phase: 'debug',
                        status: 'info', 
                        message: data.message.message,
                        timestamp: data.message.timestamp
                    });
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
            
            function addProvisioningMessage(message) {
                const provisioningMessages = document.getElementById('provisioning-messages');
                
                // Clear "no activity" message if it exists
                if (provisioningMessages.children.length === 1 && 
                    provisioningMessages.children[0].textContent.includes('No ESP32 configuration')) {
                    provisioningMessages.innerHTML = '';
                }
                
                const messageDiv = document.createElement('div');
                let statusClass = 'info';
                
                // Determine status class based on status and phase
                if (message.status === 'connected' || message.status === 'ready' || message.status === 'success') {
                    statusClass = 'success';
                } else if (message.status === 'failed' || message.status === 'error') {
                    statusClass = 'error';
                } else if (message.status === 'connecting' || message.status === 'checking') {
                    statusClass = 'warning';
                }
                
                messageDiv.className = `provisioning-message ${statusClass}`;
                
                const timestamp = message.timestamp ? new Date(message.timestamp).toLocaleTimeString() : new Date().toLocaleTimeString();
                const phaseText = message.phase ? `[${message.phase.toUpperCase()}]` : '[ESP32]';
                
                messageDiv.innerHTML = `
                    <div><strong>${phaseText}</strong> ${message.message}</div>
                    <div class="timestamp" style="font-size: 0.8em; color: #666;">${timestamp}</div>
                `;
                
                provisioningMessages.appendChild(messageDiv);
                
                // Keep only last 20 messages
                while (provisioningMessages.children.length > 20) {
                    provisioningMessages.removeChild(provisioningMessages.firstChild);
                }
                
                // Scroll to bottom
                provisioningMessages.scrollTop = provisioningMessages.scrollHeight;
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
    global debug_mode
    
    parser = argparse.ArgumentParser(description="Voice Keyboard Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind to")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode for USB communication")
    parser.add_argument("--reset", action="store_true", help="Reset ESP32 devices on server startup")
    
    args = parser.parse_args()
    debug_mode = args.debug
    
    # Set logger level to INFO to reduce debug noise
    import sys
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    
    # Set HOST_IP environment variable for ESP32 SDP munging and provisioning
    if args.host != "0.0.0.0" and args.host != "localhost":
        os.environ["HOST_IP"] = args.host
        provisioning_manager.device_configs["server_ip"] = args.host
        logger.info(f"Set HOST_IP environment variable to: {args.host}")
    
    # Update server port for provisioning
    os.environ["SERVER_PORT"] = str(args.port)
    provisioning_manager.device_configs["server_port"] = str(args.port)
    
    # Always auto-detect actual network IP for ESP32 (ESP32 cannot connect to 0.0.0.0, localhost, etc.)
    try:
        # Connect to a remote address to determine local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        provisioning_manager.device_configs["server_ip"] = local_ip
        provisioning_manager.device_configs["server_port"] = str(args.port)
        logger.info(f"Auto-detected server IP for ESP32: {local_ip}:{args.port}")
    except Exception:
        logger.warning("Could not auto-detect server IP, using fallback")
        provisioning_manager.device_configs["server_ip"] = "192.168.1.100"  # fallback IP
        provisioning_manager.device_configs["server_port"] = str(args.port)
    
    logger.info(f"Starting Voice Keyboard Server on {args.host}:{args.port}")
    logger.info(f"Web interface: http://{args.host}:{args.port}")
    logger.info(f"ESP32 HTTP endpoint: http://{args.host}:{args.port}/api/offer")
    logger.info(f"ESP32 Health check: http://{args.host}:{args.port}/api/health")
    
    if debug_mode:
        print("\n🐛 DEBUG MODE ENABLED - USB communication will be printed to console")
        print("Look for messages starting with [USB DEBUG], [PROVISIONING REQUEST], etc.\n")
    
    # WiFi detection removed - simplified provisioning approach
    logger.info("📶 WiFi network matching disabled - ESP32 will use provided credentials")
    
    # Start USB JTAG provisioning manager
    provisioning_manager.start_monitoring()
    logger.info("📡 ESP32 USB JTAG provisioning system enabled")
    
    # Check for ESP32 devices at startup and optionally reset them
    def check_esp32_devices():
        time.sleep(2)  # Give time for USB enumeration
        provisioning_manager._scan_usb_devices()
        
        if not provisioning_manager.usb_devices:
            logger.warning("⚠️  WARNING: No ESP32 devices detected!")
            logger.warning("📱 Please connect your ESP32 device via USB cable")
            logger.warning("🔌 Supported devices: ESP32-S3-BOX, ESP32-S3-BOX-3, ESP32 with USB-to-UART adapter")
        else:
            logger.info(f"✅ Found {len(provisioning_manager.usb_devices)} ESP32 device(s)")
            for device in provisioning_manager.usb_devices:
                logger.info(f"   📱 {device['port']} - {device['description']}")
            
            # Reset ESP32 devices if requested
            if args.reset:
                logger.info("🔄 Reset flag detected - resetting ESP32 devices...")
                provisioning_manager.reset_all_esp32_devices()
                time.sleep(3)  # Give ESP32 time to boot and reconnect
    
    # Run ESP32 check in background
    threading.Thread(target=check_esp32_devices, daemon=True).start()
    
    uvicorn.run(
        "voice_keyboard_server:app",
        host=args.host,
        port=args.port,
        reload=args.reload
    )


if __name__ == "__main__":
    main()