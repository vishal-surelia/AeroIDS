"""
Drone IDS - GPS Spoofing Detector (Part 1)
"""
import time
import math
import logging
from typing import Dict, Any, Optional, List
from collections import deque

from .base_detector import BaseDetector, Alert, AlertSeverity


class GPSSpoofingDetector(BaseDetector):
    """Detects GPS spoofing attacks."""
    
    def __init__(self):
        super().__init__("GPSSpoofingDetector")
        
        self.max_position_jump = self.get_config('max_position_jump_meters', 50.0)
        self.max_velocity = self.get_config('max_velocity_mps', 100.0)
        self.min_satellites = self.get_config('min_satellites', 6)
        self.max_hdop = self.get_config('max_hdop', 5.0)
        self.max_vdop = self.get_config('max_vdop', 5.0)
        self.enable_imu_cross_check = self.get_config('enable_imu_cross_check', True)
        self.max_imu_gps_discrepancy = self.get_config('max_imu_gps_discrepancy_meters', 10.0)
        self.innovation_threshold = self.get_config('innovation_threshold', 3.0)
        
        self.last_gps_position: Optional[Dict[str, float]] = None
        self.last_gps_time: float = 0
        self.last_imu_position: Optional[Dict[str, float]] = None
        self.velocity_history: deque = deque(maxlen=10)
        self.position_history: deque = deque(maxlen=20)
        self.satellite_history: deque = deque(maxlen=50)
        self.hdop_history: deque = deque(maxlen=50)
        self.ekf_innovations: deque = deque(maxlen=100)
    
    def initialize(self) -> None:
        self.logger.info("GPS Spoofing Detector initialized")
        self._initialized = True
    
    def shutdown(self) -> None:
        self.logger.info("GPS Spoofing Detector shutdown")
    
    def update(self) -> None:
        pass
    
    def on_mavlink_message(self, message: Dict[str, Any]) -> None:
        msg_type = message.get('type', '')
        if msg_type == 'GPS_RAW_INT':
            self._process_gps_raw(message)
        elif msg_type == 'GLOBAL_POSITION_INT':
            self._process_global_position(message)
        elif msg_type == 'ATTITUDE':
            self._process_attitude(message)
        elif msg_type == 'EKF_STATUS_REPORT':
            self._process_ekf_status(message)
    
    def _process_gps_raw(self, msg: Dict[str, Any]) -> None:
        lat = msg.get('lat', 0) * 1e-7
        lon = msg.get('lon', 0) * 1e-7
        alt = msg.get('alt', 0) * 1e-3
        sat_count = msg.get('satellites_visible', 0)
        hdop = msg.get('eph', 0) / 100.0 if msg.get('eph') else 0
        vdop = msg.get('epv', 0) / 100.0 if msg.get('epv') else 0
        fix_type = msg.get('fix_type', 0)
        current_time = time.time()
        
        if sat_count > 0 and sat_count < self.min_satellites and fix_type >= 3:
            self._alert('gps_insufficient_satellites', AlertSeverity.WARNING,
                f"GPS 3D fix with only {sat_count} satellites (min: {self.min_satellites})",
                {'satellites': sat_count, 'fix_type': fix_type, 'lat': lat, 'lon': lon}, 'T1557')
        
        if hdop > self.max_hdop:
            self._alert('gps_high_hdop', AlertSeverity.WARNING,
                f"GPS HDOP {hdop:.1f} exceeds threshold {self.max_hdop}",
                {'hdop': hdop, 'threshold': self.max_hdop}, 'T1557')
        
        if vdop > self.max_vdop:
            self._alert('gps_high_vdop', AlertSeverity.WARNING,
                f"GPS VDOP {vdop:.1f} exceeds threshold {self.max_vdop}",
                {'vdop': vdop, 'threshold': self.max_vdop}, 'T1557')
        
        if self.last_gps_position is not None:
            dt = current_time - self.last_gps_time
            if dt > 0:
                distance = self._haversine_distance(
                    self.last_gps_position['lat'], self.last_gps_position['lon'], lat, lon)
                velocity = distance / dt
                self.velocity_history.append(velocity)
                self.position_history.append((lat, lon, current_time))
                
                if velocity > self.max_velocity:
                    self._alert('gps_impossible_velocity', AlertSeverity.CRITICAL,
                        f"GPS velocity {velocity:.1f} m/s exceeds maximum {self.max_velocity} m/s",
                        {'velocity': velocity, 'max_velocity': self.max_velocity, 'dt': dt}, 'T1557')
                
                if distance > self.max_position_jump:
                    self._alert('gps_position_jump', AlertSeverity.CRITICAL,
                        f"GPS position jump {distance:.1f}m in {dt:.1f}s (max: {self.max_position_jump}m)",
                        {'distance': distance, 'dt': dt, 'max_jump': self.max_position_jump}, 'T1557')
        
        self.satellite_history.append(sat_count)
        self.hdop_history.append(hdop)
        self.last_gps_position = {'lat': lat, 'lon': lon, 'alt': alt}
        self.last_gps_time = current_time
    
    def _process_global_position(self, msg: Dict[str, Any]) -> None:
        if not self.enable_imu_cross_check:
            return
        lat = msg.get('lat', 0) * 1e-7
        lon = msg.get('lon', 0) * 1e-7
        alt = msg.get('alt', 0) * 1e-3
        if self.last_gps_position is not None:
            distance = self._haversine_distance(
                self.last_gps_position['lat'], self.last_gps_position['lon'], lat, lon)
            if distance > self.max_imu_gps_discrepancy:
                self._alert('gps_imu_discrepancy', AlertSeverity.WARNING,
                    f"GPS-IMU position discrepancy: {distance:.1f}m (threshold: {self.max_imu_gps_discrepancy}m)",
                    {'gps_lat': self.last_gps_position['lat'], 'gps_lon': self.last_gps_position['lon'],
                     'fused_lat': lat, 'fused_lon': lon, 'discrepancy': distance}, 'T1557')
        self.last_imu_position = {'lat': lat, 'lon': lon, 'alt': alt}
    
    def _process_attitude(self, msg: Dict[str, Any]) -> None:
        pass
    
    def _process_ekf_status(self, msg: Dict[str, Any]) -> None:
        vel_innov = msg.get('vel_innov', 0)
        pos_innov = msg.get('pos_innov', 0)
        if vel_innov or pos_innov:
            innov_magnitude = math.sqrt(vel_innov**2 + pos_innov**2)
            self.ekf_innovations.append(innov_magnitude)
            if len(self.ekf_innovations) > 20:
                mean_innov = sum(self.ekf_innovations) / len(self.ekf_innovations)
                std_innov = math.sqrt(sum((x - mean_innov)**2 for x in self.ekf_innovations) / len(self.ekf_innovations))
                if std_innov > 0 and innov_magnitude > mean_innov + self.innovation_threshold * std_innov:
                    self._alert('gps_ekf_innovation_spike', AlertSeverity.WARNING,
                        f"EKF innovation spike: {innov_magnitude:.3f} (threshold: {self.innovation_threshold}σ)",
                        {'innovation': innov_magnitude, 'mean': mean_innov, 'std': std_innov}, 'T1557')
    
    def _haversine_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        R = 6371000
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lon2 - lon1)
        a = math.sin(delta_phi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        return R * c
    
    def _alert(self, alert_type: str, severity: AlertSeverity, description: str,
               details: Dict[str, Any], mitre: str) -> None:
        alert = Alert(detector_name=self.name, alert_type=alert_type, severity=severity,
            description=description, details=details, confidence=0.85, mitre_attack=mitre)
        self.generate_alert(alert)