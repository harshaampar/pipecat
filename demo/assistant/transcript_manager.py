import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import aiofiles
from loguru import logger


class TranscriptManager:
    def __init__(self, storage_dir: str = "storage"):
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(exist_ok=True)
        
        self.db_path = self.storage_dir / "conversations.db"
        self.transcripts_dir = self.storage_dir / "transcripts"
        self.transcripts_dir.mkdir(exist_ok=True)
        
        self._init_database()
        
    def _init_database(self):
        """Initialize SQLite database for conversation metadata"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    session_id TEXT PRIMARY KEY,
                    start_time TIMESTAMP,
                    end_time TIMESTAMP,
                    participant_count INTEGER DEFAULT 1,
                    message_count INTEGER DEFAULT 0,
                    title TEXT,
                    summary TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    timestamp TIMESTAMP,
                    speaker TEXT,
                    original_text TEXT,
                    corrected_text TEXT,
                    confidence REAL,
                    message_type TEXT DEFAULT 'user',
                    FOREIGN KEY (session_id) REFERENCES conversations (session_id)
                )
            """)
            conn.commit()

    async def start_conversation(self, session_id: Optional[str] = None) -> str:
        """Start a new conversation session"""
        if not session_id:
            session_id = str(uuid.uuid4())
            
        start_time = datetime.now()
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO conversations 
                (session_id, start_time, title) 
                VALUES (?, ?, ?)
            """, (session_id, start_time, f"Conversation {start_time.strftime('%Y-%m-%d %H:%M')}"))
            conn.commit()
            
        logger.info(f"Started conversation session: {session_id}")
        return session_id

    async def end_conversation(self, session_id: str):
        """End a conversation session"""
        end_time = datetime.now()
        
        with sqlite3.connect(self.db_path) as conn:
            # Update end time and message count
            conn.execute("""
                UPDATE conversations 
                SET end_time = ?, 
                    message_count = (
                        SELECT COUNT(*) FROM messages WHERE session_id = ?
                    )
                WHERE session_id = ?
            """, (end_time, session_id, session_id))
            conn.commit()
            
        logger.info(f"Ended conversation session: {session_id}")

    async def log_message(
        self, 
        session_id: str, 
        text: str, 
        speaker: str = "user", 
        confidence: float = 1.0,
        message_type: str = "user"
    ):
        """Log a message to the conversation"""
        timestamp = datetime.now()
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO messages 
                (session_id, timestamp, speaker, original_text, confidence, message_type)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (session_id, timestamp, speaker, text, confidence, message_type))
            conn.commit()
            
        # Also append to JSON file for easy access
        await self._append_to_json_file(session_id, {
            "timestamp": timestamp.isoformat(),
            "speaker": speaker,
            "text": text,
            "confidence": confidence,
            "type": message_type
        })

    async def correct_transcript(self, session_id: str, message_id: int, corrected_text: str):
        """Update a message with corrected transcript"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                UPDATE messages 
                SET corrected_text = ? 
                WHERE id = ? AND session_id = ?
            """, (corrected_text, message_id, session_id))
            conn.commit()

    async def _append_to_json_file(self, session_id: str, message_data: Dict):
        """Append message to JSON transcript file"""
        file_path = self.transcripts_dir / f"{session_id}.json"
        
        # Read existing data
        messages = []
        if file_path.exists():
            async with aiofiles.open(file_path, 'r') as f:
                content = await f.read()
                if content.strip():
                    try:
                        data = json.loads(content)
                        messages = data.get('messages', [])
                    except json.JSONDecodeError:
                        logger.warning(f"Invalid JSON in {file_path}, starting fresh")
        
        # Append new message
        messages.append(message_data)
        
        # Write back
        transcript_data = {
            "session_id": session_id,
            "created_at": datetime.now().isoformat(),
            "messages": messages
        }
        
        async with aiofiles.open(file_path, 'w') as f:
            await f.write(json.dumps(transcript_data, indent=2))

    async def get_conversations(self, limit: int = 50) -> List[Dict]:
        """Get list of conversations"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT session_id, start_time, end_time, title, summary, message_count
                FROM conversations 
                ORDER BY start_time DESC 
                LIMIT ?
            """, (limit,))
            
            return [dict(row) for row in cursor.fetchall()]

    async def get_conversation(self, session_id: str) -> Optional[Dict]:
        """Get a specific conversation with all messages"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            
            # Get conversation metadata
            cursor = conn.execute("""
                SELECT * FROM conversations WHERE session_id = ?
            """, (session_id,))
            
            conversation = cursor.fetchone()
            if not conversation:
                return None
                
            # Get messages
            cursor = conn.execute("""
                SELECT * FROM messages 
                WHERE session_id = ? 
                ORDER BY timestamp ASC
            """, (session_id,))
            
            messages = [dict(row) for row in cursor.fetchall()]
            
            result = dict(conversation)
            result['messages'] = messages
            return result

    async def search_conversations(self, query: str, limit: int = 20) -> List[Dict]:
        """Search conversations by text content"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT DISTINCT c.session_id, c.start_time, c.title, c.summary,
                       COUNT(m.id) as matching_messages
                FROM conversations c
                JOIN messages m ON c.session_id = m.session_id
                WHERE m.original_text LIKE ? OR m.corrected_text LIKE ?
                   OR c.title LIKE ? OR c.summary LIKE ?
                GROUP BY c.session_id
                ORDER BY c.start_time DESC
                LIMIT ?
            """, (f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%", limit))
            
            return [dict(row) for row in cursor.fetchall()]

    async def get_conversation_context(self, session_id: str, last_n_messages: int = 10) -> List[Dict]:
        """Get recent conversation context for memory"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("""
                SELECT speaker, 
                       COALESCE(corrected_text, original_text) as text,
                       timestamp, message_type
                FROM messages 
                WHERE session_id = ? 
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (session_id, last_n_messages))
            
            messages = [dict(row) for row in cursor.fetchall()]
            return list(reversed(messages))  # Return in chronological order