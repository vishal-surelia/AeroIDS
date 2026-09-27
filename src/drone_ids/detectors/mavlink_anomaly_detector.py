"""
Drone IDS - MAVLink Anomaly Detector (Part 1)
"""
import time
import logging
import statistics
from typing import Dict, Any
from collections import defaultdict, deque

from .base_detector import BaseDetector, Alert, AlertSeverity


class MAVLinkAnomalyDetector(BaseDetector):
    def __init__(self):
        super().__init__("MAVLinkAnomalyDetector")
        self.learning_window = self.get_config('learning_window', 300)
        self.rate_threshold = self.get_config('rate_deviation_threshold', 3.0)
        self.max_seq_gap = self.get_config('max_sequence_gap', 10)
        self.enable_seq_check = self.get_config('enable_sequence_check', True)
        self.monitored_types = set(self.get_config('monitored_message_types', []))
        self.enable_payload = self.get_config('enable_payload_analysis', True)
        
        self.message_rates = defaultdict(lambda: deque(maxlen=1000))
        self.message_counts = defaultdict(int)
        self.last_sequence = defaultdict(lambda: defaultdict(int))
        self.learning_start = time.time()
        self.learning_complete = False
        self.baseline_rates = {}
        self.baseline_std = {}
    
    def initialize(self) -> None:
        self.logger.info("MAVLink Anomaly Detector initialized")
        self._initialized = True
    
    def shutdown(self) -> None:
        self.logger.info("MAVLink Anomaly Detector shutdown")
    
    def update(self) -> None:
        if not self.learning_complete and time.time() - self.learning_start > self.learning_window:
            self._finalize_learning()
    
    def on_mavlink_message(self, message: Dict[str, Any]) -> None:
        msg_type = message.get('type', '')
        src_sys = message.get('src_sys', 0)
        src_comp = message.get('src_comp', 0)
        seq = message.get('seq', 0)
        current_time = time.time()
        
        self.message_counts[msg_type] += 1
        self.message_rates[msg_type].append(current_time)
        
        if self.enable_seq_check and seq > 0:
            last_seq = self.last_sequence[src_sys][src_comp]
            if last_seq > 0 and seq != (last_seq + 1) % 256:
                gap = (seq - last_seq) % 256
                if gap > self.max_seq_gap:
                    self._alert('mavlink_sequence_gap', AlertSeverity.WARNING,
                        f"MAVLink sequence gap: {gap} (sys={src_sys}, comp={src_comp}, type={msg_type})",
                        {'gap': gap, 'expected': (last_seq + 1) % 256, 'received': seq,
                         'src_sys': src_sys, 'src_comp': src_comp, 'type': msg_type}, 'T1557')
            self.last_sequence[src_sys][src_comp] = seq
        
        if self.learning_complete and msg_type in self.baseline_rates:
            self._check_rate_anomaly(msg_type, current_time)
    
    def _finalize_learning(self) -> None:
        for msg_type, timestamps in self.message_rates.items():
            if len(timestamps) > 10:
                intervals = [timestamps[i] - timestamps[i-1] for i in range(1, len(timestamps))]
                rates = [1.0 / max(i, 0.001) for i in intervals]
                self.baseline_rates[msg_type] = statistics.mean(rates)
                self.baseline_std[msg_type] = statistics.stdev(rates) if len(rates) > 1 else 0
        self.learning_complete = True
        self.logger.info(f"Learning complete. Baselines: {self.baseline_rates}")
    
    def _check_rate_anomaly(self, msg_type: str, current_time: float) -> None:
        timestamps = self.message_rates[msg_type]
        if len(timestamps) < 2:
            return
        recent = [t for t in timestamps if current_time - t < 5.0]
        if len(recent) < 2:
            return
        intervals = [recent[i] - recent[i-1] for i in range(1, len(recent))]
        current_rate = len(recent) / max(sum(intervals), 0.001)
        baseline = self.baseline_rates.get(msg_type, 0)
        std = self.baseline_std.get(msg_type, 0)
        if baseline > 0 and std > 0:
            z_score = abs(current_rate - baseline) / std
            if z_score > self.rate_threshold:
                self._alert('mavlink_rate_anomaly', AlertSeverity.WARNING,
                    f"MAVLink rate anomaly {msg_type}: {current_rate:.1f}/s (baseline: {baseline:.1f}/s, z={z_score:.1f})",
                    {'type': msg_type, 'current_rate': current_rate, 'baseline': baseline,
                     'z_score': z_score, 'threshold': self.rate_threshold}, 'T1499')
    
    def _alert(self, alert_type: str, severity: AlertSeverity, description: str,
               details: Dict[str, Any], mitre: str) -> None:
        alert = Alert(detector_name=self.name, alert_type=alert_type, severity=severity,
            description=description, details=details, confidence=0.75, mitre_attack=mitre)
        self.generate_alert(alert)