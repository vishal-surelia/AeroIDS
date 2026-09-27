"""
Drone IDS - DoS Detector
"""
import time
import logging
from typing import Dict, Any
from collections import deque

from .base_detector import BaseDetector, Alert, AlertSeverity


class DoSDetector(BaseDetector):
    def __init__(self):
        super().__init__("DoSDetector")
        self.monitor_link = self.get_config('monitor_link_quality', True)
        self.min_rssi = self.get_config('min_rssi_dbm', -90)
        self.max_loss = self.get_config('max_packet_loss_percent', 10.0)
        self.max_msg_rate = self.get_config('max_messages_per_second', 500)
        self.flood_window = self.get_config('flood_window', 1.0)
        self.hb_timeout = self.get_config('heartbeat_timeout', 10.0)
        self.monitor_cpu = self.get_config('monitor_cpu_memory', True)
        self.max_cpu = self.get_config('max_cpu_percent', 90.0)
        self.max_mem = self.get_config('max_memory_percent', 85.0)
        self.detect_jamming = self.get_config('detect_jamming', True)
        self.rssi_drop_thresh = self.get_config('rssi_drop_threshold_dbm', 20.0)
        self.snr_drop_thresh = self.get_config('snr_drop_threshold_db', 15.0)
        
        # Systems to EXCLUDE from heartbeat monitoring (autopilot=1, IDS itself=255)
        self.excluded_heartbeat_systems = {1, 255}
        
        self.message_times = deque(maxlen=10000)
        self.last_heartbeat = {}
        self.last_rssi = None
        self.last_snr = None
        self.heartbeat_systems = set()
    
    def initialize(self) -> None:
        self.logger.info("DoS Detector initialized")
        self._initialized = True
    
    def shutdown(self) -> None:
        self.logger.info("DoS Detector shutdown")
    
    def update(self) -> None:
        now = time.time()
        # Check heartbeat timeouts (excluding autopilot=1 and IDS itself=255)
        for sys_id, last_hb in self.last_heartbeat.items():
            if sys_id in self.excluded_heartbeat_systems:
                continue
            if now - last_hb > self.hb_timeout:
                self._alert('heartbeat_timeout', AlertSeverity.CRITICAL,
                    f"Heartbeat timeout for system {sys_id} ({now - last_hb:.1f}s)",
                    {'system': sys_id, 'timeout': self.hb_timeout, 'elapsed': now - last_hb}, 'T1499')
        
        # Check message flood
        recent = [t for t in self.message_times if now - t < self.flood_window]
        if len(recent) > self.max_msg_rate:
            self._alert('message_flood', AlertSeverity.WARNING,
                f"Message flood: {len(recent)} msgs in {self.flood_window}s (limit: {self.max_msg_rate})",
                {'count': len(recent), 'window': self.flood_window, 'limit': self.max_msg_rate}, 'T1499')
    
    def on_mavlink_message(self, message: Dict[str, Any]) -> None:
        msg_type = message.get('type', '')
        src_sys = message.get('src_sys', 0)
        current_time = time.time()
        
        self.message_times.append(current_time)
        self.heartbeat_systems.add(src_sys)
        
        # Detect floods as packets arrive instead of waiting for the periodic update.
        recent = [t for t in self.message_times if current_time - t < self.flood_window]
        if len(recent) > self.max_msg_rate:
            self._alert('message_flood', AlertSeverity.WARNING,
                f"Message flood: {len(recent)} msgs in {self.flood_window}s (limit: {self.max_msg_rate})",
                {'count': len(recent), 'window': self.flood_window, 'limit': self.max_msg_rate}, 'T1499')

        if msg_type == 'HEARTBEAT':
            self.last_heartbeat[src_sys] = current_time
        
        elif msg_type == 'RADIO_STATUS':
            rssi = message.get('rssi', 0)
            snr = message.get('remrssi', 0)  # remote RSSI as proxy for SNR
            
            if self.monitor_link:
                if rssi < self.min_rssi:
                    self._alert('low_rssi', AlertSeverity.WARNING,
                        f"Low RSSI: {rssi} dBm (min: {self.min_rssi})",
                        {'rssi': rssi, 'min_rssi': self.min_rssi}, 'T1499')
                
                if self.last_rssi is not None and self.detect_jamming:
                    rssi_drop = self.last_rssi - rssi
                    if rssi_drop > self.rssi_drop_thresh:
                        self._alert('rssi_sudden_drop', AlertSeverity.CRITICAL,
                            f"Sudden RSSI drop: {rssi_drop} dBm (possible jamming)",
                            {'rssi_drop': rssi_drop, 'prev': self.last_rssi, 'current': rssi}, 'T1499')
                
                self.last_rssi = rssi
                self.last_snr = snr
    
    def _alert(self, alert_type: str, severity: AlertSeverity, description: str,
               details: Dict[str, Any], mitre: str) -> None:
        alert = Alert(detector_name=self.name, alert_type=alert_type, severity=severity,
            description=description, details=details, confidence=0.85, mitre_attack=mitre)
        self.generate_alert(alert)