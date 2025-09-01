"""
ESP32 USB JTAG Provisioning Manager

Manages ESP32 device provisioning via USB JTAG communication.
"""

import asyncio
import getpass
import json
import os
import threading
import time
from datetime import datetime
from typing import Dict, List

from loguru import logger


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
                if hasattr(self, 'debug_mode') and self.debug_mode and loop_count % 15 == 0:
                    print(f"🔄 [USB MONITOR] Heartbeat #{loop_count}, devices: {len(self.usb_devices)}")
                
                # Scan for new USB devices
                self._scan_usb_devices()
                
                # Monitor active USB connections for provisioning messages
                self._monitor_usb_messages()
                    
            except Exception as e:
                logger.error(f"Error in USB provisioning monitor: {e}")
                if hasattr(self, 'debug_mode') and self.debug_mode:
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
                        if hasattr(self, 'debug_mode') and self.debug_mode:
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
                        if hasattr(self, 'debug_mode') and self.debug_mode:
                            print(f"🔌 [USB] Opened persistent connection to: {port}")
                    except (serial.SerialException, OSError) as e:
                        if hasattr(self, 'debug_mode') and self.debug_mode:
                            print(f"❌ [USB] Failed to open {port}: {e} - Will retry next cycle")
                        continue
                
                ser = self.serial_connections[port]
                try:
                    if ser.in_waiting > 0:
                        if hasattr(self, 'debug_mode') and self.debug_mode:
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
                                    if hasattr(self, 'debug_mode') and self.debug_mode:
                                        print(f"📡 [PROVISIONING REQUEST] {port}: {json_data}")
                                    self._handle_usb_provisioning_request(port, json_data)
                                
                                # Look for provisioning status updates
                                elif line.startswith('PROVISIONING_STATUS:'):
                                    json_data = line.replace('PROVISIONING_STATUS:', '').strip()
                                    if hasattr(self, 'debug_mode') and self.debug_mode:
                                        print(f"📱 [PROVISIONING STATUS] {port}: {json_data}")
                                    self._handle_usb_provisioning_status(port, json_data)
                                    
                                else:
                                    # Regular log messages - show all lines in debug mode
                                    if hasattr(self, 'debug_mode') and self.debug_mode and line.strip():
                                        print(f"💬 [USB DEBUG {port}] {line}")
                                        
                            except UnicodeDecodeError:
                                continue
                                
                except serial.SerialException as e:
                    # Connection lost - remove it and retry
                    if hasattr(self, 'debug_mode') and self.debug_mode:
                        print(f"❌ [SERIAL ERROR] {port}: {e} - Will retry connection")
                    try:
                        ser.close()
                    except Exception:
                        pass
                    del self.serial_connections[port]
                    continue
                except OSError as e:
                    # Device not configured or similar OS-level error
                    if hasattr(self, 'debug_mode') and self.debug_mode:
                        print(f"❌ [USB DEVICE ERROR] {port}: {e} - Removing connection")
                    try:
                        ser.close()
                    except Exception:
                        pass
                    del self.serial_connections[port]
                    continue
                except Exception as e:
                    if hasattr(self, 'debug_mode') and self.debug_mode:
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
                    if hasattr(self, 'debug_mode') and self.debug_mode:
                        print(f"📤 [PROVISIONING RESPONSE] {port}: {response_json}")
                        print(f"   Server IP being sent to ESP32: {self.device_configs['server_ip']}")
                    else:
                        logger.debug(f"Response content: {response_json[:200]}...")
                    
            except Exception as send_error:
                logger.error(f"Failed to send USB response to {port}: {send_error}")
                return
            
            # Broadcast to web interface (handle event loop gracefully)
            if hasattr(self, 'broadcast_callback'):
                try:
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(self.broadcast_callback(f"usb_{port.replace('/', '_')}", {
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
            if hasattr(self, 'broadcast_callback'):
                try:
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(self.broadcast_callback(f"usb_{port.replace('/', '_')}", {
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
                                if hasattr(self, 'new_device_callback'):
                                    try:
                                        loop = asyncio.get_event_loop()
                                        loop.create_task(self.new_device_callback(port.device))
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
                if hasattr(self, 'debug_mode') and self.debug_mode:
                    print(f"🔌 [USB] Added ESP32 devices: {added_devices}")
            if removed_devices:
                logger.debug(f"Removed ESP32 devices: {removed_devices}")
                if hasattr(self, 'debug_mode') and self.debug_mode:
                    print(f"🔌 [USB] Removed ESP32 devices: {removed_devices}")
            
        except ImportError:
            # pyserial not available, skip USB monitoring
            logger.warning("⚠️  pyserial not available - USB JTAG communication disabled")
            logger.warning("💻 Install with: pip install pyserial")
            pass
        except Exception as e:
            if hasattr(self, 'debug_mode') and self.debug_mode:
                print(f"❌ [USB ERROR] {e}")
            logger.debug(f"USB scan error: {e}")
            
    async def _handle_new_esp32_device(self, port: str):
        """Handle newly connected ESP32 device"""
        logger.info(f"📨 Handling new ESP32 device on {port}")
        
        # Broadcast to web interface that ESP32 was detected
        if hasattr(self, 'broadcast_callback'):
            await self.broadcast_callback(f"esp32_{port.replace('/', '_')}", {
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

    def set_broadcast_callback(self, callback):
        """Set callback for broadcasting messages to web interface"""
        self.broadcast_callback = callback
        
    def set_new_device_callback(self, callback):
        """Set callback for handling new ESP32 devices"""
        self.new_device_callback = callback
        
    def set_debug_mode(self, debug_mode):
        """Set debug mode for USB communication"""
        self.debug_mode = debug_mode