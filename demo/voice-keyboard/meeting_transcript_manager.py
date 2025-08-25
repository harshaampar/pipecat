import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import aiofiles
from loguru import logger


class MeetingTranscriptManager:
    def __init__(self, storage_dir: str = "storage"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(exist_ok=True)
        
        self.db_path = self.storage_dir / "meetings.db"
        self.transcripts_dir = self.storage_dir / "transcripts"
        self.transcripts_dir.mkdir(exist_ok=True)
        
        self._init_database()
        
    def _init_database(self):
        """Initialize SQLite database for meeting metadata"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS meetings (
                    meeting_id TEXT PRIMARY KEY,
                    start_time TIMESTAMP,
                    end_time TIMESTAMP,
                    participant_count INTEGER DEFAULT 1,
                    transcript_count INTEGER DEFAULT 0,
                    title TEXT,
                    summary TEXT,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS transcripts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    meeting_id TEXT,
                    timestamp TIMESTAMP,
                    speaker TEXT,
                    text TEXT,
                    confidence REAL,
                    message_type TEXT DEFAULT 'transcription',
                    is_interim BOOLEAN DEFAULT FALSE,
                    FOREIGN KEY (meeting_id) REFERENCES meetings (meeting_id)
                )
            """)
            conn.commit()

    async def start_meeting(self, meeting_id: Optional[str] = None) -> str:
        """Start a new meeting session"""
        if not meeting_id:
            meeting_id = f"meeting_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid.uuid4())[:8]}"
            
        start_time = datetime.now()
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO meetings 
                (meeting_id, start_time, title, status) 
                VALUES (?, ?, ?, ?)
            """, (meeting_id, start_time, f"Meeting {start_time.strftime('%Y-%m-%d %H:%M')}", "active"))
            conn.commit()
            
        logger.info(f"Started meeting session: {meeting_id}")
        return meeting_id

    async def end_meeting(self, meeting_id: str):
        """End a meeting session"""
        end_time = datetime.now()
        
        with sqlite3.connect(self.db_path) as conn:
            # Update end time and transcript count
            conn.execute("""
                UPDATE meetings 
                SET end_time = ?, 
                    status = 'ended',
                    transcript_count = (
                        SELECT COUNT(*) FROM transcripts WHERE meeting_id = ? AND is_interim = FALSE
                    )
                WHERE meeting_id = ?
            """, (end_time, meeting_id, meeting_id))
            conn.commit()
            
        logger.info(f"Ended meeting session: {meeting_id}")

    async def log_message(
        self, 
        meeting_id: str, 
        text: str, 
        speaker: str = "participant", 
        confidence: float = 1.0,
        message_type: str = "transcription",
        is_interim: bool = False
    ):
        """Log a transcript message to the meeting"""
        timestamp = datetime.now()
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO transcripts 
                (meeting_id, timestamp, speaker, text, confidence, message_type, is_interim)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (meeting_id, timestamp, speaker, text, confidence, message_type, is_interim))
            conn.commit()
            
        # Also append to JSON file for easy access (only final transcripts, not interim)
        if not is_interim:
            await self._append_to_json_file(meeting_id, {
                "timestamp": timestamp.isoformat(),
                "speaker": speaker,
                "text": text,
                "confidence": confidence,
                "type": message_type
            })
            
            # Update transcript count in real-time for active meetings
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    UPDATE meetings 
                    SET transcript_count = (
                        SELECT COUNT(*) FROM transcripts WHERE meeting_id = ? AND is_interim = FALSE
                    )
                    WHERE meeting_id = ?
                """, (meeting_id, meeting_id))
                conn.commit()

    async def update_transcript(self, meeting_id: str, transcript_id: int, corrected_text: str):
        """Update a transcript with corrected text"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE transcripts 
                SET text = ? 
                WHERE id = ? AND meeting_id = ?
            """, (corrected_text, transcript_id, meeting_id))
            conn.commit()

    async def _append_to_json_file(self, meeting_id: str, transcript_data: Dict):
        """Append transcript to JSON file"""
        file_path = self.transcripts_dir / f"{meeting_id}.json"
        
        # Read existing data
        transcripts = []
        metadata = {}
        if file_path.exists():
            async with aiofiles.open(file_path, 'r') as f:
                content = await f.read()
                if content.strip():
                    try:
                        data = json.loads(content)
                        transcripts = data.get('transcripts', [])
                        metadata = data.get('metadata', {})
                    except json.JSONDecodeError:
                        logger.warning(f"Invalid JSON in {file_path}, starting fresh")
        
        # Append new transcript
        transcripts.append(transcript_data)
        
        # Update metadata
        metadata.update({
            "meeting_id": meeting_id,
            "last_updated": datetime.now().isoformat(),
            "total_transcripts": len(transcripts)
        })
        
        # Write back
        meeting_data = {
            "metadata": metadata,
            "transcripts": transcripts
        }
        
        async with aiofiles.open(file_path, 'w') as f:
            await f.write(json.dumps(meeting_data, indent=2))

    async def get_meetings(self, limit: int = 1000) -> List[Dict]:
        """Get list of meetings"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT meeting_id, start_time, end_time, title, summary, transcript_count, status
                FROM meetings 
                ORDER BY start_time DESC 
                LIMIT ?
            """, (limit,))
            
            return [dict(row) for row in cursor.fetchall()]

    async def get_meeting(self, meeting_id: str) -> Optional[Dict]:
        """Get a specific meeting with all transcripts"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            
            # Get meeting metadata
            cursor = conn.execute("""
                SELECT * FROM meetings WHERE meeting_id = ?
            """, (meeting_id,))
            
            meeting = cursor.fetchone()
            if not meeting:
                return None
                
            # Get transcripts (only final ones, not interim)
            cursor = conn.execute("""
                SELECT * FROM transcripts 
                WHERE meeting_id = ? AND is_interim = FALSE
                ORDER BY timestamp ASC
            """, (meeting_id,))
            
            transcripts = [dict(row) for row in cursor.fetchall()]
            
            result = dict(meeting)
            result['transcripts'] = transcripts
            return result

    async def search_meetings(self, query: str, limit: int = 20) -> List[Dict]:
        """Search meetings by transcript content"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT DISTINCT m.meeting_id, m.start_time, m.title, m.summary, m.status,
                       COUNT(t.id) as matching_transcripts
                FROM meetings m
                JOIN transcripts t ON m.meeting_id = t.meeting_id
                WHERE t.text LIKE ? OR m.title LIKE ? OR m.summary LIKE ?
                GROUP BY m.meeting_id
                ORDER BY m.start_time DESC
                LIMIT ?
            """, (f"%{query}%", f"%{query}%", f"%{query}%", limit))
            
            return [dict(row) for row in cursor.fetchall()]

    async def get_meeting_summary(self, meeting_id: str) -> Optional[str]:
        """Get meeting summary (placeholder for future AI summarization)"""
        meeting = await self.get_meeting(meeting_id)
        if not meeting:
            return None
            
        transcripts = meeting.get('transcripts', [])
        if not transcripts:
            return "No transcripts available"
            
        # Simple summary - in future this could use AI summarization
        total_transcripts = len(transcripts)
        start_time = meeting.get('start_time', 'Unknown')
        duration = "Unknown"
        
        if meeting.get('end_time'):
            try:
                start = datetime.fromisoformat(meeting['start_time'])
                end = datetime.fromisoformat(meeting['end_time'])
                duration_seconds = (end - start).total_seconds()
                duration = f"{int(duration_seconds // 60)} minutes"
            except:
                pass
                
        return f"Meeting with {total_transcripts} transcripts, started at {start_time}, duration: {duration}"

    async def export_meeting_transcript(self, meeting_id: str, format: str = "txt") -> Optional[str]:
        """Export meeting transcript in various formats"""
        meeting = await self.get_meeting(meeting_id)
        if not meeting:
            return None
            
        transcripts = meeting.get('transcripts', [])
        if not transcripts:
            return "No transcripts available for this meeting."
            
        if format.lower() == "txt":
            lines = []
            lines.append(f"Meeting: {meeting.get('title', 'Untitled')}")
            lines.append(f"Started: {meeting.get('start_time', 'Unknown')}")
            if meeting.get('end_time'):
                lines.append(f"Ended: {meeting.get('end_time')}")
            lines.append("-" * 50)
            lines.append("")
            
            for transcript in transcripts:
                timestamp = transcript.get('timestamp', '')
                speaker = transcript.get('speaker', 'Unknown')
                text = transcript.get('text', '')
                lines.append(f"[{timestamp}] {speaker}: {text}")
            
            return "\n".join(lines)
            
        elif format.lower() == "json":
            return json.dumps(meeting, indent=2)
            
        return None

    async def get_active_meetings(self) -> List[Dict]:
        """Get currently active meetings"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT meeting_id, start_time, title, transcript_count
                FROM meetings 
                WHERE status = 'active'
                ORDER BY start_time DESC
            """)
            
            return [dict(row) for row in cursor.fetchall()]