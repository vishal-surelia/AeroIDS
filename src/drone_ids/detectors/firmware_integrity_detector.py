"""
Drone IDS - Firmware Integrity Detector
"""
import time
import logging
import hashlib
from typing import Dict, Any

from .base_detector import BaseDetector, Alert, AlertSeverity


class FirmwareIntegrityDetector(BaseDetector):
    def __init__(self):
        super().__init__("FirmwareIntegrityDetector")
        self.verify_boot = self.get_config('verify_boot_hash', True)
        self.expected_hash = self.get_config('expected_firmware_hash', '')
        self.monitor_params = self.get_config('monitor_param_integrity', True)
        self.critical_hashes = self.get_config('critical_param_hashes', {})
        self.detect_memory = self.get_config('detect_memory_corruption', True)
        self.monitor_reboots = self.get_config('monitor_unexpected_reboots', True)
        self.max_reboots = self.get_config('max_reboots_per_hour', 2)
        
        self.boot_verified = False
        self.reboot_times = []
        self.param_hashes = {}
    
    def initialize(self) -> None:
        self.logger.info("Firmware Integrity Detector initialized")
        self._initialized = True
        
        # Simulate boot verification
        if self.verify_boot and self.expected_hash:
            self._verify_boot_hash()
    
    def shutdown(self) -> None:
        self.logger.info("Firmware Integrity Detector shutdown")
    
    def update(self) -> None:
        now = time.time()
        # Clean old reboot records
        self.reboot_times = [t for t in self.reboot_times if now - t < 3600]
        
        if self.monitor_reboots and len(self.reboot_times) > self.max_reboots:
            self._alert('excessive_reboots', AlertSeverity.WARNING,
                f"Excessive reboots: {len(self.reboot_times)} in last hour (max: {self.max_reboots})",
                {'reboot_count': len(self.reboot_times), 'max': self.max_reboots}, 'T1529')
    
    def on_mavlink_message(self, message: Dict[str, Any]) -> None:
        msg_type = message.get('type', '')
        
        if msg_type == 'SYS_STATUS':
            # Check for unexpected reboot (system just started)
            if not self.boot_verified:
                self._verify_boot_hash()
                self.boot_verified = True
        
        elif msg_type == 'PARAM_VALUE':
            self._check_param_integrity(message)
    
    def _verify_boot_hash(self) -> None:
        # In real implementation, compute hash of firmware binary
        # For SITL demo, we simulate this
        if self.expected_hash:
            # Simulated current hash
            current_hash = "simulated_firmware_hash_12345"
            if current_hash != self.expected_hash:
                self._alert('firmware_hash_mismatch', AlertSeverity.CRITICAL,
                    f"Firmware hash mismatch! Expected: {self.expected_hash}, Got: {current_hash}",
                    {'expected': self.expected_hash, 'actual': current_hash}, 'T1542')
    
    def _check_param_integrity(self, msg: Dict[str, Any]) -> None:
        if not self.monitor_params:
            return
        
        param_id = msg.get('param_id', '').strip('\x00')
        param_value = msg.get('param_value', 0)
        
        if param_id in self.critical_hashes:
            expected_hash = self.critical_hashes[param_id]
            actual_hash = hashlib.sha256(str(param_value).encode()).hexdigest()[:16]
            
            if actual_hash != expected_hash:
                self._alert('param_integrity_violation', AlertSeverity.WARNING,
                    f"Critical parameter {param_id} integrity check failed",
                    {'param': param_id, 'expected_hash': expected_hash, 'actual_hash': actual_hash}, 'T1562')
    
    def record_reboot(self) -> None:
        """Call this when a reboot is detected."""
        self.reboot_times.append(time.time())
        self._alert('reboot_detected', AlertSeverity.WARNING,
            "System reboot detected",
            {'reboot_count_1h': len(self.reboot_times)}, 'T1529')
    
    def _alert(self, alert_type: str, severity: AlertSeverity, description: str,
               details: Dict[str, Any], mitre: str) -> None:
        alert = Alert(detector_name=self.name, alert_type=alert_type, severity=severity,
            description=description, details=details, confidence=0.95, mitre_attack=mitre)
        self.generate_alert(alert)