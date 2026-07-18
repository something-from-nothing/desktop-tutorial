# ESP32 Weather Station 🌦️

A weather station for the **Keyestudio ESP32** (works on most ESP32 dev boards).
It pulls the **live weather for your area** from the internet and prints it
straight to the **Arduino IDE Serial Monitor**, refreshing on a timer.
No website, no browser — just the Serial Monitor.

No sensors and no soldering are required — the weather comes from
[Open-Meteo](https://open-meteo.com), a **free** API that needs **no account and
no API key**.

Example of what the Serial Monitor shows:

```
========================================
  WEATHER FOR London
========================================
  Conditions : Partly cloudy
  Temperature: 18.4 C
  Feels like : 17.9 C
  Humidity   : 64 %
  Wind       : 11.2 mph from SW
  Rain       : 0.0 mm
  Updated    : 2026-07-18T15:00
========================================
```

---

## What you need

- A **Keyestudio ESP32** board (or any ESP32 dev board) + USB cable
- The **Arduino IDE** (free): <https://www.arduino.cc/en/software>
- Your home **WiFi name and password**
- 5 minutes

---

## Step 1 — Add ESP32 support to the Arduino IDE

1. Open the Arduino IDE.
2. Go to **File → Preferences**.
3. In **"Additional Boards Manager URLs"**, paste:
   ```
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
   ```
4. Go to **Tools → Board → Boards Manager**, search **`esp32`**, and click
   **Install** (by Espressif Systems).

## Step 2 — Install the ArduinoJson library

- Go to **Tools → Manage Libraries…**, search **`ArduinoJson`**, and install it
  (version **7** or newer).

## Step 3 — Open the sketch

- Open **`WeatherStation.ino`** from this folder in the Arduino IDE.

## Step 4 — Enter your details

Near the top of the sketch, edit these:

```cpp
const char* WIFI_SSID     = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

const float LATITUDE  = 51.5074;     // <- your latitude
const float LONGITUDE = -0.1278;     // <- your longitude
const char* LOCATION_NAME = "London";// <- a label for the printout
```

**Finding your latitude / longitude:** open [Google Maps](https://maps.google.com),
right-click your town, and click the two numbers at the top of the menu — that
copies `latitude, longitude`. Paste them in.

Optional — units (defaults are °C and mph):

```cpp
const bool USE_FAHRENHEIT = false;   // true = °F
const bool USE_MPH        = true;    // false = km/h
```

## Step 5 — Upload

1. Plug in the board.
2. **Tools → Board →** select **"ESP32 Dev Module"**.
3. **Tools → Port →** select the port your board shows up on.
4. Click **Upload** (the arrow ➜).

## Step 6 — See the weather

- Open **Tools → Serial Monitor** and set the baud rate to **115200**
  (bottom-right of the Serial Monitor).

The weather for your area is printed there right after it connects, and it
refreshes every 10 minutes. Change how often by editing `UPDATE_INTERVAL_MS`
in the sketch.

---

## How it works

| Part | What it does |
|------|--------------|
| `connectWiFi()` | Joins your WiFi network. |
| `fetchAndPrintWeather()` | Calls the Open-Meteo API for your coordinates and prints the result. |
| `loop()` | Re-runs the fetch every 10 minutes and reconnects WiFi if it drops. |

---

## Troubleshooting

- **WiFi won't connect** — double-check `WIFI_SSID` / `WIFI_PASSWORD`. ESP32 only
  supports **2.4 GHz** WiFi, not 5 GHz. Put it on your 2.4 GHz network.
- **Serial Monitor shows gibberish** — set the baud rate to **115200**.
- **Serial Monitor is blank** — make sure the correct **Port** is selected, and
  press the board's **EN/RST** button once to restart it.
- **Upload fails / "port not found"** — try a different USB cable (some are
  charge-only), and select the right **Port**. On some boards you hold the
  **BOOT** button while it starts uploading.
- **`ArduinoJson.h: No such file`** — you skipped Step 2; install the library.
