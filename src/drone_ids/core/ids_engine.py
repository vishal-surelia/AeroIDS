"""
Drone IDS - Core Engine (Part 1)
"""
import time
import threading
import logging
from collections import deque
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum

from .config import config
from .message_bus import message_bus, Message, MessageType


class EngineState(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass
class DetectionStats:
    messages_processed: int = 0
    alerts_generated: int = 0
    alerts_by_type: Dict[str, int] = field(default_factory=dict)
    start_time: float = field(default_factory=time.time)
    last_message_time: float = 0
    errors: int = 0


class IDSEngine:
    """Main IDS Engine orchestrating all detectors."""
    
    def __init__(self):
        self.state = EngineState.STOPPED
        self.detectors: List[Any] = []
        self.stats = DetectionStats()
        self._lock = threading.RLock()
        self._main_thread: Optional[threading.Thread] = None
        self._running = False
        self._message_count = 0
        self._message_rate_window: deque = deque(maxlen=5000)
        self._last_stats_log = time.time()
        
        self.logger = logging.getLogger('drone_ids.engine')
        self.logger.setLevel(logging.INFO)
        
        message_bus.subscribe(MessageType.MAVLINK_MESSAGE, self._on_mavlink_message)
        message_bus.subscribe(MessageType.COMMAND, self._on_command)
        message_bus.subscribe(MessageType.TELEMETRY, self._on_telemetry)
    
    def add_detector(self, detector: Any) -> None:
        with self._lock:
            self.detectors.append(detector)
            detector.set_engine(self)
            self.logger.info(f"Added detector: {detector.__class__.__name__}")
    
    def remove_detector(self, detector: Any) -> None:
        with self._lock:
            if detector in self.detectors:
                self.detectors.remove(detector)
                self.logger.info(f"Removed detector: {detector.__class__.__name__}")
    
    def start(self) -> None:
        if self.state != EngineState.STOPPED:
            self.logger.warning(f"Engine already running (state: {self.state})")
            return
        
        self.state = EngineState.STARTING
        self._running = True
        
        for detector in self.detectors:
            try:
                detector.initialize()
                self.logger.info(f"Initialized detector: {detector.__class__.__name__}")
            except Exception as e:
                self.logger.error(f"Failed to initialize {detector.__class__.__name__}: {e}")
                self.state = EngineState.ERROR
                return
        
        self.state = EngineState.RUNNING
        self.stats.start_time = time.time()
        self._message_rate_window.clear()
        
        self._main_thread = threading.Thread(target=self._main_loop, daemon=True)
        self._main_thread.start()
        
        self.logger.info("IDS Engine started")
        message_bus.publish(Message(
            type=MessageType.STATUS,
            source="engine",
            data={"state": "running", "detectors": len(self.detectors)}
        ))
    
    def stop(self) -> None:
        if self.state == EngineState.STOPPED:
            return
        
        self.state = EngineState.STOPPING
        self._running = False
        
        for detector in self.detectors:
            try:
                detector.shutdown()
            except Exception as e:
                self.logger.error(f"Error shutting down {detector.__class__.__name__}: {e}")
        
        if self._main_thread and self._main_thread.is_alive():
            self._main_thread.join(timeout=5.0)
        
        self.state = EngineState.STOPPED
        self.logger.info("IDS Engine stopped")
        message_bus.publish(Message(
            type=MessageType.STATUS,
            source="engine",
            data={"state": "stopped"}
        ))
    
    def _main_loop(self) -> None:
        interval = config.get('engine.processing_interval', 0.1)
        
        while self._running:
            start = time.time()
            
            for detector in self.detectors:
                try:
                    detector.update()
                except Exception as e:
                    self.logger.error(f"Error in detector {detector.__class__.__name__}: {e}")
                    self.stats.errors += 1
            
            if time.time() - self._last_stats_log > config.get('engine.statistics_interval', 30.0):
                self._log_statistics()
                self._last_stats_log = time.time()
            
            elapsed = time.time() - start
            sleep_time = max(0, interval - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)
    
    def _on_mavlink_message(self, message: Message) -> None:
        self._message_count += 1
        self.stats.messages_processed += 1
        self.stats.last_message_time = time.time()
        self._message_rate_window.append(self.stats.last_message_time)
        
        for detector in self.detectors:
            try:
                detector.on_mavlink_message(message.data)
            except Exception as e:
                self.logger.error(f"Detector {detector.__class__.__name__} error: {e}")
                self.stats.errors += 1
    
    def _on_command(self, message: Message) -> None:
        for detector in self.detectors:
            try:
                if hasattr(detector, 'on_command'):
                    detector.on_command(message.data)
            except Exception as e:
                self.logger.error(f"Detector command error: {e}")
    
    def _on_telemetry(self, message: Message) -> None:
        for detector in self.detectors:
            try:
                if hasattr(detector, 'on_telemetry'):
                    detector.on_telemetry(message.data)
            except Exception as e:
                self.logger.error(f"Detector telemetry error: {e}")
    
    def _log_statistics(self) -> None:
        uptime = time.time() - self.stats.start_time
        rate = self.stats.messages_processed / uptime if uptime > 0 else 0
        
        self.logger.info(
            f"Stats: msgs={self.stats.messages_processed}, "
            f"rate={rate:.1f}/s, alerts={self.stats.alerts_generated}, "
            f"errors={self.stats.errors}"
        )
        
        message_bus.publish(Message(
            type=MessageType.STATUS,
            source="engine",
            data={
                "messages_processed": self.stats.messages_processed,
                "rate": rate,
                "alerts_generated": self.stats.alerts_generated,
                "alerts_by_type": self.stats.alerts_by_type,
                "uptime": uptime
            }
        ))
    
    def record_alert(self, alert_type: str) -> None:
        self.stats.alerts_generated += 1
        self.stats.alerts_by_type[alert_type] = self.stats.alerts_by_type.get(alert_type, 0) + 1
    
    def get_status(self) -> Dict[str, Any]:
        now = time.time()
        uptime = now - self.stats.start_time
        while self._message_rate_window and now - self._message_rate_window[0] > 5.0:
            self._message_rate_window.popleft()
        rate = len(self._message_rate_window) / 5.0
        return {
            "state": self.state.value,
            "detectors": len(self.detectors),
            "stats": {
                "messages_processed": self.stats.messages_processed,
                "rate": rate,
                "alerts_generated": self.stats.alerts_generated,
                "alerts_by_type": self.stats.alerts_by_type,
                "uptime": uptime,
                "errors": self.stats.errors
            }
        }


engine = IDSEngine()