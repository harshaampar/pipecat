from typing import Dict, List, Optional
from datetime import datetime, timedelta
from loguru import logger

from transcript_manager import TranscriptManager


class ConversationMemory:
    def __init__(self, transcript_manager: TranscriptManager):
        self.transcript_manager = transcript_manager
        self.current_session_id: Optional[str] = None
        self.context_window = 20  # Number of recent messages to keep in context
        
    async def set_current_session(self, session_id: str):
        """Set the current active conversation session"""
        self.current_session_id = session_id
        logger.info(f"Set current session to: {session_id}")
        
    async def get_current_context(self) -> List[Dict]:
        """Get current conversation context for LLM"""
        if not self.current_session_id:
            return []
            
        context = await self.transcript_manager.get_conversation_context(
            self.current_session_id, 
            self.context_window
        )
        
        return context
        
    async def search_past_conversations(self, query: str, days_back: int = 7) -> List[str]:
        """Search through past conversations for relevant context"""
        try:
            results = await self.transcript_manager.search_conversations(query, limit=10)
            
            # Filter by date range
            cutoff_date = datetime.now() - timedelta(days=days_back)
            recent_results = []
            
            for result in results:
                start_time = datetime.fromisoformat(result['start_time'])
                if start_time >= cutoff_date:
                    # Get the actual conversation content
                    conversation = await self.transcript_manager.get_conversation(result['session_id'])
                    if conversation:
                        # Extract relevant snippets
                        relevant_snippets = self._extract_relevant_snippets(
                            conversation['messages'], query
                        )
                        recent_results.extend(relevant_snippets)
                        
            return recent_results[:5]  # Return top 5 relevant snippets
            
        except Exception as e:
            logger.error(f"Error searching past conversations: {e}")
            return []
    
    def _extract_relevant_snippets(self, messages: List[Dict], query: str) -> List[str]:
        """Extract relevant message snippets containing the query"""
        snippets = []
        query_lower = query.lower()
        
        for i, message in enumerate(messages):
            text = message.get('corrected_text') or message.get('original_text', '')
            if query_lower in text.lower():
                # Include context: previous and next message if available
                context_messages = []
                
                # Previous message
                if i > 0:
                    prev_text = messages[i-1].get('corrected_text') or messages[i-1].get('original_text', '')
                    context_messages.append(f"Previous: {prev_text}")
                
                # Current message
                context_messages.append(f"Match: {text}")
                
                # Next message
                if i < len(messages) - 1:
                    next_text = messages[i+1].get('corrected_text') or messages[i+1].get('original_text', '')
                    context_messages.append(f"Next: {next_text}")
                
                snippet = " | ".join(context_messages)
                snippets.append(snippet)
                
        return snippets
    
    async def get_memory_enhanced_context(self, current_input: str) -> Dict:
        """Get enhanced context including current conversation and relevant past context"""
        # Get current conversation context
        current_context = await self.get_current_context()
        
        # Search for relevant past conversations
        past_context = await self.search_past_conversations(current_input)
        
        memory_context = {
            "current_conversation": current_context,
            "relevant_past_context": past_context,
            "has_past_context": len(past_context) > 0
        }
        
        return memory_context
    
    def format_context_for_llm(self, memory_context: Dict) -> str:
        """Format memory context for inclusion in LLM prompt"""
        context_parts = []
        
        # Add current conversation context
        if memory_context["current_conversation"]:
            context_parts.append("Recent conversation:")
            for msg in memory_context["current_conversation"][-5:]:  # Last 5 messages
                speaker = msg["speaker"]
                text = msg.get("corrected_text") or msg.get("text", "")
                context_parts.append(f"{speaker}: {text}")
        
        # Add relevant past context if available
        if memory_context["has_past_context"]:
            context_parts.append("\nRelevant past discussions:")
            for snippet in memory_context["relevant_past_context"]:
                context_parts.append(f"- {snippet}")
        
        return "\n".join(context_parts) if context_parts else ""