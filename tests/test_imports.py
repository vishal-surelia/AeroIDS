"""Test imports for Drone IDS."""
import sys
from pathlib import Path

# Add the local src/ directory to path so drone_ids is imported from this source tree.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_core_imports():
    from drone_ids.core.config import config
    from drone_ids.core.message_bus import message_bus, Message, MessageType
    from drone_ids.core.ids_engine import engine, IDSEngine, EngineState
    print("✓ Core imports successful")


def test_detector_imports():
    from drone_ids.detectors.gps_spoofing_detector import GPSSpoofingDetector
    from drone_ids.detectors.mavlink_anomaly_detector import MAVLinkAnomalyDetector
    from drone_ids.detectors.command_injection_detector import CommandInjectionDetector
    from drone_ids.detectors.telemetry_manipulation_detector import TelemetryManipulationDetector
    from drone_ids.detectors.dos_detector import DoSDetector
    from drone_ids.detectors.firmware_integrity_detector import FirmwareIntegrityDetector
    print("✓ Detector imports successful")


def test_interface_imports():
    from drone_ids.interfaces.mavlink_interface import MAVLinkInterface
    from drone_ids.interfaces.sitl_interface import SITLInterface, SITLManager
    print("✓ Interface imports successful")


def test_alerting_imports():
    from drone_ids.alerting.alert_manager import AlertManager, ConsoleFormatter, FileFormatter
    print("✓ Alerting imports successful")


def test_config_loading():
    from drone_ids.core.config import config
    config.load()
    assert config.get('sitl.connection_string') is not None
    assert config.get('engine.processing_interval') == 0.1
    print("✓ Config loading successful")


def test_detector_instantiation():
    from drone_ids.detectors.gps_spoofing_detector import GPSSpoofingDetector
    from drone_ids.detectors.mavlink_anomaly_detector import MAVLinkAnomalyDetector
    from drone_ids.detectors.command_injection_detector import CommandInjectionDetector
    from drone_ids.detectors.telemetry_manipulation_detector import TelemetryManipulationDetector
    from drone_ids.detectors.dos_detector import DoSDetector
    from drone_ids.detectors.firmware_integrity_detector import FirmwareIntegrityDetector
    
    detectors = [
        GPSSpoofingDetector(),
        MAVLinkAnomalyDetector(),
        CommandInjectionDetector(),
        TelemetryManipulationDetector(),
        DoSDetector(),
        FirmwareIntegrityDetector()
    ]
    
    for d in detectors:
        d.initialize()
        d.shutdown()
    print("✓ Detector instantiation successful")


def test_engine_integration():
    from drone_ids.core.ids_engine import engine
    from drone_ids.detectors.gps_spoofing_detector import GPSSpoofingDetector
    
    # Add a detector
    gps_detector = GPSSpoofingDetector()
    engine.add_detector(gps_detector)
    
    # Start and stop engine
    engine.start()
    import time
    time.sleep(0.5)
    status = engine.get_status()
    assert status['state'] == 'running'
    assert status['detectors'] >= 1
    assert 'rate' in status['stats']
    assert status['stats']['rate'] >= 0
    engine.stop()
    print("✓ Engine integration successful")


if __name__ == '__main__':
    test_core_imports()
    test_detector_imports()
    test_interface_imports()
    test_alerting_imports()
    test_config_loading()
    test_detector_instantiation()
    test_engine_integration()
    print("\n✅ All tests passed!")