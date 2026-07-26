import json
import time
from pathlib import Path

from config import INBOX_DIR, VALID_MSG_TYPES


class MessageBus:
    def __init__(self,inbox_dir:Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str
             ,msg_type: str = "message",extra: dict = None)-> str:
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid message type '{msg_type}'"
        msg = {
            "sender": sender, 
            "timestamp": time.time(),
            "content": content, 
            "type": msg_type,
        }
        if extra:
            msg.update(extra)
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return f"Sent {msg_type} to {to}"
    
    def read_inbox(self, name: str) -> list:
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        messages = []
        for line in inbox_path.read_text().strip().splitlines():
            if line:
                messages.append(json.loads(line))
        inbox_path.write_text("")
        return messages

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"

BUS = MessageBus(INBOX_DIR)
