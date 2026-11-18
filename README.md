# TwinTrust

**An educational IoT project demonstrating data quality and noise handling using an STM32 board and a BME280 sensor.**

TwinTrust shows why sensor data should be checked before it is used for decisions. It compares temperature readings before and after filtering, demonstrates missing data, and evaluates simple temperature forecasts.

## Project purpose

This project supports the topic:

**Data Quality and Noise Handling in Digital Twins and IoT**

The main lessons are:

- Sensor readings can contain noise and sudden spikes.
- Filtering can reduce disturbances, but also delay real changes.
- Missing data should remain visible.
- Old readings should not be treated as current information.
- A smoother signal does not automatically produce a better forecast.

TwinTrust is a learning prototype, not a validated industrial monitoring or safety system.

## Hardware

- STM32F4 Discovery board — STM32F407G-DISC1
- BME280 temperature, humidity and pressure sensor
- USB cable
- Jumper wires
- Windows laptop

### Sensor wiring

The working project uses I²C1:

| BME280 | STM32F4 Discovery |
|---|---|
| VCC / VIN | 3V / 3.3V |
| GND | GND |
| SDA / SDI | PB7 |
| SCL / SCK | PB8 |

The firmware pin configuration must match the wiring.

The active STM32F407 sensor firmware is in:

`firmware/Discovery_BME280_Test/`

The repository also contains a separate `TwinTrust_Station` firmware project targeting STM32F439. Use the firmware matching your board.

## How it works

1. The BME280 measures temperature, humidity and pressure.
2. The STM32 reads the sensor and sends messages through USB.
3. A Python gateway forwards the messages to an MQTT broker.
4. The backend stores messages and checks data quality.
5. The dashboard displays readings, filtering demonstrations and saved results.

The STM32 firmware supplies:

- `seq`: message sequence number.
- `uptime_ms`: time since board startup, not a synchronized date-and-time clock.
- `valid`: device validity flag, which does not prove measurement accuracy.

Default MQTT topic:

` twintrust/discovery01/telemetry `

## Website sections

### 1. Live readings

Displays live sensor information and compares accepted temperature readings before and after filtering.

Temperature processing uses:

- A median filter over five readings.
- Exponential moving average smoothing with alpha 0.25.

Dashboard states:

| State | Meaning |
|---|---|
| READY | A recent accepted reading is available |
| STALE | No accepted update has arrived for more than five seconds |
| UNAVAILABLE | No usable accepted reading is available for the current processing session |

When data becomes stale, previous values remain visible, but decisions and forecasts are withheld.

The dashboard also provides a simple 30-second temperature forecast. It requires sufficient recent history and is an estimate, not a guaranteed prediction.

The 22°C warning threshold is for demonstration only.

Humidity and pressure are displayed as additional measurements. The filtering comparisons and forecast demonstrations focus on temperature.

### 2. Noise and filtering demo

Replays recorded data with simulated changes.

| Scenario | What it demonstrates |
|---|---|
| Spikes | Filtering reduces the effect of brief isolated jumps |
| Sustained rise | Filtering follows a continuing increase but introduces delay |
| Dropout | Missing readings remain visible as gaps |

Blue represents raw data. Yellow represents filtered data.

The controls allow users to change the median window, EMA alpha and playback speed.

These controls affect the replay only. They do not change the physical sensor or live processing settings.

### 3. Experiment results

Shows saved evaluations of 30-second forecasts.

The methods compared are:

- Raw trend: extrapolates the recent raw temperature trend.
- Filtered trend: extrapolates the recent filtered temperature trend.
- Hold value: assumes the latest raw temperature remains unchanged.

MAE means Mean Absolute Error, measured in °C. Lower values mean less average prediction error.

Coverage describes the proportion of eligible forecast starting points that received a scored prediction.

Saved experiment results do not change when replay filter settings are adjusted.

## Presenter pause and resume controls

The website includes presenter controls protected by a private key.

- Pause live updates: stops MQTT forwarding while the gateway continues reading and saving raw sensor data.
- Resume live updates: forwards new readings again.

Paused readings are not queued for later forwarding.

After more than five seconds without accepted updates, the dashboard becomes STALE. Fresh accepted readings restore READY.

Keep the STM32 connected during this demonstration.

Use `serial_to_mqtt_demo.py` for website-controlled forwarding.

Keep the presenter key private. Do not commit it to GitHub.

## Software requirements

- Python and a virtual environment
- Packages listed in `requirements.txt`
- Mosquitto MQTT broker
- STM32 development and flashing tools
- cloudflared, if public access is needed

## Local setup

Clone the repository:

```powershell
git clone https://github.com/nirzor-deb-25/twintrust-project.git
Set-Location twintrust-project
```

Create the Python environment and install dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Build and flash the appropriate firmware using STM32CubeIDE.

Local recordings, databases, presenter credentials and backups are excluded from this repository. Saved replay assets or experiment reports may need to be generated or supplied separately before those sections are available in a fresh checkout.

## Start the system

Run the following programs in separate PowerShell windows.

For the Python commands, first change to the repository folder.

### 1. MQTT broker

For a standard Windows Mosquitto installation:

```powershell
& "C:\Program Files\mosquitto\mosquitto.exe" -v
```

The application expects a local broker on port 1883.

If a broker is already running, use that instance.

### 2. Backend

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
```

### 3. Sensor gateway

```powershell
.\.venv\Scripts\python.exe -u .\gateway\serial_to_mqtt_demo.py --port COM8 --seconds 7200
```

Replace `COM8` with the board's actual serial port.

This command runs the gateway for two hours.

### 4. Open the dashboard

http://127.0.0.1:8000/

Leave the broker, backend and gateway running.

## Start a new session after a board restart

The firmware does not provide a unique boot identifier. After a board restart, sequence and uptime counters restart, so messages may be rejected as backward counters.

After a confirmed board restart:

1. Stop the gateway with Ctrl+C.
2. Keep the broker and backend running.
3. Save a new shared session boundary:

```powershell
.\.venv\Scripts\python.exe -m backend.app --start-new-quality-session
```

4. Wait for `Shared boundary saved`.
5. Press the board's black RESET button once.
6. Restart the gateway.

The saved boundary applies to all dashboard visitors. Visitors do not need to enter a message ID.

A normal pause/resume demonstration does not require a board reset or a new session.

## Public access

With the local website working, start Cloudflare Tunnel:

```powershell
& "$env:USERPROFILE\Downloads\cloudflared-windows-amd64.exe" tunnel --protocol http2 --url http://127.0.0.1:8000
```

Wait for `Registered tunnel connection` and share the HTTPS address printed by cloudflared.

Quick Tunnel addresses are temporary and may change when a new tunnel is created.

The presentation entry page is:

https://nirzor-deb-25.github.io/twintrust/

Its redirect must point to the current tunnel address. This GitHub Pages address does not run the Python backend.

Public access requires the laptop, backend and tunnel to remain running. Live measurements also require the broker, gateway and connected sensor.

## Tests

Run the Python test suite:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Hardware operation and public access should also be checked on the actual presentation setup.

## Repository structure

| Folder | Purpose |
|---|---|
| `firmware/` | STM32 firmware projects |
| `gateway/` | Serial capture, MQTT forwarding and evaluation tools |
| `backend/` | Web API and presenter control routes |
| `processing/` | Data quality and temperature processing |
| `web/` | Dashboard interface |
| `tests/` | Automated Python tests |

## Limitations

- Filtering does not prove sensor accuracy.
- A sudden spike can represent a real event.
- Stronger smoothing can delay a warning.
- Filtering cannot recover missing measurements.
- Recent backend reception does not prove a recent source measurement.
- Counter-based restart handling requires operator confirmation.
- Processing uses a limited window of stored messages.
- The forecast is a simple trend estimate, not a validated safety signal.
- Saved experiments use recorded data and synthetic variations, not a calibrated physical reference or independent validation.

## Acknowledgements

This project uses third-party components, including STM32 HAL, CMSIS, STM32 USB libraries and the Bosch BME280 driver. Their applicable license notices are retained in the source tree.
