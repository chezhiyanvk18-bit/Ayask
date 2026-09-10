/*
 ==============================================================================
  TEAM AYASK · NMDC CONVEYOR AI SCADA SYSTEM (SIH 26008)
  FIRMWARE: ESP32 MULTI-SENSOR WIRELESS IOT NODE
  
  Sensors:
    1) DS18B20  : Dual Digital Thermal Probes (Idler 1 & Idler 2) on GPIO 4
    2) MPU-6050 : 3-Axis Vibration / Accelerometer (I2C: SDA=21, SCL=22)
    3) HX711    : 24-Bit ADC + Load Cell (DT=GPIO 18, SCK=GPIO 19)

  Telemetry Modes:
    - HTTP Web Server: Hosts GET http://<ESP32_IP>/data (JSON output)
    - USB Serial: Streams 115200 baud JSON & Human-Readable Telemetry
 ==============================================================================
*/

#include <Wire.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include "HX711.h"
#include <math.h>
#include <WiFi.h>
#include <WebServer.h>

// =====================================================
// WIFI CONFIGURATION -- EDIT YOUR NETWORK DETAILS HERE
// =====================================================

const char* WIFI_SSID     = "Power_House";                   // <-- Replace with your WiFi SSID / Hotspot Name
const char* WIFI_PASSWORD = "14211421";  // <-- Replace with your WiFi Password

WebServer server(80);

// =====================================================
// PIN DEFINITIONS
// =====================================================

// DS18B20 (1-Wire Data Pin)
#define ONE_WIRE_BUS 4

// MPU6050 I2C Address
#define MPU_ADDR 0x68

// HX711 Load Cell Pins
#define DT_PIN 18
#define SCK_PIN 19

// =====================================================
// THRESHOLDS
// =====================================================

#define WARNING_TEMP 50.0
#define CRITICAL_TEMP 70.0

#define WARNING_VIBRATION 1.2
#define CRITICAL_VIBRATION 2.0

#define WARNING_LOAD 7.0
#define CRITICAL_LOAD 10.0

// =====================================================
// DS18B20 TEMPERATURE SENSORS
// =====================================================

OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature sensors(&oneWire);

// DS18B20 ROM addresses – auto-discovered at startup from the 1-Wire bus.
// Index 0 → idler1, Index 1 → idler2.
// Filled in setup() via sensors.getDeviceAddress() — no hardcoded ROM needed.
DeviceAddress idler1 = { 0,0,0,0,0,0,0,0 };
DeviceAddress idler2 = { 0,0,0,0,0,0,0,0 };

bool idler1Found = false;
bool idler2Found = false;

// Non-blocking temperature conversion state
bool temperatureConversionRunning = false;
unsigned long temperatureRequestTime = 0;

// 9-bit resolution is fast (~94-100 ms)
const unsigned long TEMPERATURE_CONVERSION_TIME = 100;

// =====================================================
// HX711 LOAD CELL
// =====================================================

HX711 scale;

float calibration_factor = 3206.0;

// =====================================================
// LIVE SENSOR VALUES & CACHED STATUS
// =====================================================

float latestTemp1 = NAN;
float latestTemp2 = NAN;

float latestVibration = NAN;
float latestLoad = NAN;

String latestTemp1Status = "NO DATA";
String latestTemp2Status = "NO DATA";
String latestVibrationStatus = "NO DATA";
String latestLoadStatus = "NO DATA";

// =====================================================
// MPU VIBRATION SAMPLING
// =====================================================

// Collect 100 samples non-blocking over 1 second (10ms interval)
const int REQUIRED_VIBRATION_SAMPLES = 100;

float vibrationSumSquares = 0.0;
int vibrationSampleCount = 0;

unsigned long lastVibrationSample = 0;

const unsigned long VIBRATION_SAMPLE_INTERVAL = 10;

// =====================================================
// TIMING INTERVALS
// =====================================================

unsigned long lastLoadRead = 0;
const unsigned long LOAD_INTERVAL = 500;

unsigned long lastTemperatureCycle = 0;
const unsigned long TEMPERATURE_INTERVAL = 1000;

unsigned long lastWiFiCheck = 0;
const unsigned long WIFI_CHECK_INTERVAL = 5000;

unsigned long lastSerialPrint = 0;
const unsigned long SERIAL_PRINT_INTERVAL = 1000;

// =====================================================
// STATUS EVALUATION FUNCTIONS
// =====================================================

String getTemperatureStatus(float value)
{
  if (isnan(value))
    return "NO DATA";

  if (value < WARNING_TEMP)
    return "NORMAL";

  if (value <= CRITICAL_TEMP)
    return "WARNING";

  return "CRITICAL";
}

String getVibrationStatus(float value)
{
  if (isnan(value))
    return "NO DATA";

  if (value < WARNING_VIBRATION)
    return "NORMAL";

  if (value < CRITICAL_VIBRATION)
    return "WARNING";

  return "CRITICAL";
}

String getLoadStatus(float value)
{
  if (isnan(value))
    return "NO DATA";

  if (value < WARNING_LOAD)
    return "NORMAL";

  if (value <= CRITICAL_LOAD)
    return "WARNING";

  return "CRITICAL";
}

// =====================================================
// JSON BUILDER HELPER
// =====================================================

String buildJsonPayload()
{
  String json = "{";

  // Vibration
  json += "\"vibration\":";
  if (isnan(latestVibration))
    json += "null";
  else
    json += String(latestVibration, 3);
  json += ",";
  json += "\"vibration_status\":\"";
  json += latestVibrationStatus;
  json += "\",";

  // Load
  json += "\"load\":";
  if (isnan(latestLoad))
    json += "null";
  else
    json += String(latestLoad, 2);
  json += ",";
  json += "\"load_status\":\"";
  json += latestLoadStatus;
  json += "\",";

  // Idler 1
  json += "\"idler1_temperature\":";
  if (isnan(latestTemp1))
    json += "null";
  else
    json += String(latestTemp1, 2);
  json += ",";
  json += "\"idler1_status\":\"";
  json += latestTemp1Status;
  json += "\",";

  // Idler 2
  json += "\"idler2_temperature\":";
  if (isnan(latestTemp2))
    json += "null";
  else
    json += String(latestTemp2, 2);
  json += ",";
  json += "\"idler2_status\":\"";
  json += latestTemp2Status;
  json += "\"";

  json += "}";
  return json;
}

// =====================================================
// WIFI CONNECT
// =====================================================

void connectWiFi()
{
  Serial.println();
  Serial.println("==============================================");
  Serial.println("             WIFI CONNECTION");
  Serial.println("==============================================");

  WiFi.mode(WIFI_STA);

  // Keep connection responsive
  WiFi.setSleep(false);

  Serial.print("Connecting to: ");
  Serial.println(WIFI_SSID);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long startTime = millis();

  while (
    WiFi.status() != WL_CONNECTED &&
    millis() - startTime < 15000
  )
  {
    delay(250);
    Serial.print(".");
  }

  Serial.println();

  if (WiFi.status() == WL_CONNECTED)
  {
    Serial.println("WIFI CONNECTED!");

    Serial.print("ESP32 SENSOR IP: ");
    Serial.println(WiFi.localIP());

    Serial.print("Gateway: ");
    Serial.println(WiFi.gatewayIP());

    Serial.print("RSSI: ");
    Serial.print(WiFi.RSSI());
    Serial.println(" dBm");

    Serial.print("DATA URL: http://");
    Serial.print(WiFi.localIP());
    Serial.println("/data");
  }
  else
  {
    Serial.println("WIFI CONNECTION FAILED (or timed out). Running offline/Serial mode.");
  }
}

// =====================================================
// WIFI RECONNECT
// =====================================================

void checkWiFi()
{
  if (WiFi.status() == WL_CONNECTED)
    return;

  unsigned long now = millis();

  if (now - lastWiFiCheck < WIFI_CHECK_INTERVAL)
    return;

  lastWiFiCheck = now;

  Serial.println();
  Serial.println("WiFi disconnected.");
  Serial.println("Trying to reconnect...");

  WiFi.disconnect();
  delay(50);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long startTime = millis();

  while (
    WiFi.status() != WL_CONNECTED &&
    millis() - startTime < 5000
  )
  {
    server.handleClient();
    delay(100);
  }

  if (WiFi.status() == WL_CONNECTED)
  {
    Serial.println("WIFI RECONNECTED!");

    Serial.print("ESP32 SENSOR IP: ");
    Serial.println(WiFi.localIP());
  }
  else
  {
    Serial.println("WiFi reconnect failed.");
  }
}

// =====================================================
// HTTP /DATA
// =====================================================

void handleData()
{
  String json = buildJsonPayload();

  server.sendHeader(
    "Access-Control-Allow-Origin",
    "*"
  );

  server.sendHeader(
    "Cache-Control",
    "no-cache"
  );

  server.send(
    200,
    "application/json",
    json
  );
}

// =====================================================
// ROOT PAGE
// =====================================================

void handleRoot()
{
  String html = "";

  html += "<!DOCTYPE html><html>";
  html += "<head>";
  html += "<title>AYASK Sensor ESP32</title>";
  html += "<style>body{font-family:sans-serif;padding:24px;background:#0e1117;color:#f0f2f6;} a{color:#4facfe;}</style>";
  html += "</head>";

  html += "<body>";

  html += "<h1>AYASK Wireless Sensor Monitor</h1>";

  if (WiFi.status() == WL_CONNECTED)
  {
    html += "<p style='color:#00e676;'>WiFi Status: <strong>CONNECTED</strong></p>";

    html += "<p>ESP32 IP: <strong>";
    html += WiFi.localIP().toString();
    html += "</strong></p>";
  }
  else
  {
    html += "<p style='color:#ff5252;'>WiFi Status: <strong>DISCONNECTED</strong></p>";
  }

  html += "<p><a href='/data'>Open Live Sensor JSON (/data)</a></p>";

  html += "</body>";
  html += "</html>";

  server.send(
    200,
    "text/html",
    html
  );
}

// =====================================================
// START DS18B20 CONVERSION
// =====================================================

void startTemperatureConversion()
{
  if (temperatureConversionRunning)
    return;

  sensors.requestTemperatures();

  temperatureRequestTime = millis();

  temperatureConversionRunning = true;
}

// =====================================================
// FINISH DS18B20 CONVERSION
// =====================================================

void finishTemperatureConversion()
{
  if (!temperatureConversionRunning)
    return;

  if (
    millis() - temperatureRequestTime <
    TEMPERATURE_CONVERSION_TIME
  )
  {
    return;
  }

  float temp1 = idler1Found ? sensors.getTempC(idler1) : DEVICE_DISCONNECTED_C;
  float temp2 = idler2Found ? sensors.getTempC(idler2) : DEVICE_DISCONNECTED_C;

  // Idler 1
  if (
    temp1 == DEVICE_DISCONNECTED_C
  )
  {
    latestTemp1 = NAN;
    latestTemp1Status = "NO DATA";
  }
  else
  {
    latestTemp1 = temp1;

    latestTemp1Status =
      getTemperatureStatus(temp1);
  }

  // Idler 2
  if (
    temp2 == DEVICE_DISCONNECTED_C
  )
  {
    latestTemp2 = NAN;
    latestTemp2Status = "NO DATA";
  }
  else
  {
    latestTemp2 = temp2;

    latestTemp2Status =
      getTemperatureStatus(temp2);
  }

  temperatureConversionRunning = false;
}

// =====================================================
// TEMPERATURE TASK
// =====================================================

void updateTemperatureSensors()
{
  unsigned long now = millis();

  if (
    !temperatureConversionRunning &&
    now - lastTemperatureCycle >= TEMPERATURE_INTERVAL
  )
  {
    lastTemperatureCycle = now;

    startTemperatureConversion();
  }

  finishTemperatureConversion();
}

// =====================================================
// READ MPU6050 SAMPLE
// =====================================================

void collectVibrationSample()
{
  unsigned long now = millis();

  if (
    now - lastVibrationSample <
    VIBRATION_SAMPLE_INTERVAL
  )
  {
    return;
  }

  lastVibrationSample = now;

  Wire.beginTransmission(MPU_ADDR);

  Wire.write(0x3B);

  if (
    Wire.endTransmission(false) != 0
  )
  {
    return;
  }

  Wire.requestFrom(
    MPU_ADDR,
    14,
    true
  );

  if (Wire.available() < 14)
  {
    return;
  }

  int16_t AcX =
    (Wire.read() << 8) |
    Wire.read();

  int16_t AcY =
    (Wire.read() << 8) |
    Wire.read();

  int16_t AcZ =
    (Wire.read() << 8) |
    Wire.read();

  // Skip temperature
  Wire.read();
  Wire.read();

  // Skip gyro
  Wire.read();
  Wire.read();

  Wire.read();
  Wire.read();

  Wire.read();
  Wire.read();

  float ax =
    (AcX / 16384.0) * 9.80665;

  float ay =
    (AcY / 16384.0) * 9.80665;

  float az =
    (AcZ / 16384.0) * 9.80665;

  float totalAcceleration =
    sqrt(
      ax * ax +
      ay * ay +
      az * az
    );

  float vibration =
    totalAcceleration - 9.80665;

  // Prevent tiny negative values caused by rounding
  if (vibration < 0)
  {
    vibration = -vibration;
  }

  vibrationSumSquares +=
    vibration * vibration;

  vibrationSampleCount++;

  // Complete RMS calculation
  if (
    vibrationSampleCount >=
    REQUIRED_VIBRATION_SAMPLES
  )
  {
    latestVibration =
      sqrt(
        vibrationSumSquares /
        vibrationSampleCount
      );

    latestVibrationStatus =
      getVibrationStatus(
        latestVibration
      );

    // Reset for next RMS window
    vibrationSumSquares = 0.0;

    vibrationSampleCount = 0;
  }
}

// =====================================================
// LOAD CELL
// =====================================================

void updateLoadCell()
{
  unsigned long now = millis();

  if (
    now - lastLoadRead <
    LOAD_INTERVAL
  )
  {
    return;
  }

  lastLoadRead = now;

  if (
    scale.wait_ready_timeout(50)
  )
  {
    float weight =
      scale.get_units(2);

    if (
      weight > -0.10 &&
      weight < 0.10
    )
    {
      weight = 0.00;
    }

    if (weight < 0)
    {
      weight = 0.00;
    }

    latestLoad = weight;

    latestLoadStatus =
      getLoadStatus(weight);
  }
  else
  {
    latestLoad = NAN;
    latestLoadStatus = "NO DATA";
  }
}

// =====================================================
// SERIAL OUTPUT (Both Human Readout + Direct SCADA JSON)
// =====================================================

void printSensorData()
{
  unsigned long now = millis();

  if (
    now - lastSerialPrint <
    SERIAL_PRINT_INTERVAL
  )
  {
    return;
  }

  lastSerialPrint = now;

  // 1. Output compact JSON line for AYASK SCADA Hardware Bridge
  Serial.println(buildJsonPayload());

  // 2. Output formatted human table for Arduino IDE Serial Monitor
  Serial.println("==============================================");
  Serial.println("              SENSOR READINGS");
  Serial.println("==============================================");

  // Idler 1
  Serial.println("IDLER 1");
  if (isnan(latestTemp1))
  {
    Serial.println("Temperature: SENSOR ERROR");
  }
  else
  {
    Serial.print("Temperature: ");
    Serial.print(latestTemp1, 2);
    Serial.println(" C");
    Serial.print("Status: ");
    Serial.println(latestTemp1Status);
  }

  // Idler 2
  Serial.println();
  Serial.println("IDLER 2");
  if (isnan(latestTemp2))
  {
    Serial.println("Temperature: SENSOR ERROR");
  }
  else
  {
    Serial.print("Temperature: ");
    Serial.print(latestTemp2, 2);
    Serial.println(" C");
    Serial.print("Status: ");
    Serial.println(latestTemp2Status);
  }

  // Vibration
  Serial.println();
  Serial.println("VIBRATION");
  if (isnan(latestVibration))
  {
    Serial.println("Vibration RMS: CALCULATING...");
  }
  else
  {
    Serial.print("Vibration RMS: ");
    Serial.print(latestVibration, 3);
    Serial.println(" m/s2");
    Serial.print("Status: ");
    Serial.println(latestVibrationStatus);
  }

  // Load
  Serial.println();
  Serial.println("LOAD CELL");
  if (isnan(latestLoad))
  {
    Serial.println("Load: SENSOR ERROR");
  }
  else
  {
    Serial.print("Load: ");
    Serial.print(latestLoad, 2);
    Serial.println(" kg");
    Serial.print("Status: ");
    Serial.println(latestLoadStatus);
  }

  Serial.println("==============================================");
}

// =====================================================
// SETUP
// =====================================================

void setup()
{
  Serial.begin(115200);

  delay(1000);

  Serial.println();
  Serial.println("==============================================");
  Serial.println("       AYASK WIRELESS SENSOR MONITOR");
  Serial.println("==============================================");

  // ===================================================
  // DS18B20
  // ===================================================

  sensors.begin();

  // Faster temperature resolution (9-bit: 0.5 C precision)
  sensors.setResolution(9);

  // Do not block while waiting for conversion
  sensors.setWaitForConversion(false);

  int sensorCount = sensors.getDeviceCount();
  Serial.print("DS18B20 Sensors detected: ");
  Serial.println(sensorCount);

  // Auto-discover ROM addresses by bus index (no hardcoding needed)
  if (sensorCount >= 1 && sensors.getDeviceAddress(idler1, 0)) {
    idler1Found = true;
    sensors.setResolution(idler1, 9);
    Serial.print("IDLER 1 ROM: ");
    for (int i = 0; i < 8; i++) {
      if (idler1[i] < 0x10) Serial.print("0");
      Serial.print(idler1[i], HEX);
      if (i < 7) Serial.print(",");
    }
    Serial.println(" → CONNECTED");
  } else {
    Serial.println("IDLER 1 TEMPERATURE: NOT DETECTED (no sensor at bus index 0)");
  }

  if (sensorCount >= 2 && sensors.getDeviceAddress(idler2, 1)) {
    idler2Found = true;
    sensors.setResolution(idler2, 9);
    Serial.print("IDLER 2 ROM: ");
    for (int i = 0; i < 8; i++) {
      if (idler2[i] < 0x10) Serial.print("0");
      Serial.print(idler2[i], HEX);
      if (i < 7) Serial.print(",");
    }
    Serial.println(" → CONNECTED");
  } else {
    Serial.println("IDLER 2 TEMPERATURE: NOT DETECTED (no sensor at bus index 1)");
  }

  // ===================================================
  // MPU6050
  // ===================================================

  Wire.begin(21, 22);

  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x6B);
  Wire.write(0x00);

  if (Wire.endTransmission() == 0)
  {
    Serial.println("MPU6050: CONNECTED");
  }
  else
  {
    Serial.println("MPU6050: NOT DETECTED");
  }

  // ===================================================
  // HX711
  // ===================================================

  scale.begin(DT_PIN, SCK_PIN);

  Serial.println("HX711: INITIALIZING...");

  delay(1000);

  if (scale.wait_ready_timeout(2000))
  {
    Serial.println("HX711: CONNECTED");
    Serial.println("Remove all weight from scale...");

    delay(3000);

    scale.set_scale();
    scale.tare(20);
    scale.set_scale(calibration_factor);

    Serial.println("HX711 TARE COMPLETE!");
  }
  else
  {
    Serial.println("HX711: NOT READY!");
  }

  // ===================================================
  // WIFI
  // ===================================================

  connectWiFi();

  // ===================================================
  // HTTP SERVER
  // ===================================================

  server.on("/", handleRoot);
  server.on("/data", handleData);

  server.begin();

  Serial.println("HTTP SERVER STARTED");

  if (WiFi.status() == WL_CONNECTED)
  {
    Serial.print("SENSOR DATA URL: http://");
    Serial.print(WiFi.localIP());
    Serial.println("/data");
  }

  Serial.println("SYSTEM READY");
}

// =====================================================
// MAIN LOOP
// =====================================================

void loop()
{
  // Keep HTTP server responsive
  server.handleClient();

  // Monitor and reconnect WiFi if dropped
  checkWiFi();

  // Temperature update state machine
  updateTemperatureSensors();

  // Vibration sampling
  collectVibrationSample();

  // Load cell update
  updateLoadCell();

  // Serial output (JSON + tables)
  printSensorData();

  // Tiny delay for RTOS yield
  delay(2);
}
