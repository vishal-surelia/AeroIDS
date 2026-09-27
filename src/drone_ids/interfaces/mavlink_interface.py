"""
Drone IDS - MAVLink Interface
Handles MAVLink communication with SITL and attack injection.
"""
import time
import logging
import threading
import socket
import json
from typing import Dict, Any, Optional, Callable
from dataclasses import dataclass

try:
    from pymavlink import mavutil
    MAVLINK_AVAILABLE = True
except ImportError:
    MAVLINK_AVAILABLE = False
    mavutil = None

from ..core.message_bus import message_bus, Message, MessageType
from ..core.config import config


@dataclass
class ConnectionStatus:
    connected: bool = False
    last_heartbeat: float = 0
    messages_received: int = 0
    messages_sent: int = 0
    errors: int = 0


class MAVLinkInterface:
    """MAVLink communication interface with dual-port support (SITL + injection)."""
    
    def __init__(self, sitl_connection: str = None, injection_port: int = 14551):
        self.sitl_connection = sitl_connection or config.get('sitl.connection_string', 'udp:127.0.0.1:14550')
        self.injection_port = injection_port
        self.source_system = config.get('sitl.source_system', 255)
        self.source_component = config.get('sitl.source_component', 190)
        
        self.master = None
        self.injection_socket = None
        self.status = ConnectionStatus()
        self._running = False
        self._receive_thread = None
        self._heartbeat_thread = None
        self._injection_thread = None
        self._message_callback = None
        
        self.logger = logging.getLogger('drone_ids.mavlink')
        self.logger.setLevel(logging.INFO)
    
    def connect(self) -> bool:
        if not MAVLINK_AVAILABLE:
            self.logger.error("pymavlink not available. Install with: pip install pymavlink")
            return False
        
        try:
            self.logger.info(f"Connecting to SITL: {self.sitl_connection}")
            self.master = mavutil.mavlink_connection(
                self.sitl_connection,
                source_system=self.source_system,
                source_component=self.source_component,
                autoreconnect=True
            )
            
            self.logger.info("Waiting for SITL heartbeat...")
            self.master.wait_heartbeat(timeout=config.get('sitl.heartbeat_timeout', 5.0))
            
            self.status.connected = True
            self.status.last_heartbeat = time.time()
            self.logger.info(f"Connected to SITL system {self.master.target_system}")
            
            self._start_injection_listener()
            
            self._running = True
            self._receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
            self._receive_thread.start()
            
            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()
            
            return True
            
        except Exception as e:
            self.logger.error(f"Connection failed: {e}")
            self.status.errors += 1
            return False
    
    def _start_injection_listener(self):
        try:
            port = int(self.injection_port)
            self.injection_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.injection_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.injection_socket.bind(('0.0.0.0', self.injection_port))
            self.injection_socket.settimeout(1.0)
            
            self.logger.info(f"Injection listener started on UDP port {self.injection_port}")
            self._running = True
            self._injection_thread = threading.Thread(target=self._injection_loop, daemon=True)
            self._injection_thread.start()
            
        except Exception as e:
            self.logger.error(f"Failed to start injection listener: {e}")
    
    def _injection_loop(self):
        self.logger.info("Injection loop started, waiting for packets on port %d", self.injection_port)
        while self._running and self.injection_socket:
            try:
                data, addr = self.injection_socket.recvfrom(65535)
                self.logger.debug("Received %d bytes from %s", len(data), addr)
                try:
                    msg_dict = json.loads(data.decode('utf-8'))
                    msg_dict['src_sys'] = msg_dict.get('src_sys', 1)
                    msg_dict['src_comp'] = msg_dict.get('src_comp', 1)
                    msg_dict['seq'] = msg_dict.get('seq', 0)
                    
                    self.logger.debug("Publishing message type: %s", msg_dict.get('type', 'UNKNOWN'))
                    message_bus.publish(Message(
                        type=MessageType.MAVLINK_MESSAGE,
                        source="injection",
                        data=msg_dict
                    ))
                    self.logger.debug("Published message type: %s", msg_dict.get('type', 'UNKNOWN'))
                except json.JSONDecodeError as e:
                    self.logger.warning(f"Invalid JSON from {addr}: {e}")
                except Exception as e:
                    self.logger.error(f"Injection error: {e}")
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    self.logger.error(f"Injection loop error: {e}")
    
    def start_injection_listener_only(self) -> bool:
        """Open the attack-injection listener without requiring a SITL heartbeat."""
        if self.injection_socket:
            self._running = True
            return True
        self._start_injection_listener()
        if self.injection_socket:
            self._running = True
            return True
        return False

    def disconnect(self) -> None:
        self._running = False
        
        for thread in [self._receive_thread, self._heartbeat_thread, self._injection_thread]:
            if thread and thread.is_alive():
                thread.join(timeout=2.0)
        
        if self.master:
            self.master.close()
            self.master = None
        
        if self.injection_socket:
            self.injection_socket.close()
            self.injection_socket = None
        
        self.status.connected = False
        self.logger.info("Disconnected")
    
    def set_message_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        self._message_callback = callback
    
    def _receive_loop(self):
        while self._running and self.master:
            try:
                msg = self.master.recv_match(blocking=True, timeout=1.0)
                if msg:
                    self._process_message(msg)
            except Exception as e:
                self.logger.error(f"Receive error: {e}")
                self.status.errors += 1
                time.sleep(0.1)
    
    def _process_message(self, msg):
        self.status.messages_received += 1
        self.status.last_heartbeat = time.time()
        
        msg_dict = msg.to_dict()
        msg_dict['type'] = msg.get_type()
        msg_dict['src_sys'] = msg.get_srcSystem()
        msg_dict['src_comp'] = msg.get_srcComponent()
        msg_dict['seq'] = msg.get_seq()
        
        message_bus.publish(Message(
            type=MessageType.MAVLINK_MESSAGE,
            source="mavlink_interface",
            data=msg_dict
        ))
        
        if self._message_callback:
            try:
                self._message_callback(msg_dict)
            except Exception as e:
                self.logger.error(f"Callback error: {e}")
    
    def _heartbeat_loop(self):
        while self._running and self.master:
            try:
                self.master.mav.heartbeat_send(
                    mavutil.mavlink.MAV_TYPE_ONBOARD_CONTROLLER,
                    mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                    0, 0, 0
                )
                self.status.messages_sent += 1
                time.sleep(1.0)
            except Exception as e:
                self.logger.error(f"Heartbeat error: {e}")
                self.status.errors += 1
                time.sleep(1.0)
    
    def send_command(self, command: int, params: list = None,
                     target_system: int = None, target_component: int = 1,
                     confirmation: int = 0) -> bool:
        if not self.master or not self.status.connected:
            return False
        try:
            params = params or [0]*7
            self.master.mav.command_long_send(
                target_system or self.master.target_system,
                target_component,
                command,
                confirmation,
                *params
            )
            self.status.messages_sent += 1
            return True
        except Exception as e:
            self.logger.error(f"Command send error: {e}")
            return False
    
    def request_data_stream(self, stream_id: int, rate: int = 10) -> bool:
        if not self.master:
            return False
        try:
            self.master.mav.request_data_stream_send(
                self.master.target_system,
                self.master.target_component,
                stream_id,
                rate,
                1
            )
            return True
        except Exception as e:
            self.logger.error(f"Data stream request error: {e}")
            return False
    
    def get_status(self) -> Dict[str, Any]:
        return {
            'connected': self.status.connected,
            'last_heartbeat': self.status.last_heartbeat,
            'messages_received': self.status.messages_received,
            'messages_sent': self.status.messages_sent,
            'errors': self.status.errors,
            'injection_port': self.injection_port
        }
