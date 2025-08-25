#!/usr/bin/env python3
"""
Test script for the meeting assistant server.
This script verifies the server can start and respond to basic requests.
"""

import asyncio
import os
import sys
from pathlib import Path

# Add the meeting assistant directory to Python path
sys.path.insert(0, str(Path(__file__).parent))

async def test_transcript_manager():
    """Test the meeting transcript manager functionality"""
    print("Testing Meeting Transcript Manager...")
    
    from meeting_transcript_manager import MeetingTranscriptManager
    
    # Initialize with test storage
    manager = MeetingTranscriptManager("test_storage")
    
    # Test starting a meeting
    meeting_id = await manager.start_meeting()
    print(f"✓ Started meeting: {meeting_id}")
    
    # Test logging transcripts
    await manager.log_message(meeting_id, "Hello, this is a test transcript.", "participant")
    await manager.log_message(meeting_id, "Testing the meeting assistant functionality.", "participant")
    print("✓ Logged test transcripts")
    
    # Test getting meeting
    meeting = await manager.get_meeting(meeting_id)
    print(f"✓ Retrieved meeting with {len(meeting.get('transcripts', []))} transcripts")
    
    # Test ending meeting
    await manager.end_meeting(meeting_id)
    print("✓ Ended meeting")
    
    # Test getting meetings list
    meetings = await manager.get_meetings()
    print(f"✓ Retrieved {len(meetings)} total meetings")
    
    print("Meeting Transcript Manager tests passed!\n")
    
    # Cleanup test storage
    import shutil
    if Path("test_storage").exists():
        shutil.rmtree("test_storage")
    

def test_imports():
    """Test that all required imports work"""
    print("Testing imports...")
    
    try:
        from meeting_server import app
        print("✓ Meeting server imports successful")
        
        from meeting_transcript_manager import MeetingTranscriptManager
        print("✓ Meeting transcript manager imports successful")
        
        # Test FastAPI app creation
        assert app is not None
        print("✓ FastAPI app created successfully")
        
    except ImportError as e:
        print(f"✗ Import error: {e}")
        return False
        
    except Exception as e:
        print(f"✗ Unexpected error: {e}")
        return False
    
    print("All imports successful!\n")
    return True


def check_environment():
    """Check required environment variables"""
    print("Checking environment variables...")
    
    required_vars = [
        "DEEPGRAM_API_KEY"
    ]
    
    missing_vars = []
    for var in required_vars:
        if not os.getenv(var):
            missing_vars.append(var)
    
    if missing_vars:
        print(f"⚠️  Missing environment variables: {', '.join(missing_vars)}")
        print("   Set these in your .env file or environment")
    else:
        print("✓ All required environment variables are set")
    
    print()


async def main():
    """Run all tests"""
    print("🎙️ Meeting Assistant Server Tests\n")
    print("=" * 50)
    
    # Check environment
    check_environment()
    
    # Test imports
    if not test_imports():
        print("❌ Import tests failed!")
        return 1
    
    # Test transcript manager
    try:
        await test_transcript_manager()
    except Exception as e:
        print(f"❌ Transcript manager tests failed: {e}")
        return 1
    
    print("=" * 50)
    print("🎉 All tests passed!")
    print("\nTo start the meeting assistant server:")
    print("  python meeting_server.py --host 0.0.0.0 --port 8765")
    print("\nThen configure your ESP32 with:")
    print("  export PIPECAT_SMALLWEBRTC_URL=\"http://YOUR_SERVER_IP:8765/api/offer\"")
    
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)