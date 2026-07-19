# Pico W Weather Station 🌦️ (WiFi → computer, no sensors)

A weather station for the **Raspberry Pi Pico W**, written for the **Arduino IDE**.
It pulls the **live weather for your area** from the internet and **transmits it
back to your computer over WiFi** as plain text. No screen, and **no DHT /
temperature sensor** — the weather comes from [Open-Meteo](https://open-meteo.com),
a **free** API that needs **no account and no API key**.

You view it on your computer with a terminal program — like the Serial Monitor,
but wireless.

Example output:

```
Connected to Pico W Weather Station.
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

- A **Raspberry Pi Pico W** (or **Pico 2 W**) + **Micro-USB data cable**
- The **Arduino IDE** (free): <https://www.arduino.cc/en/software>
- Your home **2.4 GHz WiFi** name and password
- On the computer: **PuTTY** (Windows) or the built-in `nc` / `telnet` (Mac/Linux)

*(No breadboard, sensors or wiring — the Pico W just plugs into USB.)*

---

## Step 1 — Add Pico support to the Arduino IDE

1. **File → Preferences → "Additional Boards Manager URLs"**, add:
   ```
   https://github.com/earlephilhower/arduino-pico/releases/download/global/package_rp2040_index.json
   ```
2. **Tools → Board → Boards Manager**, search **`pico`**, install
   **"Raspberry Pi Pico/RP2040/RP2350" by Earle Philhower**.

## Step 2 — Install the ArduinoJson library

- **Tools → Manage Libraries…**, search **`ArduinoJson`**, install it (v7+).

## Step 3 — Open the sketch and enter your details

Open **`WeatherStation.ino`** and edit:

```cpp
const char* WIFI_SSID     = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

const float LATITUDE  = 51.5074;     // <- your latitude
const float LONGITUDE = -0.1278;     // <- your longitude
const char* LOCATION_NAME = "London";
```

Right-click your town in [Google Maps](https://maps.google.com) and click the
numbers at the top to copy your latitude/longitude.

## Step 4 — Upload

1. **Tools → Board →** select **"Raspberry Pi Pico W"**.
2. **Hold the BOOTSEL button** while plugging in the USB (first upload only),
   then release it.
3. **Tools → Port →** select the port, and click **Upload**.

## Step 5 — Get the IP address (one time)

- Open **Tools → Serial Monitor** at **115200 baud**. It prints something like:
  ```
  [wifi] IP address: 192.168.1.55
  [wifi] Connect from your PC:  nc 192.168.1.55 8080   (or PuTTY Raw, port 8080)
  ```
- Note that IP. (You can also find the Pico in your router's device list.)

## Step 6 — Receive the weather over WiFi

- **Windows:** open **PuTTY** → Connection type **Raw** → Host = the Pico's IP →
  Port **8080** → **Open**.
- **Mac / Linux:** open a terminal and run:
  ```
  nc 192.168.1.55 8080
  ```

The latest weather appears immediately, then refreshes every 10 minutes. Once
this works you don't need the USB cable — the Pico can run from any USB charger.

---

## How it works

| Part | What it does |
|------|--------------|
| `connectWiFi()` | Joins your 2.4 GHz WiFi. |
| `fetchWeather()` | Calls the Open-Meteo API for your coordinates and formats the report. |
| `WiFiServer` on port 8080 | Lets your computer connect and receive the text. |
| `broadcast()` | Sends each update to the connected computer (and USB Serial). |

Change how often it refreshes by editing `UPDATE_INTERVAL_MS`; change the port
with `TCP_PORT`.

---

## Troubleshooting

- **WiFi won't connect** — check `WIFI_SSID` / `WIFI_PASSWORD` (case-sensitive).
  The Pico W is **2.4 GHz only**, not 5 GHz.
- **Can't connect from the computer** — make sure the PC is on the **same WiFi**,
  the IP is current, and you used port **8080**. Some firewalls block outgoing
  connections — allow it if prompted.
- **`nc` not found on Windows** — use **PuTTY** in **Raw** mode instead.
- **Only one computer connects at a time** — that's by design to keep it simple.
- **`ArduinoJson.h: No such file`** — install the library (Step 2).
```
