# 🌤️ Keyestudio ESP32 Weather Station

A little weather station built around a **Keyestudio ESP32** board, plus a
companion **web app** that tells you the weather in your area.

There are two parts, and you can use either on its own:

| Part | What it is | Where it lives |
| --- | --- | --- |
| **Firmware** | A C++ (Arduino) sketch for the ESP32. Reads the on-board sensor, pulls the live forecast for your area, shows it on the OLED, and serves its own web dashboard over Wi-Fi. | [`firmware/weather_station/weather_station.ino`](firmware/weather_station/weather_station.ino) |
| **Web app** | A self-contained page that shows the weather in your area using your browser's location. No hardware needed. | [`app/index.html`](app/index.html) |

Both use the free [Open-Meteo](https://open-meteo.com) API — **no API key or sign-up required.**

---

## 1. The web app (try this first — no hardware)

The quickest way to see it working:

1. Download or open [`app/index.html`](app/index.html).
2. **Double-click it** to open in any browser (Chrome, Safari, Edge, Firefox).
3. Allow the location prompt, or type your town/city into the search box.

You'll get the current conditions, the next 24 hours, and a 7-day forecast,
on a background that changes with the sky and time of day.

> **Note:** the page needs internet access (it calls the Open-Meteo weather
> service directly from your browser). Because it makes live network calls, it
> is meant to be opened as a local file or hosted on your own web server — it
> is intentionally **not** published as a locked-down preview, since those
> block outbound requests. Hosting it on GitHub Pages works great.

### Host it free on GitHub Pages (optional)

1. Push this repo to GitHub.
2. Repo **Settings → Pages → Build from branch**, pick your branch, folder `/ (root)`.
3. Visit `https://<you>.github.io/<repo>/app/`.

---

## 2. The ESP32 firmware

### What you need

- A **Keyestudio ESP32** board (the common weather / environment-monitoring kit)
- A **BME280** sensor (temperature / humidity / pressure) on the I²C bus
- A **0.96" SSD1306 OLED** (128×64) on the I²C bus
- A USB cable and the free **Arduino IDE**

> Different Keyestudio kits ship slightly different parts. If yours has a
> **DHT11** instead of a BME280, the sketch has a clearly-marked
> `DHT11 ALTERNATIVE` block explaining the swap.

### Wiring (I²C — the default on Keyestudio ESP32)

| Sensor / OLED pin | ESP32 pin |
| --- | --- |
| SDA | GPIO **21** |
| SCL | GPIO **22** |
| VCC | **3V3** |
| GND | **GND** |

The OLED and BME280 share the same SDA/SCL bus — just wire both to 21/22.

### Arduino IDE setup

1. **Add the ESP32 boards.** `File → Preferences → Additional Boards Manager URLs`, add:
   ```
   https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
   ```
   Then `Tools → Board → Boards Manager` → install **esp32**.
2. **Install these libraries** (`Tools → Manage Libraries`):
   - Adafruit BME280 Library
   - Adafruit Unified Sensor
   - Adafruit SSD1306
   - Adafruit GFX Library
   - ArduinoJson (v6+)
3. **Select the board:** `Tools → Board → ESP32 Dev Module`.

### Configure and upload

Open [`weather_station.ino`](firmware/weather_station/weather_station.ino) and
edit the **CONFIG** block near the top:

```cpp
const char* WIFI_SSID     = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

const char* LOCATION_NAME = "London";     // your area's name
const float LATITUDE      = 51.5074;      // your latitude
const float LONGITUDE     = -0.1278;      // your longitude

const char* TEMP_UNIT = "celsius";        // or "fahrenheit"
```

> **Finding your latitude/longitude:** open Google Maps, right-click your area,
> and click the coordinates at the top of the menu to copy them.

Then press **Upload**. Open `Tools → Serial Monitor` at **115200 baud** — once
it connects to Wi-Fi it prints a line like:

```
Connected. Open the app at:  http://192.168.1.42
```

### Using it

- **On the OLED:** the outdoor temperature and conditions for your area, wind
  and humidity, plus your indoor sensor reading. It refreshes every 10 minutes.
- **In a browser:** go to the IP address printed in the Serial Monitor (any
  phone or laptop on the same Wi-Fi). You get a small dashboard with both the
  outdoor forecast and the indoor readings, auto-refreshing every 60 seconds.

---

## How it works

```
                 ┌─────────────────────┐
   Open-Meteo ──▶│  ESP32 (C++ sketch) │──▶ OLED display
  (your area's   │                     │
   forecast)     │  BME280 sensor ────▶│──▶ Web dashboard (Wi-Fi)
                 └─────────────────────┘

        app/index.html ──▶ browser geolocation ──▶ Open-Meteo ──▶ your forecast
```

- **Outdoor** data comes from Open-Meteo for the latitude/longitude you set.
- **Indoor** data comes from the BME280 on the board.
- WMO weather codes from the API are translated into friendly labels
  ("Partly cloudy", "Rain showers", …) in both the firmware and the web app.

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| OLED stays blank | Try changing `OLED_ADDRESS` from `0x3C` to `0x3D`. Check SDA/SCL wiring. |
| "BME280 not found" | The sketch tries `0x76` and `0x77` automatically — re-check wiring and 3V3 power. |
| No IP / Wi-Fi fails | Double-check SSID/password. The ESP32 needs a **2.4 GHz** network. |
| Outdoor shows "no data" | Confirm the board has internet and your lat/lon are set. |
| Web app won't load data | It needs internet; open it as a file or via GitHub Pages, not a sandboxed preview. |

---

Built with [Open-Meteo](https://open-meteo.com). Have fun. ⛅
