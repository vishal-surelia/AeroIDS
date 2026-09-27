"""
Drone IDS - FastAPI Web Server
Serves the HTML dashboard and streams real-time data via WebSocket.

Endpoints:
  GET  /              → index.html
  GET  /api/status    → engine status (JSON)
  GET  /api/alerts    → recent alerts (JSON)
  WS   /ws            → real-time push (alerts, logs, status)
"""
import asyncio
import json
import logging
import math
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse


# ---------------------------------------------------------------------------
# WebSocket Hub
# ---------------------------------------------------------------------------

class WebSocketHub:
    """
    Thread-safe hub that broadcasts JSON messages to all connected WebSocket
    clients.  background threads call broadcast_sync(); the hub schedules the
    actual send on the asyncio event loop.
    """

    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # In-memory stores for initial-state delivery to new clients
        self._recent_alerts: deque = deque(maxlen=500)
        self._recent_logs: deque = deque()
        self._telemetry: Dict[str, Any] = {
            "connected": False,
            "message_count": 0,
            "message_types": {},
        }
        self._track: deque = deque(maxlen=250)
        self._last_telemetry_broadcast = 0.0

    def clear_track(self) -> None:
        """Remove the previous mission's rendered route before a new mission."""
        with self._lock:
            self._track.clear()

    # ------------------------------------------------------------------
    # Loop registration (called from FastAPI startup event)
    # ------------------------------------------------------------------

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ------------------------------------------------------------------
    # Client management
    # ------------------------------------------------------------------

    def add_client(self, ws: WebSocket) -> None:
        with self._lock:
            self._clients.add(ws)

    def remove_client(self, ws: WebSocket) -> None:
        with self._lock:
            self._clients.discard(ws)

    # ------------------------------------------------------------------
    # Broadcasting
    # ------------------------------------------------------------------

    def broadcast_sync(self, data: Dict[str, Any]) -> None:
        """Called from background threads – schedules send on the event loop."""
        if self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._broadcast(data), self._loop)

    async def _broadcast(self, data: Dict[str, Any]) -> None:
        payload = json.dumps(data, default=str)
        with self._lock:
            clients = list(self._clients)

        dead: list = []
        for ws in clients:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)

        if dead:
            with self._lock:
                for ws in dead:
                    self._clients.discard(ws)

    # ------------------------------------------------------------------
    # Initial state delivery
    # ------------------------------------------------------------------

    async def send_initial_state(self, ws: WebSocket) -> None:
        """Push current state, history, and telemetry to a newly connected client."""
        try:
            from ..core.ids_engine import engine
            await ws.send_text(json.dumps({
                "type": "status",
                "data": engine.get_status(),
            }, default=str))
            await ws.send_text(json.dumps({
                "type": "telemetry",
                "data": self.telemetry_payload(),
            }, default=str))
        except Exception:
            return

        for alert in self._recent_alerts:
            try:
                await ws.send_text(json.dumps({"type": "alert", "data": alert}, default=str))
            except Exception:
                return

        for log in self._recent_logs:
            try:
                await ws.send_text(json.dumps({"type": "log", "data": log}, default=str))
            except Exception:
                return

    # ------------------------------------------------------------------
    # message_bus callbacks (called from detector / engine threads)
    # ------------------------------------------------------------------

    def on_alert(self, message: Any) -> None:
        alert_data = message.data
        self._recent_alerts.append(alert_data)
        self.broadcast_sync({"type": "alert", "data": alert_data})

    def on_status(self, message: Any) -> None:
        self.broadcast_sync({"type": "status", "data": message.data})

    def on_mavlink_message(self, message: Any) -> None:
        """Maintain a lightweight live telemetry snapshot for the dashboard."""
        msg = message.data or {}
        now = time.time()

        with self._lock:
            telemetry = self._telemetry
            telemetry["connected"] = True
            telemetry["source"] = message.source
            telemetry["last_message_time"] = now
            telemetry["message_count"] = int(telemetry.get("message_count", 0)) + 1
            telemetry["src_sys"] = msg.get("src_sys")
            telemetry["src_comp"] = msg.get("src_comp")
            telemetry["seq"] = msg.get("seq")

            msg_type = msg.get("type", "UNKNOWN")
            counts = telemetry.setdefault("message_types", {})
            counts[msg_type] = int(counts.get(msg_type, 0)) + 1
            telemetry["last_message_type"] = msg_type
            telemetry["link_online"] = bool(msg.get("link_online", True))

            if msg.get("display_update", True):
                self._update_telemetry(msg)
            track = list(self._track)
            snapshot = dict(telemetry)

        # DoS floods can produce hundreds of messages per second; throttle UI updates.
        if now - self._last_telemetry_broadcast >= 0.5:
            self._last_telemetry_broadcast = now
            self.broadcast_sync({
                "type": "telemetry",
                "data": {"state": snapshot, "track": track},
            })

    def _update_telemetry(self, msg: Dict[str, Any]) -> None:
        """Extract common MAVLink fields into normalized dashboard units."""
        telemetry = self._telemetry
        msg_type = msg.get("type")

        if msg_type == "GPS_RAW_INT":
            lat = msg.get("lat", 0) / 1e7
            lon = msg.get("lon", 0) / 1e7
            telemetry.update({
                "gps_lat": lat,
                "gps_lon": lon,
                "gps_alt_m": msg.get("alt", 0) / 1000.0,
                "fix_type": msg.get("fix_type"),
                "satellites": msg.get("satellites_visible"),
                "hdop": (msg.get("eph") or 0) / 100.0,
                "vdop": (msg.get("epv") or 0) / 100.0,
                "gps_speed_mps": (msg.get("vel") or 0) / 100.0,
                "gps_course_deg": (msg.get("cog") or 0) / 100.0,
            })
            if abs(lat) > 0.000001 or abs(lon) > 0.000001:
                self._track.append({
                    "lat": lat,
                    "lon": lon,
                    "alt": msg.get("alt", 0) / 1000.0,
                    "time": time.time(),
                })

        elif msg_type == "GLOBAL_POSITION_INT":
            vx = msg.get("vx", 0) / 100.0
            vy = msg.get("vy", 0) / 100.0
            vz = msg.get("vz", 0) / 100.0
            telemetry.update({
                "fused_lat": msg.get("lat", 0) / 1e7,
                "fused_lon": msg.get("lon", 0) / 1e7,
                "fused_alt_m": msg.get("alt", 0) / 1000.0,
                "relative_alt_m": msg.get("relative_alt", 0) / 1000.0,
                "vx_mps": vx,
                "vy_mps": vy,
                "vz_mps": vz,
                "ground_speed_mps": math.sqrt(vx * vx + vy * vy),
                "heading_deg": msg.get("hdg", 0) / 100.0,
            })

        elif msg_type == "ATTITUDE":
            telemetry.update({
                "roll_deg": math.degrees(msg.get("roll", 0)),
                "pitch_deg": math.degrees(msg.get("pitch", 0)),
                "yaw_deg": math.degrees(msg.get("yaw", 0)),
                "roll_rate_dps": math.degrees(msg.get("rollspeed", 0)),
                "pitch_rate_dps": math.degrees(msg.get("pitchspeed", 0)),
                "yaw_rate_dps": math.degrees(msg.get("yawspeed", 0)),
            })

        elif msg_type == "SYS_STATUS":
            telemetry.update({
                "battery_v": msg.get("voltage_battery", 0) / 1000.0,
                "battery_a": msg.get("current_battery", 0) / 100.0,
                "battery_remaining_pct": msg.get("battery_remaining"),
                "load_pct": (msg.get("load") or 0) / 10.0,
                "comm_drop_rate_pct": (msg.get("drop_rate_comm") or 0) / 100.0,
                "errors_comm": msg.get("errors_comm"),
            })

        elif msg_type == "VFR_HUD":
            telemetry.update({
                "airspeed_mps": msg.get("airspeed"),
                "groundspeed_mps": msg.get("groundspeed"),
                "hud_alt_m": msg.get("alt"),
                "climb_mps": msg.get("climb"),
                "throttle_pct": msg.get("throttle"),
                "heading_deg": msg.get("heading"),
            })

        elif msg_type == "RADIO_STATUS":
            telemetry.update({
                "rssi": msg.get("rssi"),
                "remote_rssi": msg.get("remrssi"),
                "noise": msg.get("noise"),
                "remote_noise": msg.get("remnoise"),
                "rx_errors": msg.get("rxerrors"),
                "fixed_errors": msg.get("fixed"),
            })

        elif msg_type == "HEARTBEAT":
            telemetry.update({
                "custom_mode": msg.get("custom_mode"),
                "base_mode": msg.get("base_mode"),
                "system_status": msg.get("system_status"),
                "mav_type": msg.get("type"),
                "autopilot": msg.get("autopilot"),
                "heartbeat_time": time.time(),
            })

        elif msg_type in ("EKF_STATUS_REPORT", "ESTIMATOR_STATUS"):
            telemetry.update({
                "velocity_variance": msg.get("velocity_variance"),
                "pos_horiz_variance": msg.get("pos_horiz_variance"),
                "pos_vert_variance": msg.get("pos_vert_variance"),
                "compass_variance": msg.get("compass_variance"),
                "pos_horiz_ratio": msg.get("pos_horiz_ratio"),
                "pos_vert_ratio": msg.get("pos_vert_ratio"),
            })

    def telemetry_payload(self) -> Dict[str, Any]:
        with self._lock:
            return {"state": dict(self._telemetry), "track": list(self._track)}

# ---------------------------------------------------------------------------
# Logging handler
# ---------------------------------------------------------------------------

class WebSocketLogHandler(logging.Handler):
    """Captures Python log records and forwards them to the dashboard console."""

    def __init__(self, hub: WebSocketHub) -> None:
        super().__init__()
        self.hub = hub

    def emit(self, record: logging.LogRecord) -> None:
        try:
            log_entry = {
                "timestamp": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
            }
            self.hub._recent_logs.append(log_entry)
            self.hub.broadcast_sync({"type": "log", "data": log_entry})
        except Exception:
            pass  # Never let logging errors crash the IDS


# ---------------------------------------------------------------------------
# Detector capability model + global hub + FastAPI app
# ---------------------------------------------------------------------------

_DETECTOR_PROFILES = [
    {
        "id": "GPSSpoofingDetector",
        "name": "GPS Spoofing",
        "test_id": "TC-01",
        "signals": "Position jump, IMU/EKF divergence, satellite quality",
        "mitre": "T1557",
    },
    {
        "id": "MAVLinkAnomalyDetector",
        "name": "MAVLink Anomaly",
        "test_id": "TC-02",
        "signals": "Sequence gaps, rate baseline, protocol timing",
        "mitre": "T1499 / T1557",
    },
    {
        "id": "CommandInjectionDetector",
        "name": "Command Injection",
        "test_id": "TC-03",
        "signals": "Unauthorized source, critical commands, missing ACK",
        "mitre": "T1021 / T1562",
    },
    {
        "id": "DoSDetector",
        "name": "Denial of Service",
        "test_id": "TC-04",
        "signals": "Packet flood, heartbeat health, RSSI/link degradation",
        "mitre": "T1499",
    },
    {
        "id": "TelemetryManipulationDetector",
        "name": "Telemetry Manipulation",
        "test_id": "TC-05",
        "signals": "Impossible kinematics, power jumps, sensor disagreement",
        "mitre": "T1557",
    },
    {
        "id": "FirmwareIntegrityDetector",
        "name": "Firmware Integrity",
        "test_id": "TC-06",
        "signals": "Boot/reference hash and critical parameter integrity",
        "mitre": "T1542 / T1562",
    },
]

hub = WebSocketHub()

app = FastAPI(title="ANALOG ZERO Drone IDS Mission Console", version="1.0.0")

_STATIC_DIR = Path(__file__).parent / "static"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_LOG_DIR = _PROJECT_ROOT / "logs"
_ALLOWED_LOGS = {
    "alerts": _LOG_DIR / "alerts.log",
    "evidence": _LOG_DIR / "evidence.log",
    "chain": _LOG_DIR / "chain_of_custody.log",
}


def _read_json_lines(path: Path) -> list:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _verify_evidence_records(records: list) -> int:
    import hashlib
    verified = 0
    for record in records:
        expected = record.get("evidence_hash")
        if not expected:
            continue
        candidate = dict(record)
        candidate["evidence_hash"] = ""
        digest = hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest()
        if digest == expected:
            verified += 1
    return verified


# Demonstration-only mission telemetry simulator. It publishes to the same bus
# used by MAVLinkInterface, but never sends commands to a real vehicle.
_MISSION_WAYPOINTS = [
    {"lat": 28.61390, "lon": 77.20900, "alt": 120.0},
    {"lat": 28.61600, "lon": 77.21350, "alt": 125.0},
    {"lat": 28.61420, "lon": 77.21800, "alt": 130.0},
    {"lat": 28.61050, "lon": 77.21400, "alt": 135.0},
]
_simulation_lock = threading.RLock()
_simulation_stop = threading.Event()
_simulation_thread: Optional[threading.Thread] = None
_simulation_state: Dict[str, Any] = {
    "running": False,
    "mode": "SIMULATION",
    "waypoints": _MISSION_WAYPOINTS,
    "message_count": 0,
    "rtl_active": False,
    "link_online": True,
    "stop_after_rtl": False,
    "home_reached": False,
}


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    value = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def _mission_route_position(distance_m: float) -> Dict[str, float]:
    legs = []
    total = 0.0
    for index, start in enumerate(_MISSION_WAYPOINTS):
        end = _MISSION_WAYPOINTS[(index + 1) % len(_MISSION_WAYPOINTS)]
        length = _haversine_m(start["lat"], start["lon"], end["lat"], end["lon"])
        legs.append((start, end, length))
        total += length
    distance_m = distance_m % total
    for start, end, length in legs:
        if distance_m <= length:
            ratio = distance_m / length
            lat = start["lat"] + (end["lat"] - start["lat"]) * ratio
            lon = start["lon"] + (end["lon"] - start["lon"]) * ratio
            alt = start["alt"] + (end["alt"] - start["alt"]) * ratio
            dlat_m = (end["lat"] - start["lat"]) * 111320.0
            dlon_m = (end["lon"] - start["lon"]) * 111320.0 * math.cos(math.radians(start["lat"]))
            heading = (math.degrees(math.atan2(dlon_m, dlat_m)) + 360.0) % 360.0
            return {"lat": lat, "lon": lon, "alt": alt, "heading": heading, "vx": 8.0 * math.sin(math.radians(heading)), "vy": 8.0 * math.cos(math.radians(heading))}
        distance_m -= length
    start = _MISSION_WAYPOINTS[0]
    return {"lat": start["lat"], "lon": start["lon"], "alt": start["alt"], "heading": 0.0, "vx": 0.0, "vy": 0.0}


def _rtl_position_step(current: Dict[str, float], dt: float) -> Dict[str, float]:
    """Move the simulated vehicle directly toward home when the C2 link is lost."""
    home = _MISSION_WAYPOINTS[0]
    distance = _haversine_m(current["lat"], current["lon"], home["lat"], home["lon"])
    if distance < 2.0:
        return {
            "lat": home["lat"], "lon": home["lon"], "alt": home["alt"],
            "heading": current.get("heading", 0.0), "vx": 0.0, "vy": 0.0,
        }
    step = min(distance, 12.0 * dt)
    ratio = step / distance
    lat = current["lat"] + (home["lat"] - current["lat"]) * ratio
    lon = current["lon"] + (home["lon"] - current["lon"]) * ratio
    dlat_m = (home["lat"] - current["lat"]) * 111320.0
    dlon_m = (home["lon"] - current["lon"]) * 111320.0 * math.cos(math.radians(current["lat"]))
    heading = (math.degrees(math.atan2(dlon_m, dlat_m)) + 360.0) % 360.0
    alt = max(home["alt"], current.get("alt", home["alt"]) - 2.0 * dt)
    return {
        "lat": lat, "lon": lon, "alt": alt, "heading": heading,
        "vx": 12.0 * math.sin(math.radians(heading)),
        "vy": 12.0 * math.cos(math.radians(heading)),
    }


def _publish_simulation_message(payload: Dict[str, Any], sequence: int) -> None:
    from ..core.message_bus import Message, MessageType, message_bus
    with _simulation_lock:
        link_online = bool(_simulation_state.get("link_online", True))
    payload.update({
        "time_usec": int(time.time() * 1e6),
        "src_sys": 1,
        "src_comp": 1,
        "seq": sequence % 256,
        "link_online": link_online,
    })
    message_bus.publish(Message(
        type=MessageType.MAVLINK_MESSAGE,
        source="analog_zero_mission_simulator",
        data=payload,
    ))


def _mission_simulation_loop(stop_event: threading.Event) -> None:
    """Generate a smooth, physically plausible waypoint loop for demonstrations."""
    from ..core.message_bus import Message, MessageType, message_bus
    start_wall = time.time()
    start_monotonic = time.monotonic()
    last_tick = start_monotonic
    sequence = 0
    route_distance = 0.0
    current_position = _mission_route_position(0.0)
    home_reached = False

    logger = logging.getLogger("drone_ids.mission_simulator")
    logger.info("Firmware integrity check complete — reference accepted")
    logger.info("Mode changed: GUIDED")
    logger.warning("Drone ARMED")
    logger.info("Mission started: waypoint loop active")

    while not stop_event.is_set():
        now = time.monotonic()
        dt = max(0.001, now - last_tick)
        last_tick = now
        with _simulation_lock:
            rtl_active = bool(_simulation_state.get("rtl_active", False))
            link_online = bool(_simulation_state.get("link_online", True))
        if rtl_active:
            position = _rtl_position_step(current_position, dt)
        else:
            route_distance += 8.0 * dt
            position = _mission_route_position(route_distance)
        current_position = dict(position)
        if rtl_active and _haversine_m(position["lat"], position["lon"], _MISSION_WAYPOINTS[0]["lat"], _MISSION_WAYPOINTS[0]["lon"]) < 2.0:
            home_reached = True
        elapsed = now - start_monotonic
        battery_v = max(15.2, 16.8 - elapsed * 0.002)
        battery_pct = max(5, int(100 - elapsed * 0.5))
        roll = math.radians(3.0 * math.sin(elapsed * 0.7))
        pitch = math.radians(2.0 * math.cos(elapsed * 0.6))
        yaw = math.radians(position["heading"])

        common = {
            "lat": int(position["lat"] * 1e7),
            "lon": int(position["lon"] * 1e7),
            "alt": int((220.0 + position["alt"]) * 1000),
            "link_online": link_online,
        }
        messages = [
            {
                "type": "GPS_RAW_INT",
                **common,
                "eph": 95,
                "epv": 140,
                "vel": 800,
                "cog": int(position["heading"] * 100),
                "satellites_visible": 14,
                "fix_type": 3,
            },
            {
                "type": "GLOBAL_POSITION_INT",
                **common,
                "relative_alt": int(position["alt"] * 1000),
                "vx": int(position["vx"] * 100),
                "vy": int(position["vy"] * 100),
                "vz": 0,
                "hdg": int(position["heading"] * 100),
            },
            {
                "type": "ATTITUDE",
                "roll": roll,
                "pitch": pitch,
                "yaw": yaw,
                "rollspeed": math.radians(2.0),
                "pitchspeed": math.radians(1.5),
                "yawspeed": math.radians(5.0),
            },
            {
                "type": "VFR_HUD",
                "airspeed": 12.0 if rtl_active else 8.0,
                "groundspeed": 12.0 if rtl_active else 8.0,
                "alt": 220.0 + position["alt"],
                "climb": 0.2 * math.sin(elapsed),
                "throttle": 55 if rtl_active else 42,
                "heading": int(position["heading"]),
            },
        ]

        # Lower-rate heartbeat, power, radio, and estimator evidence.
        if sequence % 5 == 0:
            messages.extend([
                {"type": "HEARTBEAT", "custom_mode": 6 if rtl_active else 4, "base_mode": 217, "system_status": 4, "mavlink_version": 3},
                {
                    "type": "SYS_STATUS",
                    "voltage_battery": int(battery_v * 1000),
                    "current_battery": int((10.0 + math.sin(elapsed * 0.3)) * 100),
                    "battery_remaining": battery_pct,
                    "load": 220,
                    "drop_rate_comm": 5,
                    "errors_comm": 0,
                },
                {"type": "RADIO_STATUS", "rssi": -96 if not link_online else 72, "remrssi": -97 if not link_online else 70, "noise": 30 if not link_online else 15, "remnoise": 30 if not link_online else 16, "rxerrors": sequence % 100 if not link_online else 0, "fixed": 0},
                {
                    "type": "ESTIMATOR_STATUS",
                    "velocity_variance": 0.03,
                    "pos_horiz_variance": 0.04,
                    "pos_vert_variance": 0.05,
                    "compass_variance": 0.02,
                    "pos_horiz_ratio": 0.08,
                    "pos_vert_ratio": 0.08,
                },
            ])

        for payload in messages:
            if stop_event.is_set():
                break
            _publish_simulation_message(payload, sequence)
            sequence += 1

        with _simulation_lock:
            _simulation_state.update({
                "running": True,
                "mode": "RTL" if rtl_active else "MISSION_LOOP",
                "position": position,
                "rtl_active": rtl_active,
                "link_online": link_online,
                "battery_v": battery_v,
                "battery_pct": battery_pct,
                "elapsed_s": elapsed,
                "started_at": start_wall,
                "message_count": sequence,
            })
        message_bus.publish(Message(
            type=MessageType.STATUS,
            source="mission_simulator",
            data={"simulation_running": True, "simulation_mode": "RTL" if rtl_active else "MISSION_LOOP", "rtl_active": rtl_active, "link_online": link_online, "position": position, "message_count": sequence},
        ))
        if home_reached:
            break
        stop_event.wait(0.2)

    with _simulation_lock:
        _simulation_state["running"] = False
        _simulation_state["mode"] = "HOME" if home_reached else "SIMULATION_STOPPED"
        _simulation_state["rtl_active"] = False
        _simulation_state["link_online"] = True
        _simulation_state["stop_after_rtl"] = False
        _simulation_state["home_reached"] = home_reached
    if home_reached:
        logger.info("Home waypoint reached")
        logger.info("RTL complete — mode changed to HOLD")
        logger.info("MAVLink link recovered")
        logger.warning("Drone DISARMED")
    else:
        logger.info("Mission stopped at last known position")
        logger.warning("Drone DISARMED")


def start_mission_simulation() -> bool:
    global _simulation_thread
    with _simulation_lock:
        if _simulation_state.get("rtl_active"):
            return False
        if _simulation_thread and _simulation_thread.is_alive():
            return True
        _simulation_stop.clear()
        hub.clear_track()
        _simulation_state.update({
            "running": True,
            "mode": "MISSION_STARTING",
            "message_count": 0,
            "rtl_active": False,
            "link_online": True,
            "stop_after_rtl": False,
            "home_reached": False,
        })
        _simulation_thread = threading.Thread(
            target=_mission_simulation_loop,
            args=(_simulation_stop,),
            name="analog-zero-mission-simulator",
            daemon=True,
        )
        _simulation_thread.start()
    return True


def stop_mission_simulation(force: bool = False) -> bool:
    """Request mission shutdown; normal shutdown is a safe RTL-to-home."""
    global _simulation_thread
    with _simulation_lock:
        thread = _simulation_thread
        was_running = bool(thread and thread.is_alive())
        if was_running and not force:
            _simulation_state["mode"] = "RTL"
            _simulation_state["rtl_active"] = True
            _simulation_state["stop_after_rtl"] = True
            # A user-requested stop is not a link failure, so telemetry remains online.
            _simulation_state["link_online"] = True
            logging.getLogger("drone_ids.mission_simulator").warning(
                "Stop requested — mode changed to RTL"
            )
            logging.getLogger("drone_ids.mission_simulator").warning(
                "Vehicle returning to home waypoint before shutdown"
            )
            return True
    _simulation_stop.set()
    if was_running and thread:
        thread.join(timeout=2.0)
    with _simulation_lock:
        _simulation_state["running"] = False
        _simulation_state["mode"] = "SIMULATION_STOPPED"
        _simulation_state["rtl_active"] = False
        _simulation_state["link_online"] = True
        _simulation_state["stop_after_rtl"] = False
    return was_running


def set_mission_mode(mode: str) -> Dict[str, Any]:
    """Set demonstration mission state (normal loop or autonomous RTL)."""
    normalized = mode.upper()
    if normalized not in {"MISSION_LOOP", "RTL"}:
        raise ValueError("mode must be MISSION_LOOP or RTL")
    with _simulation_lock:
        mission_running = bool(_simulation_state.get("running"))
    if normalized == "RTL" and not mission_running:
        raise ValueError("RTL requires an active mission")
    rtl = normalized == "RTL"
    logger = logging.getLogger("drone_ids.mission_simulator")
    with _simulation_lock:
        _simulation_state["mode"] = normalized
        _simulation_state["rtl_active"] = rtl
        _simulation_state["link_online"] = not rtl
        state = dict(_simulation_state)
    if rtl:
        logger.warning("MAVLink COMMAND LINK OFFLINE — packet flood detected")
        logger.warning("Failsafe engaged: mode changed to RTL")
        logger.warning("Vehicle returning to home waypoint")
    else:
        logger.info("MAVLink link recovered")
    return state


# ------------------------------------------------------------------
# Lifespan events
# ------------------------------------------------------------------

@app.on_event("startup")
async def _on_startup() -> None:
    """Capture the running event loop so background threads can schedule sends."""
    hub.set_loop(asyncio.get_event_loop())
    # Kick off a periodic status broadcast every 2 seconds
    asyncio.create_task(_periodic_status())


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    stop_mission_simulation(force=True)


async def _periodic_status() -> None:
    """Push engine status to all clients every 2 seconds."""
    from ..core.ids_engine import engine  # lazy import avoids circular deps
    while True:
        await asyncio.sleep(2)
        try:
            status = engine.get_status()
            await hub._broadcast({"type": "status", "data": status})
        except Exception:
            pass


# ------------------------------------------------------------------
# HTTP routes
# ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard() -> HTMLResponse:
    html = (_STATIC_DIR / "mission.html").read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.get("/api/status")
async def api_status() -> JSONResponse:
    from ..core.ids_engine import engine
    return JSONResponse(engine.get_status())


@app.get("/api/alerts")
async def api_alerts(limit: int = 500) -> JSONResponse:
    alerts = list(hub._recent_alerts)
    if not alerts:
        alerts = _read_json_lines(_ALLOWED_LOGS["alerts"])
    return JSONResponse({"alerts": alerts[-limit:]})


@app.get("/api/telemetry")
async def api_telemetry() -> JSONResponse:
    return JSONResponse(hub.telemetry_payload())


@app.post("/api/simulation/start")
async def api_simulation_start() -> JSONResponse:
    started = start_mission_simulation()
    with _simulation_lock:
        state = dict(_simulation_state)
    if not started:
        return JSONResponse({
            "detail": "Mission start is disabled while RTL is active",
            "running": False,
            "simulation": state,
        }, status_code=409)
    return JSONResponse({"running": True, "simulation": state})


@app.post("/api/simulation/stop")
async def api_simulation_stop() -> JSONResponse:
    was_running = stop_mission_simulation()
    with _simulation_lock:
        state = dict(_simulation_state)
    return JSONResponse({
        "running": bool(state.get("running")),
        "was_running": was_running,
        "simulation": state,
    })


@app.get("/api/simulation/state")
async def api_simulation_state() -> JSONResponse:
    with _simulation_lock:
        state = dict(_simulation_state)
    return JSONResponse({"simulation": state})


@app.post("/api/simulation/mode")
async def api_simulation_mode(payload: Dict[str, Any]) -> JSONResponse:
    try:
        state = set_mission_mode(str(payload.get("mode", "")))
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return JSONResponse({"simulation": state})


@app.get("/api/detectors")
async def api_detectors() -> JSONResponse:
    from ..core.ids_engine import EngineState, engine

    source_alerts = list(hub._recent_alerts)
    if not source_alerts:
        source_alerts = _read_json_lines(_ALLOWED_LOGS["alerts"])[-500:]

    alert_counts: Dict[str, int] = {}
    for alert in source_alerts:
        detector = alert.get("detector", "UNKNOWN")
        alert_counts[detector] = alert_counts.get(detector, 0) + 1

    running = engine.state == EngineState.RUNNING
    detectors = []
    for profile in _DETECTOR_PROFILES:
        detectors.append({
            **profile,
            "alert_count": alert_counts.get(profile["id"], 0),
            "active": running,
        })
    return JSONResponse({"detectors": detectors})


@app.get("/api/forensics")
async def api_forensics() -> JSONResponse:
    alerts = _read_json_lines(_ALLOWED_LOGS["alerts"])
    evidence = _read_json_lines(_ALLOWED_LOGS["evidence"])
    chain = _read_json_lines(_ALLOWED_LOGS["chain"])

    linked_tail = 0
    for index in range(len(chain) - 1, -1, -1):
        if index == len(chain) - 1:
            linked_tail = 1
        elif chain[index + 1].get("previous_hash") == chain[index].get("current_hash"):
            linked_tail += 1
        else:
            break

    latest_evidence = evidence[-1] if evidence else None
    latest_chain = chain[-1] if chain else None
    return JSONResponse({
        "alerts": {
            "records": len(alerts),
            "path": str(_ALLOWED_LOGS["alerts"]),
        },
        "evidence": {
            "records": len(evidence),
            "verified_records": _verify_evidence_records(evidence),
            "latest_hash": (latest_evidence or {}).get("evidence_hash"),
            "path": str(_ALLOWED_LOGS["evidence"]),
        },
        "chain": {
            "records": len(chain),
            "linked_tail_records": linked_tail,
            "latest_hash": (latest_chain or {}).get("current_hash"),
            "path": str(_ALLOWED_LOGS["chain"]),
        },
        "latest_evidence": latest_evidence,
    })


@app.get("/api/logs/{log_name}")
async def api_download_log(log_name: str) -> Any:
    if log_name not in _ALLOWED_LOGS:
        return JSONResponse({"detail": "Unknown evidence log"}, status_code=404)  # type: ignore[return-value]
    path = _ALLOWED_LOGS[log_name]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    return FileResponse(path, filename=path.name, media_type="application/x-ndjson")


# ------------------------------------------------------------------
# WebSocket endpoint
# ------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    hub.add_client(ws)
    await hub.send_initial_state(ws)
    try:
        while True:
            # Keep connection open; ignore any client pings
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        hub.remove_client(ws)


# ---------------------------------------------------------------------------
# Setup helper called from run_web.py
# ---------------------------------------------------------------------------

def setup_hub() -> None:
    """Subscribe the hub to the global message bus."""
    from ..core.message_bus import message_bus, MessageType
    message_bus.subscribe(MessageType.ALERT, hub.on_alert)
    message_bus.subscribe(MessageType.STATUS, hub.on_status)
    message_bus.subscribe(MessageType.MAVLINK_MESSAGE, hub.on_mavlink_message)
