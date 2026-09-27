"""
Drone IDS - Message Bus
Thread-safe message passing between components.
"""
import queue
import threading
from typing import Any, Dict, List, Callable, Optional
from dataclasses import dataclass, field
from enum import Enum
import time


class MessageType(Enum):
    MAVLINK_MESSAGE = "mavlink_message"
    ALERT = "alert"
    TELEMETRY = "telemetry"
    COMMAND = "command"
    STATUS = "status"
    RAW_DATA = "raw_data"


@dataclass
class Message:
    type: MessageType
    source: str
    timestamp: float = field(default_factory=time.time)
    data: Dict[str, Any] = field(default_factory=dict)
    raw: bytes = b''


class MessageBus:
    """Thread-safe pub/sub message bus for inter-component communication."""
    
    def __init__(self, maxsize: int = 10000):
        self._queues: Dict[str, queue.Queue] = {}
        self._subscribers: Dict[MessageType, List[Callable]] = {}
        self._lock = threading.RLock()
        self._maxsize = maxsize
        self._running = False
    
    def subscribe(self, msg_type: MessageType, callback: Callable[[Message], None]) -> None:
        """Subscribe to a message type."""
        with self._lock:
            if msg_type not in self._subscribers:
                self._subscribers[msg_type] = []
            self._subscribers[msg_type].append(callback)
    
    def unsubscribe(self, msg_type: MessageType, callback: Callable) -> None:
        """Unsubscribe from a message type."""
        with self._lock:
            if msg_type in self._subscribers:
                try:
                    self._subscribers[msg_type].remove(callback)
                except ValueError:
                    pass
    
    def publish(self, message: Message) -> None:
        """Publish a message to all subscribers."""
        with self._lock:
            subscribers = self._subscribers.get(message.type, []).copy()
        
        for callback in subscribers:
            try:
                callback(message)
            except Exception as e:
                print(f"Error in subscriber for {message.type}: {e}")
    
    def publish_async(self, message: Message) -> None:
        """Publish message asynchronously (non-blocking)."""
        threading.Thread(target=self.publish, args=(message,), daemon=True).start()


# Global message bus instance
message_bus = MessageBus()