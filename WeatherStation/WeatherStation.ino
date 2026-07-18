/*
 * ============================================================================
 *  ESP32 Weather Station  ·  for Keyestudio ESP32 (and most ESP32 dev boards)
 * ============================================================================
 *
 *  What it does
 *  ------------
 *  1. Connects your Keyestudio ESP32 to your WiFi.
 *  2. Fetches the LIVE weather for YOUR area from Open-Meteo
 *     (https://open-meteo.com) — a free API that needs NO account and NO key.
 *  3. Runs a little web "app" on the ESP32 itself. Open the board's IP address
 *     in any phone/laptop browser on the same WiFi and you get a clean weather
 *     dashboard that refreshes on its own.
 *  4. Also prints the weather to the Arduino IDE Serial Monitor.
 *
 *  ---------------------------------------------------------------------------
 *  QUICK START (Arduino IDE)
 *  ---------------------------------------------------------------------------
 *  1) Install the ESP32 boards package:
 *       File > Preferences > "Additional Boards Manager URLs", add:
 *         https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
 *       Then Tools > Board > Boards Manager > search "esp32" > Install.
 *
 *  2) Install the ArduinoJson library (v7 or newer):
 *       Tools > Manage Libraries > search "ArduinoJson" > Install.
 *
 *  3) Select your board:
 *       Tools > Board > "ESP32 Dev Module"  (works for the Keyestudio ESP32).
 *       Pick the correct COM port under Tools > Port.
 *
 *  4) Edit the THREE settings just below (WiFi + your location).
 *
 *  5) Click Upload. Open Tools > Serial Monitor at 115200 baud.
 *     It prints the IP address — type that into a browser to open the app.
 *
 *  ---------------------------------------------------------------------------
 *  FINDING YOUR LATITUDE / LONGITUDE
 *  ---------------------------------------------------------------------------
 *  Easiest way: open Google Maps, right-click your town, and click the numbers
 *  at the top of the menu (that copies "latitude, longitude").
 *  Defaults below point at London, UK — change them to your area.
 * ============================================================================
 */

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <WebServer.h>
#include <ArduinoJson.h>

// ======================= 1) EDIT YOUR WIFI HERE =============================
const char* WIFI_SSID     = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// ======================= 2) EDIT YOUR LOCATION HERE =========================
// Defaults = London, UK. Replace with your own coordinates (see note above).
const float LATITUDE  = 51.5074;
const float LONGITUDE = -0.1278;
const char* LOCATION_NAME = "London";   // Just a label shown in the app.

// ======================= 3) EDIT YOUR UNITS (optional) =====================
// Set to true for Fahrenheit / mph, false for Celsius / km-h.
const bool USE_FAHRENHEIT = false;
const bool USE_MPH        = true;

// How often to refresh the weather from the internet (milliseconds).
const unsigned long UPDATE_INTERVAL_MS = 10UL * 60UL * 1000UL;  // 10 minutes

// ===========================================================================
//                    You normally don't need to edit below.
// ===========================================================================

WebServer server(80);

// Cached weather readings, updated by fetchWeather().
struct Weather {
  bool     valid           = false;
  float    temperature     = 0;
  float    feelsLike        = 0;
  float    humidity        = 0;
  float    precipitation    = 0;
  float    windSpeed        = 0;
  int      windDirection   = 0;
  int      weatherCode      = -1;
  bool     isDay            = true;
  String   lastUpdated      = "never";
} weather;

unsigned long lastFetch = 0;

// --- WMO weather-code lookup: returns a human description + an emoji. -------
struct CodeInfo { const char* text; const char* emoji; };

CodeInfo describeCode(int code, bool isDay) {
  switch (code) {
    case 0:  return { "Clear sky",            isDay ? "☀️" : "\U0001F319" };
    case 1:  return { "Mainly clear",         isDay ? "\U0001F324️" : "\U0001F319" };
    case 2:  return { "Partly cloudy",        "⛅" };
    case 3:  return { "Overcast",             "☁️" };
    case 45: return { "Fog",                  "\U0001F32B️" };
    case 48: return { "Depositing rime fog",  "\U0001F32B️" };
    case 51: return { "Light drizzle",        "\U0001F326️" };
    case 53: return { "Moderate drizzle",     "\U0001F326️" };
    case 55: return { "Dense drizzle",        "\U0001F327️" };
    case 56: return { "Freezing drizzle",     "\U0001F328️" };
    case 57: return { "Freezing drizzle",     "\U0001F328️" };
    case 61: return { "Light rain",           "\U0001F327️" };
    case 63: return { "Moderate rain",        "\U0001F327️" };
    case 65: return { "Heavy rain",           "\U0001F327️" };
    case 66: return { "Freezing rain",        "\U0001F328️" };
    case 67: return { "Freezing rain",        "\U0001F328️" };
    case 71: return { "Light snow",           "\U0001F328️" };
    case 73: return { "Moderate snow",        "\U0001F328️" };
    case 75: return { "Heavy snow",           "❄️" };
    case 77: return { "Snow grains",          "❄️" };
    case 80: return { "Light showers",        "\U0001F326️" };
    case 81: return { "Moderate showers",     "\U0001F327️" };
    case 82: return { "Violent showers",      "⛈️" };
    case 85: return { "Snow showers",         "\U0001F328️" };
    case 86: return { "Heavy snow showers",   "❄️" };
    case 95: return { "Thunderstorm",         "⛈️" };
    case 96: return { "Thunderstorm + hail",  "⛈️" };
    case 99: return { "Thunderstorm + hail",  "⛈️" };
    default: return { "Unknown",              "\U0001F321️" };
  }
}

// --- Fetch live weather from Open-Meteo and update the cache. ---------------
void fetchWeather() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[weather] WiFi not connected, skipping fetch.");
    return;
  }

  // Build the request URL for OUR coordinates and unit choices.
  String url = "https://api.open-meteo.com/v1/forecast";
  url += "?latitude="  + String(LATITUDE, 4);
  url += "&longitude=" + String(LONGITUDE, 4);
  url += "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
         "is_day,precipitation,weather_code,wind_speed_10m,wind_direction_10m";
  url += "&timezone=auto";
  url += USE_FAHRENHEIT ? "&temperature_unit=fahrenheit" : "&temperature_unit=celsius";
  url += USE_MPH        ? "&wind_speed_unit=mph"         : "&wind_speed_unit=kmh";

  WiFiClientSecure client;
  client.setInsecure();  // Skip TLS certificate validation (fine for this use).

  HTTPClient https;
  https.setTimeout(10000);
  Serial.println("[weather] Requesting update...");

  if (!https.begin(client, url)) {
    Serial.println("[weather] Unable to start HTTPS request.");
    return;
  }

  int status = https.GET();
  if (status != HTTP_OK && status != 200) {
    Serial.printf("[weather] HTTP error: %d\n", status);
    https.end();
    return;
  }

  String payload = https.getString();
  https.end();

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, payload);
  if (err) {
    Serial.print("[weather] JSON parse failed: ");
    Serial.println(err.c_str());
    return;
  }

  JsonObject cur = doc["current"];
  if (cur.isNull()) {
    Serial.println("[weather] Response missing 'current' block.");
    return;
  }

  weather.temperature   = cur["temperature_2m"]      | 0.0f;
  weather.feelsLike       = cur["apparent_temperature"] | 0.0f;
  weather.humidity      = cur["relative_humidity_2m"] | 0.0f;
  weather.precipitation   = cur["precipitation"]        | 0.0f;
  weather.windSpeed       = cur["wind_speed_10m"]       | 0.0f;
  weather.windDirection = cur["wind_direction_10m"]   | 0;
  weather.weatherCode     = cur["weather_code"]         | -1;
  weather.isDay          = (int)(cur["is_day"] | 1) == 1;
  weather.lastUpdated    = String((const char*)(cur["time"] | "unknown"));
  weather.valid          = true;

  CodeInfo info = describeCode(weather.weatherCode, weather.isDay);
  Serial.println("------------------------------------------------");
  Serial.printf("[weather] %s, %s\n", LOCATION_NAME, info.text);
  Serial.printf("[weather] Temp: %.1f%s (feels %.1f%s)\n",
                weather.temperature, USE_FAHRENHEIT ? "F" : "C",
                weather.feelsLike,    USE_FAHRENHEIT ? "F" : "C");
  Serial.printf("[weather] Humidity: %.0f%%  Wind: %.1f %s\n",
                weather.humidity, weather.windSpeed, USE_MPH ? "mph" : "km/h");
  Serial.printf("[weather] Updated: %s\n", weather.lastUpdated.c_str());
  Serial.println("------------------------------------------------");
}

// --- Compass direction from degrees (e.g. 270 -> "W"). ----------------------
String compass(int deg) {
  const char* names[] = {"N","NE","E","SE","S","SW","W","NW"};
  return names[(int)((deg + 22.5) / 45.0) % 8];
}

// --- /api/weather : returns the cached readings as JSON for the web app. -----
void handleApi() {
  JsonDocument doc;
  CodeInfo info = describeCode(weather.weatherCode, weather.isDay);

  doc["valid"]        = weather.valid;
  doc["location"]     = LOCATION_NAME;
  doc["description"]  = info.text;
  doc["emoji"]        = info.emoji;
  doc["temperature"]  = weather.temperature;
  doc["feelsLike"]    = weather.feelsLike;
  doc["humidity"]     = weather.humidity;
  doc["precipitation"]= weather.precipitation;
  doc["windSpeed"]    = weather.windSpeed;
  doc["windDir"]      = compass(weather.windDirection);
  doc["isDay"]        = weather.isDay;
  doc["updated"]      = weather.lastUpdated;
  doc["tempUnit"]     = USE_FAHRENHEIT ? "°F" : "°C";
  doc["windUnit"]     = USE_MPH ? "mph" : "km/h";

  String out;
  serializeJson(doc, out);
  server.send(200, "application/json", out);
}

// --- The web "app" (a single self-contained HTML page). ---------------------
const char INDEX_HTML[] PROGMEM = R"HTML(
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ESP32 Weather Station</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    min-height: 100vh; display: grid; place-items: center;
    background: linear-gradient(160deg, #1e3a5f 0%, #0b1622 100%);
    color: #f2f6fa; padding: 20px;
  }
  .card {
    width: 100%; max-width: 420px; background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.12); border-radius: 24px;
    padding: 32px 28px; backdrop-filter: blur(8px);
    box-shadow: 0 20px 60px rgba(0,0,0,0.4); text-align: center;
  }
  .place { font-size: 1.1rem; letter-spacing: .5px; opacity: .8; }
  .emoji { font-size: 5rem; line-height: 1; margin: 10px 0; }
  .temp { font-size: 4rem; font-weight: 700; }
  .desc { font-size: 1.25rem; margin-top: 4px; opacity: .95; }
  .grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 26px;
  }
  .tile {
    background: rgba(255,255,255,0.05); border-radius: 14px; padding: 14px 10px;
  }
  .tile .label { font-size: .72rem; text-transform: uppercase; opacity: .6; letter-spacing: .6px; }
  .tile .value { font-size: 1.35rem; font-weight: 600; margin-top: 4px; }
  .foot { margin-top: 22px; font-size: .72rem; opacity: .5; }
  .loading { opacity: .6; }
</style>
</head>
<body>
  <div class="card">
    <div class="place" id="place">Loading…</div>
    <div class="emoji" id="emoji">…</div>
    <div class="temp" id="temp">--</div>
    <div class="desc" id="desc" class="loading">Fetching your weather</div>
    <div class="grid">
      <div class="tile"><div class="label">Feels like</div><div class="value" id="feels">--</div></div>
      <div class="tile"><div class="label">Humidity</div><div class="value" id="humidity">--</div></div>
      <div class="tile"><div class="label">Wind</div><div class="value" id="wind">--</div></div>
      <div class="tile"><div class="label">Rain</div><div class="value" id="rain">--</div></div>
    </div>
    <div class="foot" id="foot">Powered by Open-Meteo · updates every few minutes</div>
  </div>

<script>
async function load() {
  try {
    const r = await fetch('/api/weather');
    const d = await r.json();
    if (!d.valid) { document.getElementById('desc').textContent = 'Waiting for first reading…'; return; }
    document.getElementById('place').textContent    = d.location;
    document.getElementById('emoji').textContent    = d.emoji;
    document.getElementById('temp').textContent     = Math.round(d.temperature) + d.tempUnit;
    document.getElementById('desc').textContent     = d.description;
    document.getElementById('feels').textContent    = Math.round(d.feelsLike) + d.tempUnit;
    document.getElementById('humidity').textContent = Math.round(d.humidity) + '%';
    document.getElementById('wind').textContent     = Math.round(d.windSpeed) + ' ' + d.windUnit + ' ' + d.windDir;
    document.getElementById('rain').textContent     = d.precipitation.toFixed(1) + ' mm';
    document.getElementById('foot').textContent     = 'Open-Meteo · station updated ' + d.updated;
  } catch (e) {
    document.getElementById('desc').textContent = 'Could not reach the station.';
  }
}
load();
setInterval(load, 60000);   // refresh the page data every 60 seconds
</script>
</body>
</html>
)HTML";

void handleRoot() {
  server.send_P(200, "text/html", INDEX_HTML);
}

void connectWiFi() {
  Serial.printf("\n[wifi] Connecting to \"%s\" ", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 30000) {
    delay(500);
    Serial.print(".");
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println(" connected!");
    Serial.print("[wifi] IP address: ");
    Serial.println(WiFi.localIP());
    Serial.println("[wifi] Open that address in a browser to see your weather app.");
  } else {
    Serial.println(" FAILED.");
    Serial.println("[wifi] Check WIFI_SSID / WIFI_PASSWORD and reset the board.");
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=== ESP32 Weather Station starting ===");

  connectWiFi();

  server.on("/", handleRoot);
  server.on("/api/weather", handleApi);
  server.onNotFound([]() { server.send(404, "text/plain", "Not found"); });
  server.begin();
  Serial.println("[web] Web app is running on port 80.");

  fetchWeather();          // Get the first reading immediately.
  lastFetch = millis();
}

void loop() {
  server.handleClient();   // Serve the web app to any browser that connects.

  // Reconnect WiFi if it drops.
  if (WiFi.status() != WL_CONNECTED) {
    static unsigned long lastRetry = 0;
    if (millis() - lastRetry > 15000) { lastRetry = millis(); connectWiFi(); }
  }

  // Refresh the weather on a timer.
  if (millis() - lastFetch >= UPDATE_INTERVAL_MS) {
    fetchWeather();
    lastFetch = millis();
  }
}
