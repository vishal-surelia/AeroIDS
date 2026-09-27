"""Integration test for detection capabilities."""
import sys
import time
from pathlib import Path

# Add the local src/ directory to path so tests exercise this source tree.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_gps_spoofing_detection():
    """Test GPS spoofing detection with simulated messages."""
    from drone_ids.core.config import config
    from drone_ids.core.ids_engine import engine
    from drone_ids.core.message_bus import message_bus, Message, MessageType
    from drone_ids.detectors.gps_spoofing_detector import GPSSpoofingDetector
    from drone_ids.alerting.alert_manager import AlertManager
    
    config.load()
    
    # Create detector
    gps_detector = GPSSpoofingDetector()
    engine.add_detector(gps_detector)
    
    # Capture alerts
    alerts = []
    def capture_alert(msg):
        alerts.append(msg.data)
    
    message_bus.subscribe(MessageType.ALERT, capture_alert)
    
    # Start engine
    engine.start()
    
    # Send normal GPS messages first
    base_time = time.time()
    for i in range(5):
        msg = Message(
            type=MessageType.MAVLINK_MESSAGE,
            source="test",
            data={
                'type': 'GPS_RAW_INT',
                'lat': int((47.397742 + i * 0.0001) * 1e7),
                'lon': int((8.545594 + i * 0.0001) * 1e7),
                'alt': int((500 + i) * 1000),
                'satellites_visible': 10,
                'eph': 100,
                'epv': 150,
                'fix_type': 3,
                'time_usec': int((base_time + i) * 1e6),
                'src_sys': 1,
                'src_comp': 1,
                'seq': i
            }
        )
        message_bus.publish(msg)
        time.sleep(0.1)
    
    # Send spoofed GPS (large position jump)
    spoofed_msg = Message(
        type=MessageType.MAVLINK_MESSAGE,
        source="test",
        data={
            'type': 'GPS_RAW_INT',
            'lat': int((47.397742 + 0.01) * 1e7),  # ~1km jump
            'lon': int((8.545594 + 0.01) * 1e7),
            'alt': int(500 * 1000),
            'satellites_visible': 10,
            'eph': 100,
            'epv': 150,
            'fix_type': 3,
            'time_usec': int((base_time + 5) * 1e6),
            'src_sys': 1,
            'src_comp': 1,
            'seq': 5
        }
    )
    message_bus.publish(spoofed_msg)
    time.sleep(0.5)
    
    # Check alerts
    gps_alerts = [a for a in alerts if 'gps' in a.get('alert_type', '').lower() or 'position' in a.get('alert_type', '').lower()]
    
    engine.stop()
    
    print(f"Total alerts: {len(alerts)}")
    print(f"GPS alerts: {len(gps_alerts)}")
    for a in gps_alerts:
        print(f"  - {a.get('alert_type')}: {a.get('description')}")
    
    # Should detect position jump
    assert len(gps_alerts) > 0, "Should detect GPS position jump"
    print("✓ GPS spoofing detection test passed")


def test_command_injection_detection():
    """Test command injection detection."""
    from drone_ids.core.config import config
    from drone_ids.core.ids_engine import engine
    from drone_ids.core.message_bus import message_bus, Message, MessageType
    from drone_ids.detectors.command_injection_detector import CommandInjectionDetector
    
    # Create new engine instance for clean test
    from drone_ids.core.ids_engine import IDSEngine
    test_engine = IDSEngine()
    
    cmd_detector = CommandInjectionDetector()
    test_engine.add_detector(cmd_detector)
    
    alerts = []
    def capture_alert(msg):
        alerts.append(msg.data)
    
    message_bus.subscribe(MessageType.ALERT, capture_alert)
    
    test_engine.start()
    
    # Send command from unauthorized source
    cmd_msg = Message(
        type=MessageType.MAVLINK_MESSAGE,
        source="test",
        data={
            'type': 'COMMAND_LONG',
            'command': 400,  # ARM
            'param1': 1,
            'src_sys': 99,  # Unauthorized source
            'src_comp': 1,
            'seq': 1
        }
    )
    message_bus.publish(cmd_msg)
    time.sleep(0.2)
    
    # Check alerts
    cmd_alerts = [a for a in alerts if 'command' in a.get('alert_type', '').lower() or 'unauthorized' in a.get('alert_type', '').lower()]
    
    test_engine.stop()
    
    print(f"Total alerts: {len(alerts)}")
    print(f"Command alerts: {len(cmd_alerts)}")
    for a in cmd_alerts:
        print(f"  - {a.get('alert_type')}: {a.get('description')}")
    
    assert len(cmd_alerts) > 0, "Should detect unauthorized command source"
    print("✓ Command injection detection test passed")


def test_dos_flood_detection():
    """Test that a flood of injected messages triggers the DoS detector."""
    from drone_ids.core.config import config
    from drone_ids.core.message_bus import message_bus, MessageType
    from drone_ids.detectors.dos_detector import DoSDetector

    config.load()
    detector = DoSDetector()
    detector.initialize()

    alerts = []
    message_bus.subscribe(MessageType.ALERT, lambda msg: alerts.append(msg.data))

    for i in range(600):
        detector.on_mavlink_message({
            'type': 'PARAM_REQUEST_READ',
            'src_sys': 1,
            'src_comp': 1,
            'seq': i
        })

    # RADIO_STATUS should be handled without raising a message-variable error.
    detector.on_mavlink_message({
        'type': 'RADIO_STATUS', 'rssi': 72, 'remrssi': 70,
        'src_sys': 1, 'src_comp': 1, 'seq': 600
    })

    flood_alerts = [a for a in alerts if a.get('alert_type') == 'message_flood']
    assert flood_alerts, "Should detect a message flood"


def test_telemetry_manipulation_detection():
    """Test telemetry attacks using MAVLink's rad/s and cm/s units."""
    from drone_ids.core.config import config
    from drone_ids.core.message_bus import message_bus, MessageType
    from drone_ids.detectors.telemetry_manipulation_detector import TelemetryManipulationDetector

    config.load()
    detector = TelemetryManipulationDetector()
    detector.initialize()

    alerts = []
    message_bus.subscribe(MessageType.ALERT, lambda msg: alerts.append(msg.data))

    detector.on_mavlink_message({
        'type': 'ATTITUDE',
        'rollspeed': 8.72,  # Approximately 500 deg/s
        'pitchspeed': 0.0,
        'yawspeed': 0.0,
        'src_sys': 1,
        'src_comp': 1,
        'seq': 1
    })
    detector.on_mavlink_message({
        'type': 'GLOBAL_POSITION_INT',
        'lat': int(47.397742 * 1e7),
        'lon': int(8.545594 * 1e7),
        'alt': 500000,
        'vx': 20000,  # 200 m/s in MAVLink cm/s units
        'vy': 0,
        'vz': 0,
        'src_sys': 1,
        'src_comp': 1,
        'seq': 2
    })

    alert_types = {a.get('alert_type') for a in alerts}
    assert 'impossible_attitude_rate' in alert_types
    assert 'impossible_reported_velocity' in alert_types


def test_firmware_param_integrity_detection():
    """Test configured firmware parameter integrity hash checking."""
    from drone_ids.core.config import config
    from drone_ids.core.message_bus import message_bus, MessageType
    from drone_ids.detectors.firmware_integrity_detector import FirmwareIntegrityDetector

    config.load()
    detector = FirmwareIntegrityDetector()
    detector.initialize()

    alerts = []
    message_bus.subscribe(MessageType.ALERT, lambda msg: alerts.append(msg.data))

    detector.on_mavlink_message({
        'type': 'PARAM_VALUE',
        'param_id': 'SYSID_THISMAV',
        'param_value': 999.0,
        'src_sys': 1,
        'src_comp': 1,
        'seq': 1
    })

    integrity_alerts = [a for a in alerts if a.get('alert_type') == 'param_integrity_violation']
    assert integrity_alerts, "Should detect a critical parameter integrity violation"



if __name__ == '__main__':
    test_gps_spoofing_detection()
    test_command_injection_detection()
    test_dos_flood_detection()
    test_telemetry_manipulation_detection()
    test_firmware_param_integrity_detection()
    print("\n✅ All detection tests passed!")