from fastapi import WebSocket
from typing import Dict, List

class ConnectionManager:
    def __init__(self):
        # user_id -> List[WebSocket]
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, user_id: str, websocket: WebSocket):
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)
        print(f"[WS] Connection registered for user: {user_id}")

    def disconnect(self, user_id: str, websocket: WebSocket):
        if user_id in self.active_connections:
            if websocket in self.active_connections[user_id]:
                self.active_connections[user_id].remove(websocket)
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]
        print(f"[WS] Connection closed/removed for user: {user_id}")

    async def send_to_user(self, user_id: str, message: str) -> bool:
        """
        Sends a message to all active WebSocket connections for a user.
        Returns True if at least one message was successfully sent.
        """
        sent = False
        if user_id in self.active_connections and self.active_connections[user_id]:
            for ws in list(self.active_connections[user_id]):
                try:
                    await ws.send_text(message)
                    sent = True
                except Exception as e:
                    print(f"[WS ERROR] Failed to send message to user {user_id}: {e}")
                    # Try to clean up stale socket
                    try:
                        self.disconnect(user_id, ws)
                    except Exception:
                        pass
        return sent

manager = ConnectionManager()
