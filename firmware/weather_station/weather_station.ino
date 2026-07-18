/*
 * ============================================================================
 *  Keyestudio ESP32 Weather Station
 * ============================================================================
 *  What it does
 *  ------------
 *   1. Connects the ESP32 to your Wi-Fi.
 *   2. Reads the on-board sensor (indoor temperature / humidity / pressure).
 *   3. Fetches the LIVE outdoor weather for YOUR AREA from the free
 *      Open-Meteo API (no API key or sign-up required).
 *   4. Shows everything on the 0.96" OLED display.
 *   5. Also serves a small web page (the "app") on your Wi-Fi so you can
 *      open the weather station from your phone or laptop browser.
 *
 *  Hardware (typical Keyestudio ESP32 weather / environment kit)
 *  ------------------------------------------------------------
 *   - Keyestudio ESP32 board
 *   - BME280 sensor (temp / humidity / pressure) on the I2C bus
 *   - SSD1306 0.96" OLED (128x64) on the I2C bus
 *
 *   I2C wiring (default on most Keyestudio ESP32 kits):
 *       SDA -> GPIO 21
 *       SCL -> GPIO 22
 *       VCC -> 3V3
 *       GND -> GND
 *
 *   > If your kit uses a DHT11 instead of a BME280, see the clearly marked
 *   > "DHT11 ALTERNATIVE" block near the top of loop() below.
 *
 *  Arduino IDE setup
 *  -----------------
 *   1. Install the ESP32 board package:
 *        File > Preferences > Additional Boards Manager URLs, add:
 *          https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
 *        Then Tools > Board > Boards Manager > install "esp32".
 *   2. Install these libraries (Tools > Manage Libraries):
 *        - "Adafruit BME280 Library"   (by Adafruit)
 *        - "Adafruit Unified Sensor"   (dependency of the above)
 *        - "Adafruit SSD1306"          (by Adafruit)
 *        - "Adafruit GFX Library"      (dependency of the above)
 *        - "ArduinoJson"               (by Benoit Blanchon, v6+)
 *   3. Select your board: Tools > Board > "ESP32 Dev Module"
 *      (or the exact Keyestudio ESP32 entry if present).
 *   4. Fill in the CONFIG section below, then Upload.
 *
 *  After upload, open the Serial Monitor at 115200 baud to see the IP
 *  address, then browse to http://<that-ip>/ from any device on the
 *  same network.
 * ============================================================================
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <ArduinoJson.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>
#include <Adafruit_SSD1306.h>

// ============================================================================
//  CONFIG  --  EDIT THESE
// ============================================================================

// --- Your Wi-Fi ---
const char* WIFI_SSID     = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// --- Your area (for the outdoor forecast) ---
// Look up your latitude/longitude (e.g. on Google Maps: right-click a spot).
// The defaults below point at London, UK -- change them to your area.
const char* LOCATION_NAME = "London";
const float LATITUDE      = 51.5074;
const float LONGITUDE     = -0.1278;

// Units: "celsius" or "fahrenheit"
const char* TEMP_UNIT = "celsius";

// How often to refresh the outdoor forecast, in milliseconds (10 min default).
const unsigned long WEATHER_REFRESH_MS = 10UL * 60UL * 1000UL;

// ============================================================================
//  Hardware setup
// ============================================================================
#define SCREEN_WIDTH   128
#define SCREEN_HEIGHT  64
#define OLED_RESET     -1
#define OLED_ADDRESS   0x3C   // Common SSD1306 address; try 0x3D if blank.

Adafruit_SSD1306 display(SCREEN_WIDTH, SCREEN_HEIGHT, &Wire, OLED_RESET);
Adafruit_BME280  bme;
bool bmeReady   = false;
bool oledReady  = false;

WiFiServer server(80);

// ============================================================================
//  State
// ============================================================================
struct OutdoorWeather {
  bool  valid       = false;
  float temperature = 0;
  float windSpeed   = 0;
  int   humidity    = 0;
  int   weatherCode = -1;
  String description = "--";
};
OutdoorWeather outdoor;

unsigned long lastWeatherFetch = 0;

// Human-readable text for Open-Meteo WMO weather codes.
String describeWeatherCode(int code) {
  switch (code) {
    case 0:  return "Clear sky";
    case 1:  return "Mainly clear";
    case 2:  return "Partly cloudy";
    case 3:  return "Overcast";
    case 45:
    case 48: return "Fog";
    case 51:
    case 53:
    case 55: return "Drizzle";
    case 56:
    case 57: return "Freezing drizzle";
    case 61:
    case 63:
    case 65: return "Rain";
    case 66:
    case 67: return "Freezing rain";
    case 71:
    case 73:
    case 75: return "Snow";
    case 77: return "Snow grains";
    case 80:
    case 81:
    case 82: return "Rain showers";
    case 85:
    case 86: return "Snow showers";
    case 95: return "Thunderstorm";
    case 96:
    case 99: return "Thunderstorm + hail";
    default: return "Unknown";
  }
}

// ============================================================================
//  Setup
// ============================================================================
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("Keyestudio ESP32 Weather Station starting...");

  Wire.begin();  // Default SDA=21, SCL=22 on Keyestudio ESP32.

  // --- OLED ---
  if (display.begin(SSD1306_SWITCHCAPVCC, OLED_ADDRESS)) {
    oledReady = true;
    display.clearDisplay();
    display.setTextColor(SSD1306_WHITE);
    display.setTextSize(1);
    display.setCursor(0, 0);
    display.println("Weather Station");
    display.println("Booting...");
    display.display();
  } else {
    Serial.println("OLED not found (check address / wiring). Continuing.");
  }

  // --- BME280 sensor ---
  // Most Keyestudio boards use 0x76; some use 0x77.
  if (bme.begin(0x76) || bme.begin(0x77)) {
    bmeReady = true;
    Serial.println("BME280 sensor ready.");
  } else {
    Serial.println("BME280 not found (check wiring / address). Continuing.");
  }

  connectWiFi();
  server.begin();

  // First forecast pull right away.
  fetchOutdoorWeather();
  updateDisplay();
}

// ============================================================================
//  Main loop
// ============================================================================
void loop() {
  // Reconnect Wi-Fi if it dropped.
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  // Refresh the outdoor forecast on schedule.
  if (millis() - lastWeatherFetch >= WEATHER_REFRESH_MS) {
    fetchOutdoorWeather();
    updateDisplay();
  }

  // Serve any browser that connects (the "app").
  handleWebClient();

  // -------------------------------------------------------------------------
  //  DHT11 ALTERNATIVE
  //  If your kit has a DHT11 instead of a BME280:
  //    1. Install the "DHT sensor library" by Adafruit.
  //    2. #include <DHT.h> at the top.
  //    3. Replace readIndoor*() below with DHT reads. Example:
  //         #define DHTPIN 4
  //         #define DHTTYPE DHT11
  //         DHT dht(DHTPIN, DHTTYPE);
  //         // in setup(): dht.begin();
  //         float t = dht.readTemperature();
  //         float h = dht.readHumidity();
  // -------------------------------------------------------------------------

  delay(50);
}

// ============================================================================
//  Wi-Fi
// ============================================================================
void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;

  Serial.print("Connecting to Wi-Fi \"");
  Serial.print(WIFI_SSID);
  Serial.print("\" ");

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int tries = 0;
  while (WiFi.status() != WL_CONNECTED && tries < 40) {
    delay(500);
    Serial.print(".");
    tries++;
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("Connected. Open the app at:  http://");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("Wi-Fi failed. Will keep retrying.");
  }
}

// ============================================================================
//  Indoor sensor reads
// ============================================================================
float readIndoorTemperature() {
  if (!bmeReady) return NAN;
  float t = bme.readTemperature();                 // Celsius
  if (strcmp(TEMP_UNIT, "fahrenheit") == 0) t = t * 9.0 / 5.0 + 32.0;
  return t;
}
float readIndoorHumidity() {
  if (!bmeReady) return NAN;
  return bme.readHumidity();                        // %
}
float readIndoorPressure() {
  if (!bmeReady) return NAN;
  return bme.readPressure() / 100.0F;               // hPa
}

// ============================================================================
//  Outdoor forecast from Open-Meteo (free, no API key)
// ============================================================================
void fetchOutdoorWeather() {
  lastWeatherFetch = millis();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Skipping forecast fetch: no Wi-Fi.");
    return;
  }

  String url = "https://api.open-meteo.com/v1/forecast?latitude=" +
               String(LATITUDE, 4) + "&longitude=" + String(LONGITUDE, 4) +
               "&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m" +
               "&temperature_unit=" + String(TEMP_UNIT);

  HTTPClient http;
  http.begin(url);
  int code = http.GET();

  if (code == 200) {
    String payload = http.getString();
    StaticJsonDocument<1024> doc;
    DeserializationError err = deserializeJson(doc, payload);
    if (!err) {
      JsonObject cur = doc["current"];
      outdoor.temperature = cur["temperature_2m"]      | 0.0;
      outdoor.humidity    = cur["relative_humidity_2m"] | 0;
      outdoor.windSpeed   = cur["wind_speed_10m"]       | 0.0;
      outdoor.weatherCode = cur["weather_code"]         | -1;
      outdoor.description = describeWeatherCode(outdoor.weatherCode);
      outdoor.valid       = true;
      Serial.print("Outdoor: ");
      Serial.print(outdoor.temperature);
      Serial.print("  ");
      Serial.println(outdoor.description);
    } else {
      Serial.print("JSON parse error: ");
      Serial.println(err.c_str());
    }
  } else {
    Serial.print("Forecast HTTP error: ");
    Serial.println(code);
  }
  http.end();
}

// ============================================================================
//  OLED display
// ============================================================================
void updateDisplay() {
  if (!oledReady) return;

  const char* unit = (strcmp(TEMP_UNIT, "fahrenheit") == 0) ? "F" : "C";

  display.clearDisplay();
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.print(LOCATION_NAME);
  display.println(" weather");
  display.drawFastHLine(0, 10, SCREEN_WIDTH, SSD1306_WHITE);

  // Outdoor block
  display.setCursor(0, 14);
  if (outdoor.valid) {
    display.setTextSize(2);
    display.print(outdoor.temperature, 1);
    display.print((char)247);   // degree symbol
    display.println(unit);
    display.setTextSize(1);
    display.setCursor(0, 32);
    display.println(outdoor.description);
    display.print("Wind ");
    display.print(outdoor.windSpeed, 0);
    display.print("  Hum ");
    display.print(outdoor.humidity);
    display.println("%");
  } else {
    display.setTextSize(1);
    display.println("Outdoor: no data");
  }

  // Indoor block
  display.drawFastHLine(0, 52, SCREEN_WIDTH, SSD1306_WHITE);
  display.setCursor(0, 55);
  if (bmeReady) {
    display.print("In ");
    display.print(readIndoorTemperature(), 1);
    display.print((char)247);
    display.print(unit);
    display.print("  ");
    display.print(readIndoorHumidity(), 0);
    display.print("%");
  } else {
    display.print("Indoor sensor: n/a");
  }

  display.display();
}

// ============================================================================
//  Web app served by the ESP32
// ============================================================================
void handleWebClient() {
  WiFiClient client = server.available();
  if (!client) return;

  // Read (and discard) the request line/headers until the blank line.
  unsigned long start = millis();
  while (client.connected() && millis() - start < 1000) {
    if (client.available()) {
      String line = client.readStringUntil('\n');
      if (line == "\r" || line.length() == 0) break;
    }
  }

  const char* unit = (strcmp(TEMP_UNIT, "fahrenheit") == 0) ? "F" : "C";

  // Build the HTML page.
  String html;
  html.reserve(2600);
  html += "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>";
  html += "<meta name='viewport' content='width=device-width,initial-scale=1'>";
  html += "<meta http-equiv='refresh' content='60'>";
  html += "<title>ESP32 Weather Station</title><style>";
  html += "body{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;";
  html += "background:#0b1e33;color:#eaf2ff;text-align:center;padding:24px}";
  html += ".card{max-width:460px;margin:14px auto;background:#12305055;";
  html += "border:1px solid #1e4870;border-radius:16px;padding:20px}";
  html += "h1{font-size:20px;margin:0 0 4px}.muted{color:#8fb3d9;font-size:13px}";
  html += ".big{font-size:52px;font-weight:700;margin:6px 0}";
  html += ".row{display:flex;justify-content:space-around;margin-top:10px}";
  html += ".row div span{display:block;font-size:12px;color:#8fb3d9}";
  html += "</style></head><body>";
  html += "<h1>&#127780; ";
  html += LOCATION_NAME;
  html += " Weather Station</h1>";
  html += "<p class='muted'>Live from your Keyestudio ESP32 &middot; auto-refreshes every 60s</p>";

  // Outdoor card
  html += "<div class='card'><h1>Outdoor</h1>";
  if (outdoor.valid) {
    html += "<div class='big'>" + String(outdoor.temperature, 1) + "&deg;" + unit + "</div>";
    html += "<p>" + outdoor.description + "</p>";
    html += "<div class='row'>";
    html += "<div>" + String(outdoor.humidity) + "%<span>Humidity</span></div>";
    html += "<div>" + String(outdoor.windSpeed, 0) + "<span>Wind</span></div>";
    html += "</div>";
  } else {
    html += "<p class='muted'>Waiting for forecast data...</p>";
  }
  html += "</div>";

  // Indoor card
  html += "<div class='card'><h1>Indoor</h1>";
  if (bmeReady) {
    html += "<div class='big'>" + String(readIndoorTemperature(), 1) + "&deg;" + unit + "</div>";
    html += "<div class='row'>";
    html += "<div>" + String(readIndoorHumidity(), 0) + "%<span>Humidity</span></div>";
    html += "<div>" + String(readIndoorPressure(), 0) + "<span>hPa</span></div>";
    html += "</div>";
  } else {
    html += "<p class='muted'>No indoor sensor detected.</p>";
  }
  html += "</div>";

  html += "<p class='muted'>Powered by Open-Meteo</p>";
  html += "</body></html>";

  client.print("HTTP/1.1 200 OK\r\n");
  client.print("Content-Type: text/html\r\n");
  client.print("Connection: close\r\n");
  client.print("Content-Length: " + String(html.length()) + "\r\n\r\n");
  client.print(html);
  delay(5);
  client.stop();
}
