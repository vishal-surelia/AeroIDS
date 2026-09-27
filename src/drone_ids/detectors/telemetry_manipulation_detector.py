"""
Drone IDS - Telemetry Manipulation Detector (Part 1)
"""
import time
import logging
import math
from typing import Dict, Any

from .base_detector import BaseDetector, Alert, AlertSeverity


class TelemetryManipulationDetector(BaseDetector):
    def __init__(self):
        super().__init__("TelemetryManipulationDetector")
        self.max_attitude_rate = self.get_config('max_attitude_rate_dps', 300.0)
        self.max_accel = self.get_config('max_acceleration_g', 10.0)
        self.max_pos_rate = self.get_config('max_position_rate_mps', 100.0)
        self.max_vel_change = self.get_config('max_velocity_change_mps2', 50.0)
        self.enable_baro_check = self.get_config('enable_baro_gps_cross_check', True)
        self.max_alt_diff = self.get_config('max_altitude_discrepancy_meters', 20.0)
        self.enable_compass_check = self.get_config('enable_compass_cross_check', True)
        self.max_heading_diff = self.get_config('max_heading_discrepancy_deg', 30.0)
        self.monitor_power = self.get_config('monitor_power_system', True)
        self.max_volt_jump = self.get_config('max_voltage_jump_v', 2.0)
        self.max_curr_jump = self.get_config('max_current_jump_a', 10.0)
        
        self.last_attitude = None
        self.last_attitude_time = 0
        self.last_position = None
        self.last_position_time = 0
        self.last_velocity = None
        self.last_voltage = None
        self.last_current = None
        self.gps_alt = None
        self.baro_alt = None
        self.last_heading = None
    
    def initialize(self) -> None:
        self.logger.info("Telemetry Manipulation Detector initialized")
        self._initialized = True
    
    def shutdown(self) -> None:
        self.logger.info("Telemetry Manipulation Detector shutdown")
    
    def update(self) -> None:
        pass
    
    def on_mavlink_message(self, message: Dict[str, Any]) -> None:
        msg_type = message.get('type', '')
        if msg_type == 'ATTITUDE':
            self._check_attitude(message)
        elif msg_type == 'GLOBAL_POSITION_INT':
            self._check_position(message)
        elif msg_type == 'VFR_HUD':
            self._check_vfr_hud(message)
        elif msg_type == 'SYS_STATUS':
            self._check_power(message)
    
    def _check_attitude(self, msg: Dict[str, Any]) -> None:
        rollspeed = msg.get('rollspeed', 0)
        pitchspeed = msg.get('pitchspeed', 0)
        yawspeed = msg.get('yawspeed', 0)
        max_rate_rad = max(abs(rollspeed), abs(pitchspeed), abs(yawspeed))
        max_rate_dps = math.degrees(max_rate_rad)
        if max_rate_dps > self.max_attitude_rate:
            self._alert('impossible_attitude_rate', AlertSeverity.WARNING,
                f"Impossible attitude rate: {max_rate_dps:.1f} deg/s",
                {'roll_rate': math.degrees(rollspeed), 'pitch_rate': math.degrees(pitchspeed),
                 'yaw_rate': math.degrees(yawspeed), 'max': self.max_attitude_rate}, 'T1557')
        self.last_attitude_time = time.time()
    
    def _check_position(self, msg: Dict[str, Any]) -> None:
        lat = msg.get('lat', 0) * 1e-7
        lon = msg.get('lon', 0) * 1e-7
        alt = msg.get('alt', 0) * 1e-3
        vx = msg.get('vx', 0) / 100.0
        vy = msg.get('vy', 0) / 100.0
        vz = msg.get('vz', 0) / 100.0
        current_time = time.time()
        reported_speed = math.sqrt(vx**2 + vy**2 + vz**2)
        if reported_speed > self.max_pos_rate:
            self._alert('impossible_reported_velocity', AlertSeverity.WARNING,
                f"Impossible reported velocity: {reported_speed:.1f} m/s",
                {'reported_speed': reported_speed, 'max': self.max_pos_rate,
                 'vx': vx, 'vy': vy, 'vz': vz}, 'T1557')
        
        if self.last_position is not None:
            dt = current_time - self.last_position_time
            if dt > 0:
                dist = self._haversine(self.last_position[0], self.last_position[1], lat, lon)
                pos_rate = dist / dt
                if pos_rate > self.max_pos_rate:
                    self._alert('impossible_position_rate', AlertSeverity.WARNING,
                        f"Impossible position rate: {pos_rate:.1f} m/s",
                        {'position_rate': pos_rate, 'max': self.max_pos_rate, 'dt': dt}, 'T1557')
                if self.last_velocity is not None:
                    vel_change = math.sqrt((vx - self.last_velocity[0])**2 + 
                                           (vy - self.last_velocity[1])**2 + 
                                           (vz - self.last_velocity[2])**2) / dt
                    if vel_change > self.max_vel_change:
                        self._alert('impossible_velocity_change', AlertSeverity.WARNING,
                            f"Impossible velocity change: {vel_change:.1f} m/s²",
                            {'vel_change': vel_change, 'max': self.max_vel_change}, 'T1557')
        
        self.last_position = (lat, lon, alt)
        self.last_position_time = current_time
        self.last_velocity = (vx, vy, vz)
        self.gps_alt = alt
    
    def _check_vfr_hud(self, msg: Dict[str, Any]) -> None:
        if self.enable_baro_check:
            self.baro_alt = msg.get('alt', 0)
            if self.gps_alt is not None:
                diff = abs(self.gps_alt - self.baro_alt)
                if diff > self.max_alt_diff:
                    self._alert('baro_gps_altitude_mismatch', AlertSeverity.WARNING,
                        f"Baro-GPS altitude mismatch: {diff:.1f}m",
                        {'gps_alt': self.gps_alt, 'baro_alt': self.baro_alt, 'diff': diff}, 'T1557')
        if self.enable_compass_check:
            heading = msg.get('heading', 0)
            if self.last_heading is not None:
                diff = min(abs(heading - self.last_heading), 360 - abs(heading - self.last_heading))
                if diff > self.max_heading_diff:
                    self._alert('compass_heading_mismatch', AlertSeverity.WARNING,
                        f"Compass-GPS heading mismatch: {diff:.1f} deg",
                        {'heading': heading, 'diff': diff}, 'T1557')
            self.last_heading = heading
    
    def _check_power(self, msg: Dict[str, Any]) -> None:
        if not self.monitor_power:
            return
        voltage = msg.get('voltage_battery', 0) / 1000.0
        current = msg.get('current_battery', 0) / 100.0
        
        if self.last_voltage is not None:
            v_diff = abs(voltage - self.last_voltage)
            if v_diff > self.max_volt_jump:
                self._alert('voltage_jump', AlertSeverity.WARNING,
                    f"Voltage jump: {v_diff:.2f}V",
                    {'voltage': voltage, 'prev': self.last_voltage, 'diff': v_diff}, 'T1557')
        if self.last_current is not None:
            c_diff = abs(current - self.last_current)
            if c_diff > self.max_curr_jump:
                self._alert('current_jump', AlertSeverity.WARNING,
                    f"Current jump: {c_diff:.2f}A",
                    {'current': current, 'prev': self.last_current, 'diff': c_diff}, 'T1557')
        
        self.last_voltage = voltage
        self.last_current = current
    
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
            description=description, details=details, confidence=0.8, mitre_attack=mitre)
        self.generate_alert(alert)