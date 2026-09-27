"""
Drone IDS - Command Injection Detector (Part 1)
"""
import time
import logging
import math
from typing import Dict, Any, Set
from collections import deque

from .base_detector import BaseDetector, Alert, AlertSeverity


class CommandInjectionDetector(BaseDetector):
    def __init__(self):
        super().__init__("CommandInjectionDetector")
        self.allowed_sources = set(self.get_config('allowed_command_sources', [1, 255]))
        self.blocked_commands = set(self.get_config('blocked_commands', []))
        self.max_cmd_rate = self.get_config('max_commands_per_second', 20)
        self.burst_threshold = self.get_config('command_burst_threshold', 50)
        self.critical_params = set(self.get_config('critical_params', []))
        self.validate_mission = self.get_config('validate_mission_items', True)
        self.max_wp_dist = self.get_config('max_waypoint_distance_km', 100.0)
        self.expect_ack = self.get_config('expect_command_ack', True)
        self.ack_timeout = self.get_config('ack_timeout', 5.0)
        
        self.command_times = deque(maxlen=1000)
        self.pending_acks = {}
        self.param_values = {}
        self.mission_items = {}
    
    def initialize(self) -> None:
        self.logger.info("Command Injection Detector initialized")
        self._initialized = True
    
    def shutdown(self) -> None:
        self.logger.info("Command Injection Detector shutdown")
    
    def update(self) -> None:
        now = time.time()
        expired = [k for k, v in self.pending_acks.items() if now - v['time'] > self.ack_timeout]
        for k in expired:
            if self.expect_ack:
                self._alert('command_missing_ack', AlertSeverity.WARNING,
                    f"Command {k} not acknowledged within {self.ack_timeout}s",
                    {'command': k, 'timeout': self.ack_timeout}, 'T1557')
            del self.pending_acks[k]
    
    def on_mavlink_message(self, message: Dict[str, Any]) -> None:
        msg_type = message.get('type', '')
        if msg_type in ('COMMAND_LONG', 'COMMAND_INT'):
            self._process_command(message)
        elif msg_type == 'COMMAND_ACK':
            self._process_command_ack(message)
        elif msg_type == 'PARAM_VALUE':
            self._process_param_value(message)
        elif msg_type in ('MISSION_ITEM', 'MISSION_ITEM_INT'):
            self._process_mission_item(message)
    
    def _process_command(self, msg: Dict[str, Any]) -> None:
        src_sys = msg.get('src_sys', 0)
        src_comp = msg.get('src_comp', 0)
        cmd_id = msg.get('command', 0)
        current_time = time.time()
        
        if src_sys not in self.allowed_sources:
            self._alert('command_unauthorized_source', AlertSeverity.CRITICAL,
                f"Command {cmd_id} from unauthorized source sys={src_sys} comp={src_comp}",
                {'command': cmd_id, 'src_sys': src_sys, 'src_comp': src_comp}, 'T1021')
        
        if cmd_id in self.blocked_commands:
            self._alert('command_blocked', AlertSeverity.CRITICAL,
                f"Blocked command {cmd_id} received",
                {'command': cmd_id, 'src_sys': src_sys}, 'T1562')
        
        self.command_times.append(current_time)
        recent = [t for t in self.command_times if current_time - t < 1.0]
        if len(recent) > self.max_cmd_rate:
            self._alert('command_rate_exceeded', AlertSeverity.WARNING,
                f"Command rate {len(recent)}/s exceeds limit {self.max_cmd_rate}/s",
                {'rate': len(recent), 'limit': self.max_cmd_rate}, 'T1499')
        
        recent_5s = [t for t in self.command_times if current_time - t < 5.0]
        if len(recent_5s) > self.burst_threshold:
            self._alert('command_burst', AlertSeverity.WARNING,
                f"Command burst: {len(recent_5s)} commands in 5s",
                {'count': len(recent_5s), 'threshold': self.burst_threshold}, 'T1499')
        
        if self.expect_ack:
            self.pending_acks[cmd_id] = {'time': current_time, 'src_sys': src_sys}
        
        if cmd_id == 400:  # ARM/DISARM
            param1 = msg.get('param1', 0)
            action = 'ARM' if param1 == 1 else 'DISARM' if param1 == 0 else 'UNKNOWN'
            sev = AlertSeverity.WARNING if action in ('ARM', 'DISARM') else AlertSeverity.INFO
            self._alert(f'command_{action.lower()}_attempt', sev,
                f"{action} command from sys={src_sys}",
                {'command': cmd_id, 'src_sys': src_sys, 'action': action}, 'T1562')
    
    def _process_command_ack(self, msg: Dict[str, Any]) -> None:
        cmd_id = msg.get('command', 0)
        if cmd_id in self.pending_acks:
            del self.pending_acks[cmd_id]
    
    def _process_param_value(self, msg: Dict[str, Any]) -> None:
        param_id = msg.get('param_id', '').strip('\x00')
        param_value = msg.get('param_value', 0)
        if param_id in self.critical_params:
            old_value = self.param_values.get(param_id)
            if old_value is not None and old_value != param_value:
                self._alert('param_tampering', AlertSeverity.CRITICAL,
                    f"Critical parameter {param_id} changed: {old_value} -> {param_value}",
                    {'param': param_id, 'old': old_value, 'new': param_value}, 'T1562')
            self.param_values[param_id] = param_value
    
    def _process_mission_item(self, msg: Dict[str, Any]) -> None:
        if not self.validate_mission:
            return
        seq = msg.get('seq', 0)
        lat = msg.get('x', msg.get('lat', 0)) * 1e-7
        lon = msg.get('y', msg.get('lon', 0)) * 1e-7
        self.mission_items[seq] = (lat, lon)
        if seq > 0 and seq - 1 in self.mission_items:
            prev_lat, prev_lon = self.mission_items[seq - 1]
            dist = self._haversine(prev_lat, prev_lon, lat, lon) / 1000.0
            if dist > self.max_wp_dist:
                self._alert('mission_excessive_distance', AlertSeverity.WARNING,
                    f"Waypoint {seq} distance {dist:.1f}km exceeds limit {self.max_wp_dist}km",
                    {'wp': seq, 'distance_km': dist, 'limit_km': self.max_wp_dist}, 'T1505')
    
    def _haversine(self, lat1, lon1, lat2, lon2):
        R = 6371000
        phi1, phi2 = math.radians(lat1), math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    
    def _alert(self, alert_type: str, severity: AlertSeverity, description: str,
               details: Dict[str, Any], mitre: str) -> None:
        alert = Alert(detector_name=self.name, alert_type=alert_type, severity=severity,
            description=description, details=details, confidence=0.9, mitre_attack=mitre)
        self.generate_alert(alert)