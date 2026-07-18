# ESP32 Weather Station 🌦️

A weather station for the **Keyestudio ESP32** (works on most ESP32 dev boards).
It pulls the **live weather for your area** from the internet and shows it in a
small **web app** that you open in any browser on your WiFi — plus it prints to
the Arduino IDE Serial Monitor.

No sensors and no soldering are required — the weather comes from
[Open-Meteo](https://open-meteo.com), a **free** API that needs **no account and
no API key**.

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
const char* LOCATION_NAME = "London";// <- a label for the app
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

## Step 6 — Open the app

1. Open **Tools → Serial Monitor** and set the baud rate to **115200**
   (bottom-right of the Serial Monitor).
2. Wait for it to connect. It prints something like:
   ```
   [wifi] IP address: 192.168.1.42
   ```
3. Type that IP address into a browser (phone or laptop on the same WiFi):
   **`http://192.168.1.42`**

You'll see your local weather — temperature, "feels like", humidity, wind and
rain — and it refreshes on its own. The Serial Monitor also prints each update.

---

## How it works

| Part | What it does |
|------|--------------|
| `connectWiFi()` | Joins your WiFi network. |
| `fetchWeather()` | Every 10 minutes, calls the Open-Meteo API for your coordinates and caches the result. |
| `WebServer` on port 80 | Serves the web app at `/` and the raw data at `/api/weather`. |
| The HTML page | Reads `/api/weather` and draws the dashboard; re-checks every 60 seconds. |

Change how often it fetches by editing `UPDATE_INTERVAL_MS` in the sketch.

---

## Troubleshooting

- **WiFi won't connect** — double-check `WIFI_SSID` / `WIFI_PASSWORD`. ESP32 only
  supports **2.4 GHz** WiFi, not 5 GHz. Put it on your 2.4 GHz network.
- **Serial Monitor shows gibberish** — set the baud rate to **115200**.
- **Upload fails / "port not found"** — try a different USB cable (some are
  charge-only), and select the right **Port**. On some boards you hold the
  **BOOT** button while it starts uploading.
- **`ArduinoJson.h: No such file`** — you skipped Step 2; install the library.
- **App won't open in browser** — make sure your phone/laptop is on the **same
  WiFi** as the ESP32, and use `http://` (not `https://`).

---

Made to run entirely on the ESP32 — the board is both the weather fetcher and
the web server, so nothing else needs to stay running.
