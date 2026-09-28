# ANALOG ZERO Drone IDS Mission Console

**PUSHPAK Grand Challenge 2026 — Security of Drones, Objective 2**

ANALOG ZERO Drone IDS is a MAVLink-compatible intrusion-detection prototype for ArduPilot/PX4-style UAV systems. It passively monitors telemetry and detects six classes of cyber-physical attacks while producing live alerts and tamper-evident forensic evidence.

The project includes a Mission Planner-inspired web console, a demonstration mission simulator, a realistic UDP attack injector, and optional ArduPilot SITL integration.

---

## What the System Detects

| Test | Threat | Main detection evidence | MITRE ATT&CK |
|---|---|---|---|
| TC-01 | GPS spoofing | Position jumps, impossible GPS velocity, GPS/fused-navigation divergence, satellite quality | T1557 |
| TC-02 | MAVLink anomaly | Sequence discontinuities, timing/rate changes, message anomalies | T1499 / T1557 |
| TC-03 | Command injection | Unauthorized source IDs, ARM/DISARM attempts, command bursts, missing ACKs | T1021 / T1562 |
| TC-04 | Denial of service | Packet floods, heartbeat/link degradation, message-rate threshold violations | T1499 |
| TC-05 | Telemetry manipulation | Impossible attitude rates, impossible reported velocity, power jumps, sensor mismatch | T1557 |
| TC-06 | Firmware/parameter integrity | Critical parameter hash mismatch and boot/integrity checks | T1542 / T1562 |

---

## Main Components

- **MAVLink interface** — reads SITL/real telemetry and opens the JSON injection listener.
- **Message bus** — distributes MAVLink and status messages to components.
- **IDS engine** — invokes six detector modules and tracks runtime statistics.
- **Alert manager** — writes console, alert, evidence, and chain-of-custody records.
- **Mission web console** — satellite map, telemetry, detector coverage, alerts, validation, console, and forensics.
- **Demo mission simulator** — generates a safe waypoint loop and RTL behavior without controlling a real aircraft.
- **Attack injector** — sends realistic attack evidence through UDP `14551`.

---

## Installation

Python 3.10 or newer is recommended.

```bash
cd /home/vishal/Desktop/simulationfix
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

The satellite map uses browser-loaded Leaflet and Esri imagery. If the map services are unavailable, the interface automatically falls back to a local 2D track view.

---

## Demonstration Without SITL

This is the recommended demonstration mode. It does not require ArduPilot SITL.

### Terminal 1 — start the mission console

```bash
cd /home/vishal/Desktop/simulationfix
python run_web.py
```

Open:

```text
http://localhost:8080
```

Wait until the header shows:

```text
IDS Engine: RUNNING
```

Even without SITL, the IDS engine, detector stack, logging, and attack-injection listener remain active.

### Browser — start the simulated mission

Click:

```text
START MISSION
```

The map displays a persistent HOME/Waypoint 1 marker and the simulated route. The UAV marker moves through the waypoint loop while GPS, attitude, speed, altitude, battery, heartbeat, and radio values update.

### Terminal 2 — run the attack scenario

```bash
cd /home/vishal/Desktop/simulationfix
python run_attack.py
```

The injector executes the attacks in this order:

1. Normal baseline telemetry
2. TC-01 — GPS carry-off/spoofing
3. TC-05 — telemetry manipulation
4. TC-03 — unauthorized command injection
5. TC-02 — MAVLink sequence discontinuity
6. TC-06 — firmware/parameter integrity violation
7. TC-04 — extended DoS flood with link loss and RTL

If the web server uses a different port, point the injector to its simulation state API:

```bash
python run_attack.py --state-url http://127.0.0.1:8090/api/simulation/state
```

---

## Optional SITL Mode

Start ArduPilot SITL first:

```bash
sim_vehicle.py -v copter -f quad -I0 --console --map
```

Then start the console:

```bash
python run_web.py
```

The default connection is configured in `config/ids_config.yaml`:

```yaml
sitl:
  connection_string: "udp:127.0.0.1:14550"
  source_system: 255
  source_component: 190
```

For a serial connection, modify the connection string, for example:

```yaml
sitl:
  connection_string: "serial:///dev/ttyUSB0:115200"
```

The built-in mission simulator remains monitor-only and does not send commands to a real flight controller.

---

## Mission and RTL Behavior

### START MISSION

- Shows waypoint/home markers.
- Starts the simulated waypoint loop.
- Generates normal MAVLink-equivalent telemetry.
- Logs firmware check, GUIDED mode, arming, and mission start in the console.

### STOP MISSION

STOP MISSION is a safe stop, not an instantaneous stop:

1. The vehicle enters RTL.
2. The control displays `RTL TO HOME` and is disabled.
3. The drone returns to permanent HOME/Waypoint 1.
4. RTL completes and the drone disarms.
5. START MISSION is enabled only after home is reached.

### DoS-triggered RTL

During the final extended packet flood:

- `message_flood` alerts are generated.
- MAVLink is displayed as offline.
- The vehicle enters RTL.
- The mission does not automatically resume after the attack.
- The vehicle must reach home before another mission can start.

Attack packets are treated as detection evidence only. They do not overwrite the displayed mission route or authoritative instrument values.

---

## Web Console Views

### Security Map

- Satellite imagery
- Permanent HOME/Waypoint 1
- Waypoint route
- Moving heading arrow
- Live track
- Current coordinates and altitude
- GPS alert count

Threat circles are intentionally not drawn; security details are presented in alerts and evidence views.

### Telemetry

Displays normalized MAVLink values:

- Roll/pitch/yaw
- Latitude/longitude
- Altitude
- Ground and GPS speed
- HDOP/VDOP
- Battery voltage/current
- RSSI
- Communication drop rate
- EKF horizontal/vertical ratios

### Detection Units

Shows all six detector modules, their validation IDs, alert counts, observation signals, and MITRE mappings.

### Alerts

A full-height, filterable security-event register with severity, detector, alert type, description, MITRE technique, and confidence.

### Validation

Maps TC-01 through TC-06 to detector observations and marks each scenario as `READY` or `DETECTED`.

### Forensics

Shows alert records, evidence records, verified SHA-256 records, chain entries, latest hashes, and raw latest evidence JSON.

Downloads:

- `alerts.log`
- `evidence.log`
- `chain_of_custody.log`

### Console

Displays unlimited live engine, simulator, and detector log output.

---

## HTTP API

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/` | Mission Console |
| GET | `/api/status` | IDS engine state and statistics |
| GET | `/api/alerts?limit=500` | Current or persisted alert history |
| GET | `/api/telemetry` | Latest normalized telemetry and track |
| GET | `/api/detectors` | Detector profiles and alert counts |
| GET | `/api/forensics` | Evidence and hash-chain status |
| POST | `/api/simulation/start` | Start demo waypoint mission |
| POST | `/api/simulation/stop` | Enter safe RTL-to-home stop |
| GET | `/api/simulation/state` | Demo mission state |
| POST | `/api/simulation/mode` | Demo mode control, used internally by attacks |
| GET | `/api/logs/alerts` | Download alert log |
| GET | `/api/logs/evidence` | Download evidence log |
| GET | `/api/logs/chain` | Download chain-of-custody log |
| WS | `/ws` | Live alert, log, status, and telemetry stream |

---

## Configuration

All detector and runtime thresholds are in:

```text
config/ids_config.yaml
```

Typical settings include:

- GPS position-jump and velocity limits
- MAVLink sequence and rate thresholds
- Authorized command source IDs
- Critical parameter names and integrity hashes
- Telemetry kinematic limits
- DoS packet-rate, heartbeat, and RSSI thresholds
- Alert severity/output settings
- Forensic log paths

The current trusted parameter hash configuration includes the normal `SYSID_THISMAV = 1.0` reference used by the firmware integrity demonstration.

---

## Logs and Evidence

Runtime output is stored in `logs/`:

| File | Purpose |
|---|---|
| `alerts.log` | One JSON alert per line |
| `evidence.log` | Alert evidence with SHA-256 hashes |
| `chain_of_custody.log` | Linked previous/current hash entries |
| `drone_ids.log` | Operational engine and interface logs |

Evidence records are used by the Forensics page and can be downloaded directly from the dashboard.

---

## Tests

Run the current regression suite:

```bash
PYTHONDONTWRITEBYTECODE=1 pytest -q -p no:cacheprovider tests
```

Expected result:

```text
12 passed
```

The tests cover imports, configuration loading, engine integration, GPS spoofing, command injection, telemetry manipulation, DoS flood/radio handling, and firmware parameter integrity.

---

## Project Structure

```text
simulationfix/
├── config/
│   └── ids_config.yaml          # Detector and runtime configuration
├── logs/                        # Generated alert/evidence/runtime logs
├── src/drone_ids/
│   ├── alerting/                # Console, file, evidence, hash-chain output
│   ├── core/                    # Config, message bus, IDS engine
│   ├── detectors/               # Six attack detector modules
│   ├── interfaces/              # MAVLink/SITL and injection interfaces
│   ├── scripts/                 # Internal console entry points
│   └── web/
│       ├── server.py            # FastAPI, WebSocket, demo simulation APIs
│       └── static/mission.html  # Mission Planner-style web console
├── tests/                       # Detection and integration tests
├── run_web.py                   # Mission console launcher
├── run_ids.py                   # SITL/real MAVLink IDS launcher
├── run_attack.py                # Realistic UDP attack injector
├── requirements.txt             # Runtime and test dependencies
└── pyproject.toml               # Python package metadata
```

---

