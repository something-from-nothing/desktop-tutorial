/*
 * ============================================================================
 *  Raspberry Pi Pico W  ·  Weather Station  (WiFi text output, no sensors)
 * ============================================================================
 *  Fetches LIVE weather for your area from Open-Meteo (free, no API key) and
 *  transmits it as plain text back to your computer over WiFi on port 8080.
 *  No screen and no DHT/temperature sensor needed - the weather comes from
 *  the internet.
 *
 *  ---------------------------------------------------------------------------
 *  ARDUINO IDE SETUP (one time)
 *  ---------------------------------------------------------------------------
 *  1) File > Preferences > "Additional Boards Manager URLs", add:
 *       https://github.com/earlephilhower/arduino-pico/releases/download/global/package_rp2040_index.json
 *     Then Tools > Board > Boards Manager > search "pico" > Install
 *     ("Raspberry Pi Pico/RP2040/RP2350" by Earle Philhower).
 *  2) Tools > Manage Libraries > search "ArduinoJson" > Install (v7 or newer).
 *  3) Tools > Board > "Raspberry Pi Pico W".
 *  4) Edit the WiFi + location settings below.
 *  5) Hold the BOOTSEL button while plugging in the USB the first time, then
 *     click Upload. After that it uploads normally.
 *  6) Open Tools > Serial Monitor @ 115200 baud ONCE to read the IP address.
 *
 *  ---------------------------------------------------------------------------
 *  VIEW IT ON YOUR COMPUTER (over WiFi - no browser, no screen)
 *  ---------------------------------------------------------------------------
 *  - Windows: open PuTTY, Connection type = "Raw", Host = the Pico's IP,
 *             Port = 8080, click Open.
 *  - Mac / Linux: open a terminal and run:   nc <pico-ip> 8080
 *  The weather report streams in and refreshes every 10 minutes.
 *
 *  Find your latitude/longitude: right-click your town in Google Maps and
 *  click the numbers at the top of the menu.
 * ============================================================================
 */

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// ======================= 1) EDIT YOUR WIFI HERE =============================
const char* WIFI_SSID     = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// ======================= 2) EDIT YOUR LOCATION HERE =========================
const float LATITUDE  = 51.5074;        // Default = London, UK
const float LONGITUDE = -0.1278;
const char* LOCATION_NAME = "London";

// ======================= 3) EDIT YOUR UNITS (optional) =====================
const bool USE_FAHRENHEIT = false;      // true = Fahrenheit, false = Celsius
const bool USE_MPH        = true;       // true = mph,        false = km/h

const uint16_t      TCP_PORT           = 8080;
const unsigned long UPDATE_INTERVAL_MS = 10UL * 60UL * 1000UL;  // 10 minutes

// ===========================================================================
//                    You normally don't need to edit below.
// ===========================================================================
WiFiServer server(TCP_PORT);
WiFiClient client;
String     lastReport = "Waiting for first weather update...\n";
unsigned long lastFetch = 0;

// --- WMO weather-code lookup: returns a human description. ------------------
const char* describeCode(int code) {
  switch (code) {
    case 0:  return "Clear sky";
    case 1:  return "Mainly clear";
    case 2:  return "Partly cloudy";
    case 3:  return "Overcast";
    case 45: return "Fog";
    case 48: return "Depositing rime fog";
    case 51: return "Light drizzle";
    case 53: return "Moderate drizzle";
    case 55: return "Dense drizzle";
    case 56: case 57: return "Freezing drizzle";
    case 61: return "Light rain";
    case 63: return "Moderate rain";
    case 65: return "Heavy rain";
    case 66: case 67: return "Freezing rain";
    case 71: return "Light snow";
    case 73: return "Moderate snow";
    case 75: return "Heavy snow";
    case 77: return "Snow grains";
    case 80: return "Light showers";
    case 81: return "Moderate showers";
    case 82: return "Violent showers";
    case 85: return "Snow showers";
    case 86: return "Heavy snow showers";
    case 95: return "Thunderstorm";
    case 96: case 99: return "Thunderstorm with hail";
    default: return "Unknown";
  }
}

// --- Compass direction from degrees (e.g. 270 -> "W"). ----------------------
const char* compass(int deg) {
  const char* names[] = {"N","NE","E","SE","S","SW","W","NW"};
  return names[(int)((deg + 22.5) / 45.0) % 8];
}

// --- Send text to Serial (if USB plugged in) and the connected WiFi client. -
void broadcast(const String& text) {
  Serial.print(text);
  if (client && client.connected()) client.print(text);
}

// --- Fetch live weather from Open-Meteo, format it, and transmit it. --------
void fetchWeather() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[weather] WiFi not connected, skipping update.");
    return;
  }

  String url = "https://api.open-meteo.com/v1/forecast";
  url += "?latitude="  + String(LATITUDE, 4);
  url += "&longitude=" + String(LONGITUDE, 4);
  url += "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
         "precipitation,weather_code,wind_speed_10m,wind_direction_10m";
  url += "&timezone=auto";
  url += USE_FAHRENHEIT ? "&temperature_unit=fahrenheit" : "&temperature_unit=celsius";
  url += USE_MPH        ? "&wind_speed_unit=mph"         : "&wind_speed_unit=kmh";

  WiFiClientSecure secure;
  secure.setInsecure();  // Skip TLS certificate validation (fine for this use).

  HTTPClient https;
  https.setTimeout(10000);
  Serial.println("[weather] Requesting update...");

  if (!https.begin(secure, url)) {
    Serial.println("[weather] Unable to start HTTPS request.");
    return;
  }

  int status = https.GET();
  if (status != 200) {
    Serial.printf("[weather] HTTP error: %d\n", status);
    https.end();
    return;
  }

  String payload = https.getString();
  https.end();

  JsonDocument doc;
  if (deserializeJson(doc, payload)) {
    Serial.println("[weather] JSON parse failed.");
    return;
  }

  JsonObject cur = doc["current"];
  if (cur.isNull()) { Serial.println("[weather] No 'current' block."); return; }

  float  temperature   = cur["temperature_2m"]       | 0.0f;
  float  feelsLike     = cur["apparent_temperature"] | 0.0f;
  float  humidity      = cur["relative_humidity_2m"] | 0.0f;
  float  precipitation = cur["precipitation"]        | 0.0f;
  float  windSpeed     = cur["wind_speed_10m"]       | 0.0f;
  int    windDir       = cur["wind_direction_10m"]   | 0;
  int    weatherCode   = cur["weather_code"]         | -1;
  const char* updated  = cur["time"]                 | "unknown";
  const char* tempUnit = USE_FAHRENHEIT ? "F" : "C";
  const char* windUnit = USE_MPH ? "mph" : "km/h";

  char buf[512];
  snprintf(buf, sizeof(buf),
    "\n========================================\n"
    "  WEATHER FOR %s\n"
    "========================================\n"
    "  Conditions : %s\n"
    "  Temperature: %.1f %s\n"
    "  Feels like : %.1f %s\n"
    "  Humidity   : %.0f %%\n"
    "  Wind       : %.1f %s from %s\n"
    "  Rain       : %.1f mm\n"
    "  Updated    : %s\n"
    "========================================\n\n",
    LOCATION_NAME, describeCode(weatherCode), temperature, tempUnit,
    feelsLike, tempUnit, humidity, windSpeed, windUnit, compass(windDir),
    precipitation, updated);

  lastReport = String(buf);
  broadcast(lastReport);
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
    Serial.printf("[wifi] Connect from your PC:  nc %s %u   (or PuTTY Raw, port %u)\n",
                  WiFi.localIP().toString().c_str(), TCP_PORT, TCP_PORT);
  } else {
    Serial.println(" FAILED.");
    Serial.println("[wifi] Check WIFI_SSID / WIFI_PASSWORD (2.4 GHz only) and reset.");
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n=== Pico W Weather Station (WiFi text) starting ===");

  connectWiFi();
  server.begin();
  Serial.printf("[net] Text server listening on port %u.\n", TCP_PORT);

  fetchWeather();          // Get the first reading immediately.
  lastFetch = millis();
}

void loop() {
  // Accept a new computer connection and immediately send the latest report.
  if (!client || !client.connected()) {
    WiFiClient incoming = server.accept();
    if (incoming) {
      client = incoming;
      client.print("Connected to Pico W Weather Station.\n");
      client.print(lastReport);
    }
  }

  // Reconnect WiFi if it drops.
  if (WiFi.status() != WL_CONNECTED) {
    static unsigned long lastRetry = 0;
    if (millis() - lastRetry > 15000) { lastRetry = millis(); connectWiFi(); }
  }

  // Refresh the weather on a timer and push it out over WiFi.
  if (millis() - lastFetch >= UPDATE_INTERVAL_MS) {
    fetchWeather();
    lastFetch = millis();
  }
}
