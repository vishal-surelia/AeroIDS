"""
Drone IDS - Alert Manager
"""
import time
import json
import logging
import threading
from typing import Dict, Any
from pathlib import Path
from queue import Queue, Empty

from ..core.message_bus import message_bus, Message, MessageType
from ..core.config import config


class AlertManager:
    def __init__(self):
        self.logger = logging.getLogger('drone_ids.alert_manager')
        self.logger.setLevel(logging.INFO)
        
        self.alert_queue = Queue(maxsize=10000)
        self._running = False
        self._process_thread = None
        
        self.alert_log_path = Path(config.get('alerting.alert_log_path', 'logs/alerts.log'))
        self.evidence_log_path = Path(config.get('alerting.evidence_log_path', 'logs/evidence.log'))
        self.chain_log_path = Path(config.get('forensics.chain_of_custody_log', 'logs/chain_of_custody.log'))
        
        for p in [self.alert_log_path, self.evidence_log_path, self.chain_log_path]:
            p.parent.mkdir(parents=True, exist_ok=True)
        
        self.severity_levels = {'DEBUG': 0, 'INFO': 1, 'WARNING': 2, 'ERROR': 3, 'CRITICAL': 4}
        self.min_console = self.severity_levels.get(config.get('alerting.min_severity_console', 'WARNING'), 2)
        self.min_file = self.severity_levels.get(config.get('alerting.min_severity_file', 'INFO'), 1)
        
        self.last_hash = "0" * 64
        
        message_bus.subscribe(MessageType.ALERT, self._on_alert)
    
    def start(self) -> None:
        self._running = True
        self._process_thread = threading.Thread(target=self._process_loop, daemon=True)
        self._process_thread.start()
        self.logger.info("Alert Manager started")
    
    def stop(self) -> None:
        self._running = False
        if self._process_thread and self._process_thread.is_alive():
            self._process_thread.join(timeout=5.0)
        self.logger.info("Alert Manager stopped")
    
    def _on_alert(self, message: Message) -> None:
        try:
            self.alert_queue.put_nowait(message.data)
        except Exception:
            self.logger.error("Alert queue full, dropping alert")
    
    def _process_loop(self) -> None:
        while self._running:
            try:
                alert_data = self.alert_queue.get(timeout=1.0)
                self._process_alert(alert_data)
            except Empty:
                continue
            except Exception as e:
                self.logger.error(f"Alert processing error: {e}")
    
    def _process_alert(self, alert: Dict[str, Any]) -> None:
        severity = alert.get('severity', 'INFO')
        severity_level = self.severity_levels.get(severity, 1)
        
        if severity_level >= self.min_console and config.get('alerting.console_output', True):
            print(ConsoleFormatter().format(alert))
        
        if severity_level >= self.min_file and config.get('alerting.file_output', True):
            self._write_alert(alert)
            self._write_evidence(alert)
            self._update_chain_of_custody(alert)
    
    def _write_alert(self, alert: Dict[str, Any]) -> None:
        with open(self.alert_log_path, 'a') as f:
            f.write(FileFormatter().format(alert) + '\n')
    
    def _write_evidence(self, alert: Dict[str, Any]) -> None:
        evidence = {
            'timestamp': alert.get('timestamp', time.time()),
            'detector': alert.get('detector'),
            'alert_type': alert.get('alert_type'),
            'severity': alert.get('severity'),
            'description': alert.get('description'),
            'details': alert.get('details', {}),
            'mitre_attack': alert.get('mitre_attack', ''),
            'confidence': alert.get('confidence', 1.0),
            'evidence_hash': self._compute_hash(alert)
        }
        with open(self.evidence_log_path, 'a') as f:
            f.write(json.dumps(evidence) + '\n')
    
    def _update_chain_of_custody(self, alert: Dict[str, Any]) -> None:
        import hashlib
        chain_data = f"{self.last_hash}{json.dumps(alert, sort_keys=True)}"
        new_hash = hashlib.sha256(chain_data.encode()).hexdigest()
        
        with open(self.chain_log_path, 'a') as f:
            f.write(json.dumps({
                'timestamp': time.time(),
                'alert_type': alert.get('alert_type'),
                'previous_hash': self.last_hash,
                'current_hash': new_hash,
                'evidence_hash': self._compute_hash(alert)
            }) + '\n')
        
        self.last_hash = new_hash
    
    def _compute_hash(self, data: Dict[str, Any]) -> str:
        import hashlib
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    
    def get_recent_alerts(self, limit: int = 100) -> list:
        alerts = []
        try:
            with open(self.alert_log_path, 'r') as f:
                for line in f.readlines()[-limit:]:
                    try:
                        alerts.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except FileNotFoundError:
            pass
        return alerts


class ConsoleFormatter:
    COLORS = {'DEBUG': '\033[36m', 'INFO': '\033[32m', 'WARNING': '\033[33m',
              'ERROR': '\033[31m', 'CRITICAL': '\033[91m', 'RESET': '\033[0m'}
    
    def format(self, alert: Dict[str, Any]) -> str:
        severity = alert.get('severity', 'INFO')
        color = self.COLORS.get(severity, '')
        reset = self.COLORS['RESET']
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(alert.get('timestamp', time.time())))
        return f"{color}[{timestamp}] {severity:8s} {alert.get('detector', 'UNKNOWN'):25s} | {alert.get('alert_type', 'UNKNOWN'):30s} | {alert.get('description', '')}{reset}"


class FileFormatter:
    def format(self, alert: Dict[str, Any]) -> str:
        import json
        return json.dumps(alert)