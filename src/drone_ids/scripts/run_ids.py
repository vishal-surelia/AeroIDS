#!/usr/bin/env python3
"""
Drone IDS - Main Entry Point for SITL
Connects to running SITL on UDP 14550. Runs until stopped with Ctrl+C.
"""
import argparse
import logging
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from drone_ids.core.config import config
from drone_ids.core.ids_engine import engine
from drone_ids.detectors.gps_spoofing_detector import GPSSpoofingDetector
from drone_ids.detectors.mavlink_anomaly_detector import MAVLinkAnomalyDetector
from drone_ids.detectors.command_injection_detector import CommandInjectionDetector
from drone_ids.detectors.telemetry_manipulation_detector import TelemetryManipulationDetector
from drone_ids.detectors.dos_detector import DoSDetector
from drone_ids.detectors.firmware_integrity_detector import FirmwareIntegrityDetector
from drone_ids.interfaces.mavlink_interface import MAVLinkInterface
from drone_ids.alerting.alert_manager import AlertManager


class DroneIDSApplication:
    def __init__(self):
        self.running = False
        self.mavlink = None
        self.alert_manager = AlertManager()
        self._setup_logging()
        self.logger = logging.getLogger('drone_ids.main')
    
    def _setup_logging(self) -> None:
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[logging.FileHandler(log_dir / "drone_ids.log"), logging.StreamHandler()]
        )
    
    def initialize(self) -> bool:
        self.logger.info("Initializing Drone IDS...")
        
        config.load()
        
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        self._init_detectors()
        self.alert_manager.start()
        
        return self._init_mavlink()
    
    def _init_detectors(self) -> None:
        detectors = [
            GPSSpoofingDetector(), MAVLinkAnomalyDetector(),
            CommandInjectionDetector(), TelemetryManipulationDetector(),
            DoSDetector(), FirmwareIntegrityDetector()
        ]
        for d in detectors:
            engine.add_detector(d)
            self.logger.info(f"Registered: {d.name}")
    
    def _init_mavlink(self) -> bool:
        self.logger.info("Connecting to SITL on UDP 14550...")
        self.mavlink = MAVLinkInterface()
        if not self.mavlink.connect():
            self.logger.error("Failed to connect to SITL. Ensure SITL is running on udp:127.0.0.1:14550")
            return False
        
        self._request_data_streams()
        return True
    
    def _request_data_streams(self) -> None:
        for stream_id, rate in [(0, 10), (5, 10), (2, 5)]:
            if self.mavlink.request_data_stream(stream_id, rate):
                self.logger.info(f"Requested stream {stream_id} at {rate}Hz")
    
    def run(self) -> None:
        self.logger.info("Starting Drone IDS...")
        self.running = True
        engine.start()
        
        try:
            self.logger.info("Running indefinitely (Ctrl+C to stop)...")
            while self.running:
                time.sleep(1.0)
                if int(time.time()) % 30 == 0:
                    self.logger.info(f"Status: {engine.get_status()}")
        except KeyboardInterrupt:
            self.logger.info("Interrupted by user")
        finally:
            self.shutdown()
    
    def shutdown(self) -> None:
        self.logger.info("Shutting down...")
        self.running = False
        engine.stop()
        self.alert_manager.stop()
        if self.mavlink:
            self.mavlink.disconnect()
        self.logger.info("Shutdown complete")
    
    def _signal_handler(self, signum, frame) -> None:
        self.logger.info(f"Signal {signum}, shutting down...")
        self.running = False


def main():
    parser = argparse.ArgumentParser(description="Drone IDS for SITL")
    parser.add_argument('--debug', action='store_true', help='Debug logging')
    args = parser.parse_args()
    
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
    
    app = DroneIDSApplication()
    if not app.initialize():
        print("Failed to initialize - ensure SITL is running on udp:127.0.0.1:14550")
        sys.exit(1)
    app.run()


if __name__ == '__main__':
    main()