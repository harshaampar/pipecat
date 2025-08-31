#!/usr/bin/env python3
"""
Simple test script to verify USB JTAG communication with ESP32
"""

import serial
import serial.tools.list_ports
import time
import sys

def find_esp32_devices():
    """Find ESP32 USB JTAG devices"""
    esp32_patterns = [
        (0x303A, 0x1001),  # ESP32-S3 USB JTAG
        (0x303A, 0x4001),  # ESP32-S2 USB JTAG  
        (0x10C4, 0xEA60),  # CP210x USB to UART
        (0x1A86, 0x7523),  # CH340 USB to UART
    ]
    
    devices = []
    for port in serial.tools.list_ports.comports():
        if port.vid and port.pid:
            for vid, pid in esp32_patterns:
                if port.vid == vid and port.pid == pid:
                    devices.append({
                        'port': port.device,
                        'description': port.description,
                        'vid': port.vid,
                        'pid': port.pid
                    })
    return devices

def test_usb_connection(port_name):
    """Test USB connection to ESP32"""
    print(f"Testing USB connection to {port_name}")
    print("Press Ctrl+C to stop")
    print("-" * 50)
    
    try:
        with serial.Serial(port_name, 115200, timeout=1) as ser:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
            
            print(f"Connected to {port_name} at 115200 baud")
            print("Listening for data... (reset your ESP32 now)")
            print()
            
            line_count = 0
            while True:
                if ser.in_waiting > 0:
                    try:
                        line = ser.readline().decode('utf-8', errors='ignore').strip()
                        if line:
                            line_count += 1
                            print(f"[{line_count:04d}] {line}")
                    except Exception as e:
                        print(f"Error reading line: {e}")
                
                time.sleep(0.1)
                
    except serial.SerialException as e:
        print(f"Serial connection error: {e}")
        return False
    except KeyboardInterrupt:
        print("\nTest stopped by user")
        return True

def main():
    print("ESP32 USB JTAG Connection Test")
    print("=" * 40)
    
    # Find ESP32 devices
    devices = find_esp32_devices()
    
    if not devices:
        print("❌ No ESP32 devices found")
        print("Make sure your ESP32 is connected via USB")
        return 1
    
    print(f"✅ Found {len(devices)} ESP32 device(s):")
    for i, device in enumerate(devices):
        print(f"  {i+1}. {device['port']} - {device['description']}")
    
    # Test the first device
    if len(devices) == 1:
        test_usb_connection(devices[0]['port'])
    else:
        try:
            choice = int(input("\nEnter device number to test: ")) - 1
            if 0 <= choice < len(devices):
                test_usb_connection(devices[choice]['port'])
            else:
                print("Invalid choice")
                return 1
        except (ValueError, KeyboardInterrupt):
            print("Invalid input or cancelled")
            return 1
    
    return 0

if __name__ == "__main__":
    sys.exit(main())