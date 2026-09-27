#!/usr/bin/env python3
"""ANALOG ZERO realistic attack injector for the Drone IDS mission demo.

Sends JSON attack/telemetry packets to the IDS injection port. When the web
mission simulator is running, attacks are generated relative to its live state;
otherwise the same waypoint loop is used as a local fallback.
"""
import argparse
import json
import math
import socket
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

WAYPOINTS = [
    {"lat": 28.61390, "lon": 77.20900, "alt": 120.0},
    {"lat": 28.61600, "lon": 77.21350, "alt": 125.0},
    {"lat": 28.61420, "lon": 77.21800, "alt": 130.0},
    {"lat": 28.61050, "lon": 77.21400, "alt": 135.0},
]
ROUTE_SPEED_MPS = 8.0


def now_usec():
    return int(time.time() * 1_000_000)


def haversine_m(lat1, lon1, lat2, lon2):
    radius = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    value = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def fallback_route_position(elapsed):
    distance = (elapsed * ROUTE_SPEED_MPS)
    legs = []
    total = 0.0
    for index, start in enumerate(WAYPOINTS):
        end = WAYPOINTS[(index + 1) % len(WAYPOINTS)]
        length = haversine_m(start["lat"], start["lon"], end["lat"], end["lon"])
        legs.append((start, end, length))
        total += length
    distance %= total
    for start, end, length in legs:
        if distance <= length:
            ratio = distance / length
            lat = start["lat"] + (end["lat"] - start["lat"]) * ratio
            lon = start["lon"] + (end["lon"] - start["lon"]) * ratio
            alt = start["alt"] + (end["alt"] - start["alt"]) * ratio
            dlat = (end["lat"] - start["lat"]) * 111320.0
            dlon = (end["lon"] - start["lon"]) * 111320.0 * math.cos(math.radians(start["lat"]))
            heading = (math.degrees(math.atan2(dlon, dlat)) + 360.0) % 360.0
            return {"lat": lat, "lon": lon, "alt": alt, "heading": heading, "vx": 0.0, "vy": ROUTE_SPEED_MPS}
        distance -= length
    return {**WAYPOINTS[0], "heading": 0.0, "vx": 0.0, "vy": 0.0}


class AttackSender:
    def __init__(self, host, port):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.target = (host, port)
        self.seq = 0

    def send(self, payload):
        payload.setdefault("time_usec", now_usec())
        # Attack packets are detection evidence only. They must not overwrite the
        # mission simulator's displayed drone position or live instrument values.
        payload["display_update"] = False
        payload.setdefault("src_sys", 1)
        payload.setdefault("src_comp", 1)
        payload["seq"] = payload.get("seq", self.seq) % 256
        self.seq += 1
        self.sock.sendto(json.dumps(payload).encode("utf-8"), self.target)

    def close(self):
        self.sock.close()


class LiveState:
    def __init__(self, state_url):
        self.state_url = state_url
        self.mode_url = state_url.replace("/api/simulation/state", "/api/simulation/mode")
        self.started = time.monotonic()
        self.cached = None
        self.cached_at = 0.0

    def set_mode(self, mode):
        try:
            request = urllib.request.Request(
                self.mode_url,
                data=json.dumps({"mode": mode}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=0.5):
                pass
            return True
        except Exception:
            return False

    def current(self):
        now = time.monotonic()
        if now - self.cached_at > 0.15:
            try:
                with urllib.request.urlopen(self.state_url, timeout=0.35) as response:
                    body = json.loads(response.read().decode("utf-8"))
                sim = body.get("simulation", {})
                position = sim.get("position")
                if position:
                    self.cached = {
                        "lat": float(position["lat"]),
                        "lon": float(position["lon"]),
                        "alt": float(position.get("alt", 120.0)),
                        "heading": float(position.get("heading", 0.0)),
                        "battery_v": float(sim.get("battery_v", 16.4)),
                        "running": bool(sim.get("running", False)),
                    }
                    self.cached_at = now
            except Exception:
                self.cached = None
        if self.cached:
            return dict(self.cached)
        position = fallback_route_position(now - self.started)
        position["battery_v"] = 16.4
        position["running"] = False
        return position

def send_statustext(sender, text, severity=6):
    sender.send({"type": "STATUSTEXT", "severity": severity, "text": text})


def send_gps(sender, state, spoof_offset_m=0.0, spoof_speed_mps=None):
    heading = math.radians(state.get("heading", 0.0))
    # Carry the forged position forward along the existing heading. The aircraft
    # remains visually aligned with its route while GPS and fused navigation diverge.
    lat = state["lat"] + (spoof_offset_m * math.cos(heading)) / 111320.0
    lon_scale = 111320.0 * math.cos(math.radians(state["lat"]))
    lon = state["lon"] + (spoof_offset_m * math.sin(heading)) / max(lon_scale, 1.0)
    speed_cms = int((spoof_speed_mps if spoof_speed_mps is not None else ROUTE_SPEED_MPS) * 100)
    sender.send({
        "type": "GPS_RAW_INT", "lat": int(lat * 1e7), "lon": int(lon * 1e7),
        "alt": int((220.0 + state["alt"]) * 1000), "eph": 95, "epv": 140,
        "vel": speed_cms, "cog": int(state.get("heading", 0) * 100),
        "satellites_visible": 14, "fix_type": 3,
    })


def send_global_position(sender, state, speed_override=None):
    heading = math.radians(state.get("heading", 0.0))
    speed = speed_override if speed_override is not None else ROUTE_SPEED_MPS
    vx = speed * math.sin(heading)
    vy = speed * math.cos(heading)
    sender.send({
        "type": "GLOBAL_POSITION_INT", "lat": int(state["lat"] * 1e7), "lon": int(state["lon"] * 1e7),
        "alt": int((220.0 + state["alt"]) * 1000), "relative_alt": int(state["alt"] * 1000),
        "vx": int(vx * 100), "vy": int(vy * 100), "vz": 0,
        "hdg": int(state.get("heading", 0) * 100),
    })


def send_attitude(sender, state, impossible=False):
    elapsed = time.monotonic()
    roll = math.radians(3 * math.sin(elapsed))
    pitch = math.radians(2 * math.cos(elapsed * 0.7))
    yaw = math.radians(state.get("heading", 0.0))
    sender.send({
        "type": "ATTITUDE", "roll": roll, "pitch": pitch, "yaw": yaw,
        "rollspeed": 8.72 if impossible else math.radians(2),
        "pitchspeed": math.radians(60) if impossible else math.radians(1.5),
        "yawspeed": math.radians(5),
    })


def send_hud(sender, state, speed_override=None):
    sender.send({
        "type": "VFR_HUD", "airspeed": speed_override or ROUTE_SPEED_MPS,
        "groundspeed": speed_override or ROUTE_SPEED_MPS,
        "alt": 220.0 + state["alt"], "climb": 0.1,
        "throttle": 90 if speed_override else 42,
        "heading": int(state.get("heading", 0)),
    })


def send_power(sender, state, tampered=False):
    voltage = 8.1 if tampered else state.get("battery_v", 16.4)
    current = 120.0 if tampered else 10.0
    sender.send({
        "type": "SYS_STATUS", "voltage_battery": int(voltage * 1000),
        "current_battery": int(current * 100), "battery_remaining": 8 if tampered else 90,
        "load": 900 if tampered else 220, "drop_rate_comm": 5, "errors_comm": 0,
    })


def send_radio(sender, degraded=False):
    sender.send({
        "type": "RADIO_STATUS", "rssi": -95 if degraded else 72,
        "remrssi": -96 if degraded else 70, "noise": 25 if degraded else 15,
        "remnoise": 25 if degraded else 16, "rxerrors": 45 if degraded else 0, "fixed": 0,
    })


def send_normal_cycle(sender, live, repeats=5):
    for _ in range(repeats):
        state = live.current()
        send_gps(sender, state)
        send_global_position(sender, state)
        send_attitude(sender, state)
        send_hud(sender, state)
        send_power(sender, state)
        send_radio(sender)
        time.sleep(0.12)


def send_command(sender, command, param1=0, name="COMMAND"):
    params = [param1, 0, 0, 0, 0, 0, 0]
    sender.send({
        "type": "COMMAND_LONG", "command": command,
        "param1": params[0], "param2": params[1], "param3": params[2],
        "param4": params[3], "param5": params[4], "param6": params[5],
        "param7": params[6], "target_system": 1, "target_component": 1,
        "src_sys": 99, "src_comp": 1,
    })
    send_statustext(sender, f"ATTACK_ACTIVE unauthorized {name} from SYS 99", severity=2)
    print(f"  Injected unauthorized {name} from system 99")


def send_param(sender, name, value, source=99):
    sender.send({
        "type": "PARAM_VALUE", "param_id": name, "param_value": float(value),
        "param_type": 9, "param_count": 1, "param_index": 0,
        "src_sys": source, "src_comp": 1,
    })
    print(f"  Injected parameter {name}={value}")


def attack_gps_spoof(sender, live):
    print("\nTC-01 — GPS SPOOFING / CARRY-OFF")
    send_statustext(sender, "ATTACK_ACTIVE GPS carry-off spoofing", severity=2)
    for index in range(30):
        state = live.current()
        offset = index * 8.0
        send_gps(sender, state, spoof_offset_m=offset, spoof_speed_mps=ROUTE_SPEED_MPS + offset * 0.2)
        if index % 3 == 0:
            send_global_position(sender, state)
            send_attitude(sender, state)
        time.sleep(0.1)
    state = live.current()
    send_gps(sender, state, spoof_offset_m=350.0, spoof_speed_mps=180.0)
    time.sleep(1.0)


def attack_telemetry(sender, live):
    print("\nTC-05 — TELEMETRY MANIPULATION")
    send_statustext(sender, "ATTACK_ACTIVE impossible motion and power telemetry", severity=2)
    for index in range(20):
        state = live.current()
        send_attitude(sender, state, impossible=True)
        send_global_position(sender, state, speed_override=220.0)
        send_hud(sender, state, speed_override=220.0)
        if index % 4 == 0:
            send_power(sender, state, tampered=True)
        time.sleep(0.08)
    time.sleep(1.0)


def attack_commands(sender):
    print("\nTC-03 — COMMAND INJECTION")
    for command, param1, name in [
        (400, 1, "ARM"), (400, 0, "DISARM"), (176, 1, "DO_SET_HOME"),
        (22, 1, "TAKEOFF"), (21, 0, "PREFLIGHT_CALIBRATION"),
    ]:
        send_command(sender, command, param1, name)
        time.sleep(0.35)


def attack_dos(sender, live):
    print("\nTC-04 — DoS MESSAGE FLOOD WITH LINK LOSS / RTL")
    send_statustext(sender, "ATTACK_ACTIVE packet flood — command link under attack", severity=2)
    rtl_started = live.set_mode("RTL")
    if rtl_started:
        print("  Simulated command link offline; autopilot entering RTL")
    else:
        print("  Mission is not active; continuing with packet flood only")
    packets = 1800
    burst_size = 60
    for burst in range(packets // burst_size):
        for index in range(burst_size):
            packet_index = burst * burst_size + index
            sender.send({
                "type": "PARAM_REQUEST_READ", "target_system": 1, "target_component": 1,
                "param_id": f"FLOOD_{packet_index}", "param_index": -1,
            })
        time.sleep(0.09)
    print(f"  Sent {packets} flood packets over the extended attack window")
    time.sleep(0.7)
    if rtl_started:
        send_statustext(sender, "ATTACK_COMPLETE vehicle remains in RTL until home", severity=6)
        print("  Attack complete; vehicle remains in RTL until it reaches home")
    else:
        send_statustext(sender, "ATTACK_COMPLETE packet flood completed", severity=6)
        print("  Attack complete; no active mission was present")


def attack_sequence(sender):
    print("\nTC-02 — MAVLINK SEQUENCE GAP")
    sender.seq = (sender.seq + 95) % 256
    for index in range(15):
        send_statustext(sender, f"SEQUENCE_GAP_TEST_{index}", severity=5)
        time.sleep(0.03)
    time.sleep(0.7)


def attack_firmware(sender):
    print("\nTC-06 — FIRMWARE / PARAMETER INTEGRITY")
    send_statustext(sender, "ATTACK_ACTIVE critical parameter integrity mismatch", severity=2)
    send_param(sender, "SYSID_THISMAV", 999.0)
    send_param(sender, "ARMING_CHECK", 0.0)
    send_param(sender, "FS_GCS_ENABLE", 0.0)
    time.sleep(1.0)


def run_all_attacks(sender, live):
    print("\nNormal telemetry baseline")
    send_statustext(sender, "BASELINE normal mission telemetry", severity=6)
    send_normal_cycle(sender, live, repeats=12)
    attack_gps_spoof(sender, live)
    send_normal_cycle(sender, live, repeats=4)
    attack_telemetry(sender, live)
    send_normal_cycle(sender, live, repeats=4)
    attack_commands(sender)
    send_normal_cycle(sender, live, repeats=4)
    attack_sequence(sender)
    attack_firmware(sender)
    attack_dos(sender, live)
    send_statustext(sender, "ATTACK_SCENARIO_COMPLETE", severity=6)
    time.sleep(2.5)
    print("\nDemonstration complete — all six attack classes injected")


def main():
    parser = argparse.ArgumentParser(description="ANALOG ZERO Drone IDS realistic attack injector")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=14551)
    parser.add_argument("--state-url", default="http://127.0.0.1:8080/api/simulation/state")
    args = parser.parse_args()
    sender = AttackSender(args.host, args.port)
    live = LiveState(args.state_url)
    print("=" * 60)
    print("ANALOG ZERO — REALISTIC ATTACK SIMULATION")
    print(f"IDS injection: udp://{args.host}:{args.port}")
    print(f"Mission state source: {args.state_url}")
    print("=" * 60)
    try:
        run_all_attacks(sender, live)
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    finally:
        sender.close()
        print("Done.")


if __name__ == "__main__":
    main()
