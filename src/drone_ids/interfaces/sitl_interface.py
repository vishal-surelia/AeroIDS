"""
Drone IDS - SITL Interface
Manages ArduPilot SITL simulation for testing.
"""
import os
import subprocess
import time
import logging
import signal
from typing import Optional, List
from pathlib import Path

from .mavlink_interface import MAVLinkInterface
from ..core.config import config


class SITLManager:
    """Manages ArduPilot SITL instance."""
    
    def __init__(self, vehicle: str = "copter", instance: int = 0):
        self.vehicle = vehicle
        self.instance = instance
        self.sitl_process: Optional[subprocess.Popen] = None
        self.mavlink = MAVLinkInterface()
        self.logger = logging.getLogger('drone_ids.sitl')
        self.logger.setLevel(logging.INFO)
        
        # Default SITL parameters
        self.sitl_path = os.environ.get('ARDUPILOT_SITL', 'sim_vehicle.py')
        self.model = "quad"
        self.speedup = 1
        self.home = "-35.363261,149.165230,584,353"  # Canberra default
    
    def start(self, extra_args: List[str] = None) -> bool:
        """Start SITL instance."""
        if self.is_running():
            self.logger.warning("SITL already running")
            return True
        
        # Check if sim_vehicle.py exists and is executable
        if self.sitl_path == 'sim_vehicle.py':
            # Try to find it in common locations
            possible_paths = [
                'sim_vehicle.py',
                os.path.expanduser('~/ardupilot/Tools/autotest/sim_vehicle.py'),
                os.path.expanduser('~/ardupilot/sim_vehicle.py'),
                '/usr/local/bin/sim_vehicle.py',
            ]
            for p in possible_paths:
                if os.path.exists(p):
                    self.sitl_path = p
                    break
        
        # Try different argument formats for sim_vehicle.py
        # Newer versions use -L for location, older use --home
        args = [
            self.sitl_path,
            '-v', self.vehicle,
            '-f', self.model,
            '-I', str(self.instance),
            '--speedup', str(self.speedup),
            '-L', self.home,  # Use -L instead of --home
            '--no-rebuild',
            '--console',
            '--map'
        ]
        
        if extra_args:
            args.extend(extra_args)
        
        self.logger.info(f"Starting SITL: {' '.join(args)}")
        
        try:
            self.sitl_process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                preexec_fn=os.setsid
            )
            
            # Wait for SITL to be ready
            time.sleep(15)  # Longer wait for first startup
            
            if self.sitl_process.poll() is not None:
                stdout, _ = self.sitl_process.communicate()
                self.logger.error(f"SITL failed to start: {stdout}")
                
                # Provide helpful error message
                if "geocoder not installed" in stdout:
                    self.logger.error("ArduPilot SITL missing 'geocoder' module.")
                    self.logger.error("Install with: pip install geocoder")
                    self.logger.error("Or run the ArduPilot environment setup script.")
                elif "command not found" in stdout or "No such file" in stdout:
                    self.logger.error("sim_vehicle.py not found.")
                    self.logger.error("Install ArduPilot or set ARDUPILOT_SITL environment variable.")
                
                return False
            
            self.logger.info("SITL started successfully")
            return True
            
        except FileNotFoundError:
            self.logger.error(f"SITL executable not found: {self.sitl_path}")
            self.logger.error("Set ARDUPILOT_SITL environment variable or install ArduPilot")
            return False
        except Exception as e:
            self.logger.error(f"Failed to start SITL: {e}")
            return False
    
    def stop(self) -> None:
        """Stop SITL instance."""
        if self.sitl_process:
            self.logger.info("Stopping SITL...")
            try:
                os.killpg(os.getpgid(self.sitl_process.pid), signal.SIGTERM)
                self.sitl_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(self.sitl_process.pid), signal.SIGKILL)
            except Exception as e:
                self.logger.error(f"Error stopping SITL: {e}")
            finally:
                self.sitl_process = None
                self.logger.info("SITL stopped")
    
    def is_running(self) -> bool:
        return self.sitl_process is not None and self.sitl_process.poll() is None
    
    def connect_ids(self) -> bool:
        """Connect IDS to SITL."""
        # SITL default MAVLink endpoint is UDP port 14550 + 10*instance
        port = 14550 + 10 * self.instance
        conn_string = f"udp:127.0.0.1:{port}"
        
        self.mavlink = MAVLinkInterface(conn_string)
        return self.mavlink.connect()
    
    def disconnect_ids(self) -> None:
        """Disconnect IDS from SITL."""
        self.mavlink.disconnect()
    
    def get_connection_string(self) -> str:
        port = 14550 + 10 * self.instance
        return f"udp:127.0.0.1:{port}"


class SITLInterface:
    """High-level SITL interface for IDS testing."""
    
    def __init__(self):
        self.manager = SITLManager()
        self.logger = logging.getLogger('drone_ids.sitl_interface')
    
    def setup_test_environment(self) -> bool:
        """Set up complete test environment."""
        self.logger.info("Setting up SITL test environment...")
        
        if not self.manager.start():
            return False
        
        if not self.manager.connect_ids():
            self.manager.stop()
            return False
        
        self.logger.info("Test environment ready")
        return True
    
    def teardown_test_environment(self) -> None:
        """Tear down test environment."""
        self.manager.disconnect_ids()
        self.manager.stop()
    
    def get_mavlink_interface(self) -> MAVLinkInterface:
        return self.manager.mavlink
    
    def run_mission(self, mission_file: str = None) -> bool:
        """Load and run a mission."""
        # Implementation would load mission via MAVLink
        self.logger.info("Mission execution not implemented in demo")
        return True
    
    def simulate_gps_spoofing(self, offset_lat: float = 0.001, offset_lon: float = 0.001) -> bool:
        """Simulate GPS spoofing attack for testing."""
        self.logger.warning(f"SIMULATING GPS SPOOFING: offset=({offset_lat}, {offset_lon})")
        # In real implementation, this would inject false GPS data
        return True
    
    def simulate_command_injection(self, command: int, params: list) -> bool:
        """Simulate command injection attack."""
        self.logger.warning(f"SIMULATING COMMAND INJECTION: cmd={command}, params={params}")
        return self.manager.mavlink.send_command(command, params)
    
    def simulate_dos(self, duration: float = 5.0) -> bool:
        """Simulate DoS attack by flooding messages."""
        self.logger.warning(f"SIMULATING DoS for {duration}s")
        # Would flood the link with messages
        return True