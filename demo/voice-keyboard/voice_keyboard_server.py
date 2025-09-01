import argparse
import asyncio
import os
import socket
import threading
import time
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from loguru import logger

from api_handlers import (
    handle_webrtc_offer,
    health_check,
    receive_debug_message, 
    handle_transcript_websocket,
    broadcast_session_update
)
from esp32_provisioning import ESP32USBProvisioningManager
from web_interface import get_debug_page, get_main_page

load_dotenv(override=True)

# Global instances
app = FastAPI()

# Debug mode flag
debug_mode = False


# Global USB provisioning manager
provisioning_manager = ESP32USBProvisioningManager()

# Mount static files
current_dir = Path(__file__).parent
app.mount("/static", StaticFiles(directory=current_dir / "static"), name="static")


# WebSocket for live transcript streaming
@app.websocket("/transcript-stream")
async def transcript_stream(websocket: WebSocket):
    """WebSocket endpoint for live transcript streaming to web interface"""
    return await handle_transcript_websocket(websocket)


# HTTP endpoint for ESP32 WebRTC negotiation
@app.post("/api/offer")
async def webrtc_offer(request: dict, background_tasks: BackgroundTasks):
    """Handle WebRTC offer requests from ESP32 clients."""
    return await handle_webrtc_offer(request, background_tasks, broadcast_session_update)


# REST API endpoints
@app.get("/api/health")
async def api_health_check():
    """Simple health check endpoint for ESP32 to verify server availability"""
    return await health_check()


@app.post("/debug")
async def api_receive_debug_message(request_data: dict):
    """Receive debug messages from ESP32 via HTTP POST"""
    return await receive_debug_message(request_data, broadcast_session_update)


@app.get("/debug")
async def api_get_debug_page():
    """Debug page to view ESP32 debug messages"""
    return await get_debug_page()


@app.get("/")
async def get_index():
    """Serve the main web interface"""
    return await get_main_page()


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
    
    # Set up provisioning manager callbacks
    provisioning_manager.set_broadcast_callback(broadcast_session_update)
    provisioning_manager.set_debug_mode(debug_mode)
    
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