#!/usr/bin/env python3
"""
Drone IDS - Web Dashboard Entry Point
Starts the IDS engine in a background thread and serves the HTML dashboard.

Usage:
    python run_web.py            # connects to SITL on udp:127.0.0.1:14550
    python run_web.py --debug    # verbose logging

Then open:  http://localhost:8080
            http://<your-ip>:8080   (accessible on LAN)
"""
import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure src/ is on the import path (same pattern as run_ids.py)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

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
from drone_ids.web.server import app, hub, WebSocketLogHandler, setup_hub

import uvicorn


# ---------------------------------------------------------------------------
# Logging setup — adds WebSocket handler so console tab gets live output
# ---------------------------------------------------------------------------

def setup_logging(ws_hub: WebSocketLogHandler, debug: bool = False) -> None:
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)

    fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    ws_handler = WebSocketLogHandler(ws_hub)
    ws_handler.setFormatter(fmt)

    file_handler = logging.FileHandler(log_dir / "drone_ids.log")
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG if debug else logging.INFO)
    root_logger.handlers.clear()
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)
    root_logger.addHandler(ws_handler)

    # Quiet down uvicorn's access log (it's noisy for WS pings)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# IDS engine thread
# ---------------------------------------------------------------------------

def run_engine_thread(stop_event: threading.Event) -> None:
    logger = logging.getLogger("drone_ids.web_runner")

    try:
        config.load()
    except Exception as e:
        logger.error(f"Failed to load config: {e}")

    alert_manager = AlertManager()

    # Register all six detectors
    detectors = [
        GPSSpoofingDetector(),
        MAVLinkAnomalyDetector(),
        CommandInjectionDetector(),
        TelemetryManipulationDetector(),
        DoSDetector(),
        FirmwareIntegrityDetector(),
    ]
    for d in detectors:
        engine.add_detector(d)
        logger.info(f"Registered detector: {d.name}")

    alert_manager.start()
    logger.info("Alert manager started")

    # Connect to MAVLink / SITL
    logger.info("Connecting to SITL on udp:127.0.0.1:14550 …")
    mavlink = MAVLinkInterface()

    mavlink_connected = mavlink.connect()
    if not mavlink_connected:
        logger.warning(
            "Could not connect to SITL — the dashboard and demonstration simulator "
            "will remain active. Start SITL with: "
            "sim_vehicle.py -v copter -f quad -I0 --console --map"
        )
        if mavlink.start_injection_listener_only():
            logger.info(f"Attack injection listener active on UDP {mavlink.injection_port}")
    else:
        # Request standard data streams
        for stream_id, rate in [(0, 10), (5, 10), (2, 5)]:
            if mavlink.request_data_stream(stream_id, rate):
                logger.info(f"Requested MAVLink stream {stream_id} @ {rate} Hz")

    # Keep detectors active for both SITL traffic and the built-in demo simulator.
    engine.start()
    logger.info("IDS Engine started — monitoring active")

    stop_event.wait()  # Block until main thread signals shutdown

    logger.info("Stopping IDS engine …")
    engine.stop()
    alert_manager.stop()
    if mavlink_connected:
        mavlink.disconnect()
    logger.info("Engine thread shut down")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Drone IDS Web Dashboard")
    parser.add_argument("--host",  default="0.0.0.0",  help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port",  default=8080, type=int, help="Port (default: 8080)")
    parser.add_argument("--debug", action="store_true",   help="Enable DEBUG logging")
    args = parser.parse_args()

    # Hook up the WebSocket hub to the message bus BEFORE engine starts
    setup_hub()
    setup_logging(hub, debug=args.debug)

    logger = logging.getLogger("drone_ids.web_runner")

    # Signal handling for graceful shutdown
    stop_event = threading.Event()

    def _signal_handler(sig, _frame):
        logger.info(f"Signal {sig} received — shutting down …")
        stop_event.set()

    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Start IDS engine in a daemon background thread
    engine_thread = threading.Thread(
        target=run_engine_thread,
        args=(stop_event,),
        name="ids-engine",
        daemon=True,
    )
    engine_thread.start()

    # Banner
    host_display = "localhost" if args.host == "0.0.0.0" else args.host
    print()
    print("╔══════════════════════════════════════════╗")
    print("║        Drone IDS Mission Console         ║")
    print(f"║   http://{host_display}:{args.port}                  ║")
    print("║   Open in browser — Ctrl+C to stop      ║")
    print("╚══════════════════════════════════════════╝")
    print()

    # Run FastAPI on the main thread (uvicorn sets up the asyncio loop,
    # which the WebSocketHub needs for run_coroutine_threadsafe)
    try:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            log_level="warning",
        )
    finally:
        stop_event.set()
        engine_thread.join(timeout=6.0)
        logger.info("Goodbye.")


if __name__ == "__main__":
    main()
