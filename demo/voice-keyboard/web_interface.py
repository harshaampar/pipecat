"""
Web Interface Handlers for Voice Keyboard Server

Provides HTML interfaces for monitoring voice keyboard sessions and ESP32 debug messages.
"""

from fastapi.responses import HTMLResponse


def get_debug_page_html():
    """Get HTML content for ESP32 debug messages page"""
    return """
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
    """


def get_main_interface_html():
    """Get HTML content for the main web interface"""
    return """
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


async def get_debug_page():
    """Get debug page response"""
    return HTMLResponse(get_debug_page_html())


async def get_main_page():
    """Get main interface response"""
    return HTMLResponse(content=get_main_interface_html())