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
 *  3. Prints the weather straight to the Arduino IDE Serial Monitor and
 *     refreshes it on a timer. No website, no browser — just the Serial Monitor.
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
 *  5) Click Upload, then open Tools > Serial Monitor at 115200 baud.
 *     The weather for your area is printed there and refreshes automatically.
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
#include <ArduinoJson.h>

// ======================= 1) EDIT YOUR WIFI HERE =============================
const char* WIFI_SSID     = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// ======================= 2) EDIT YOUR LOCATION HERE =========================
// Defaults = London, UK. Replace with your own coordinates (see note above).
const float LATITUDE  = 51.5074;
const float LONGITUDE = -0.1278;
const char* LOCATION_NAME = "London";   // Just a label shown in the printout.

// ======================= 3) EDIT YOUR UNITS (optional) =====================
// Set to true for Fahrenheit / mph, false for Celsius / km-h.
const bool USE_FAHRENHEIT = false;
const bool USE_MPH        = true;

// How often to refresh the weather from the internet (milliseconds).
const unsigned long UPDATE_INTERVAL_MS = 10UL * 60UL * 1000UL;  // 10 minutes

// ===========================================================================
//                    You normally don't need to edit below.
// ===========================================================================

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
    case 56: return "Freezing drizzle";
    case 57: return "Freezing drizzle";
    case 61: return "Light rain";
    case 63: return "Moderate rain";
    case 65: return "Heavy rain";
    case 66: return "Freezing rain";
    case 67: return "Freezing rain";
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
    case 96: return "Thunderstorm with hail";
    case 99: return "Thunderstorm with hail";
    default: return "Unknown";
  }
}

// --- Compass direction from degrees (e.g. 270 -> "W"). ----------------------
const char* compass(int deg) {
  const char* names[] = {"N","NE","E","SE","S","SW","W","NW"};
  return names[(int)((deg + 22.5) / 45.0) % 8];
}

// --- Fetch live weather from Open-Meteo and print it to the Serial Monitor. --
void fetchAndPrintWeather() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[weather] WiFi not connected, skipping update.");
    return;
  }

  // Build the request URL for OUR coordinates and unit choices.
  String url = "https://api.open-meteo.com/v1/forecast";
  url += "?latitude="  + String(LATITUDE, 4);
  url += "&longitude=" + String(LONGITUDE, 4);
  url += "&current=temperature_2m,relative_humidity_2m,apparent_temperature,"
         "precipitation,weather_code,wind_speed_10m,wind_direction_10m";
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
  if (status != 200) {
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

  float  temperature  = cur["temperature_2m"]      | 0.0f;
  float  feelsLike    = cur["apparent_temperature"] | 0.0f;
  float  humidity     = cur["relative_humidity_2m"] | 0.0f;
  float  precipitation= cur["precipitation"]        | 0.0f;
  float  windSpeed    = cur["wind_speed_10m"]       | 0.0f;
  int    windDir      = cur["wind_direction_10m"]   | 0;
  int    weatherCode  = cur["weather_code"]         | -1;
  const char* updated = cur["time"]                 | "unknown";

  const char* tempUnit = USE_FAHRENHEIT ? "F" : "C";
  const char* windUnit = USE_MPH ? "mph" : "km/h";

  // Print a tidy weather report to the Serial Monitor.
  Serial.println();
  Serial.println("========================================");
  Serial.printf("  WEATHER FOR %s\n", LOCATION_NAME);
  Serial.println("========================================");
  Serial.printf("  Conditions : %s\n", describeCode(weatherCode));
  Serial.printf("  Temperature: %.1f %s\n", temperature, tempUnit);
  Serial.printf("  Feels like : %.1f %s\n", feelsLike, tempUnit);
  Serial.printf("  Humidity   : %.0f %%\n", humidity);
  Serial.printf("  Wind       : %.1f %s from %s\n", windSpeed, windUnit, compass(windDir));
  Serial.printf("  Rain       : %.1f mm\n", precipitation);
  Serial.printf("  Updated    : %s\n", updated);
  Serial.println("========================================");
  Serial.println();
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

  fetchAndPrintWeather();  // Show the weather straight away.
  lastFetch = millis();
}

void loop() {
  // Reconnect WiFi if it drops.
  if (WiFi.status() != WL_CONNECTED) {
    static unsigned long lastRetry = 0;
    if (millis() - lastRetry > 15000) { lastRetry = millis(); connectWiFi(); }
  }

  // Refresh the weather on a timer.
  if (millis() - lastFetch >= UPDATE_INTERVAL_MS) {
    fetchAndPrintWeather();
    lastFetch = millis();
  }
}
