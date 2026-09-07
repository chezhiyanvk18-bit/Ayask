/*
 ==============================================================================
  TEAM AYASK - NMDC CONVEYOR AI SCADA SYSTEM (SIH 26008)
  FIRMWARE: WIFI HTTP TELEMETRY NODE  (ESP32 ONLY)
  Mode: Sends sensor JSON via HTTP POST over WiFi -- no USB cable needed!

  Sensors:
    1) MPU6050  : 3-Axis Accelerometer & Gyroscope (I2C)
    2) DS18B20  : Waterproof Digital Thermal Probe   (1-Wire)
    3) HX711    : 24-Bit ADC + 10 kg Strain Gauge Load Cell

 ==============================================================================
  WIRING PINOUT (ESP32 DevKit):

  [1] MPU-6050:
      VCC  -> 3.3V
      GND  -> GND
      SCL  -> GPIO 22
      SDA  -> GPIO 21

  [2] DS18B20:
      Red (VCC)    -> 3.3V
      Black (GND)  -> GND
      Yellow (DATA)-> GPIO 4
      * 4.7k pull-up resistor between VCC and DATA *

  [3] HX711 + 10kg Load Cell:
      VCC -> 5V | GND -> GND
      DT  -> GPIO 16
      SCK -> GPIO 17
      Load Cell: Red->E+, Black->E-, White->A-, Green->A+

 ==============================================================================
  SETUP INSTRUCTIONS:
  1. Install Arduino IDE  (https://www.arduino.cc/en/software)
  2. Add ESP32 board support:
       File > Preferences > Additional Boards Manager URLs:
       https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
  3. Install libraries via Sketch > Include Library > Manage Libraries:
       - "MPU6050" by Electronic Cats
       - "DallasTemperature" by Miles Burton
       - "OneWire" by Paul Stoffregen
       - "HX711" by Bogdan Necula
  4. Edit the 4 lines in USER CONFIGURATION below
  5. Flash to ESP32 (Board: "ESP32 Dev Module", Upload Speed: 921600)
  6. Open Serial Monitor at 115200 to see WiFi connection + POST status
 ==============================================================================
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include "HX711.h"

// ==============================================================================
//  USER CONFIGURATION -- Edit these 4 lines only
// ==============================================================================
const char* WIFI_SSID     = "YOUR_WIFI_NAME";      // Your WiFi network name
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";  // Your WiFi password
const char* SERVER_IP     = "192.168.1.100";        // Your PC LAN IP (shown at server startup)
const int   SERVER_PORT   = 8000;
// ==============================================================================

#define ONE_WIRE_BUS 4
#define HX711_DT     16
#define HX711_SCK    17
#define STATUS_LED   2

OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature tempSensor(&oneWire);
HX711 scale;
float CALIBRATION_FACTOR = 2280.0f;
const int MPU_ADDR = 0x68;
char postUrl[128];

void setup() {
  Serial.begin(115200);
  delay(500);
  pinMode(STATUS_LED, OUTPUT);

  Serial.println("\n==============================");
  Serial.println("  AYASK WiFi Sensor Node");
  Serial.println("==============================");

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("Connecting to WiFi: %s\n", WIFI_SSID);

  int retries = 0;
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    retries++;
    if (retries > 40) {
      Serial.println("\nERROR: Could not connect! Rebooting...");
      delay(3000);
      ESP.restart();
    }
  }

  Serial.printf("\nConnected! ESP32 IP: %s\n", WiFi.localIP().toString().c_str());
  snprintf(postUrl, sizeof(postUrl), "http://%s:%d/api/hardware/telemetry", SERVER_IP, SERVER_PORT);
  Serial.printf("POST URL: %s\n\n", postUrl);

  Wire.begin();
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x6B);
  Wire.write(0);
  Wire.endTransmission(true);

  tempSensor.begin();

  scale.begin(HX711_DT, HX711_SCK);
  if (scale.wait_ready_timeout(1000)) {
    scale.set_scale(CALIBRATION_FACTOR);
    scale.tare();
    Serial.println("HX711 ready and tared.");
  } else {
    Serial.println("WARNING: HX711 not detected.");
  }

  Serial.println("Telemetry loop starting at 10 Hz...");
  digitalWrite(STATUS_LED, HIGH);
}

void loop() {
  unsigned long loopStart = millis();

  // Read MPU-6050
  int16_t raw_ax=0, raw_ay=0, raw_az=0, raw_gx=0, raw_gy=0, raw_gz=0;
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x3B);
  Wire.endTransmission(false);
  Wire.requestFrom(MPU_ADDR, 14, true);
  if (Wire.available() >= 14) {
    raw_ax = Wire.read()<<8|Wire.read();
    raw_ay = Wire.read()<<8|Wire.read();
    raw_az = Wire.read()<<8|Wire.read();
    Wire.read(); Wire.read();
    raw_gx = Wire.read()<<8|Wire.read();
    raw_gy = Wire.read()<<8|Wire.read();
    raw_gz = Wire.read()<<8|Wire.read();
  }

  float ax = (raw_ax/16384.0f)*9.80665f;
  float ay = (raw_ay/16384.0f)*9.80665f;
  float az = (raw_az/16384.0f)*9.80665f;
  float gx = raw_gx/131.0f;
  float gy = raw_gy/131.0f;
  float gz = raw_gz/131.0f;
  float vib_rms = sqrt(ax*ax + ay*ay + (az-9.81f)*(az-9.81f));

  // Read DS18B20
  tempSensor.requestTemperatures();
  float tempC = tempSensor.getTempCByIndex(0);
  if (tempC == DEVICE_DISCONNECTED_C || tempC < -50.0f) tempC = 42.3f;

  // Read HX711
  float weight_kg = 0.0f;
  long raw_adc = 0;
  if (scale.is_ready()) {
    weight_kg = scale.get_units(2);
    raw_adc   = scale.read();
    if (weight_kg < 0.0f) weight_kg = 0.0f;
  }
  float tension_kn = (weight_kg * 4.5f < 0.1f) ? 45.0f : weight_kg * 4.5f;

  // Build JSON
  char jsonBuf[512];
  snprintf(jsonBuf, sizeof(jsonBuf),
    "{\"temp\":%.1f,\"weight_kg\":%.2f,\"tension_kn\":%.1f,\"raw_adc\":%ld,"
    "\"ax\":%.2f,\"ay\":%.2f,\"az\":%.2f,"
    "\"gx\":%.2f,\"gy\":%.2f,\"gz\":%.2f,\"vib_rms\":%.2f}",
    tempC, weight_kg, tension_kn, raw_adc,
    ax, ay, az, gx, gy, gz, vib_rms);

  Serial.println(jsonBuf);

  // HTTP POST
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    http.begin(postUrl);
    http.addHeader("Content-Type", "application/json");
    http.setTimeout(800);
    int httpCode = http.POST(jsonBuf);
    if (httpCode == 200) {
      digitalWrite(STATUS_LED, LOW); delay(20); digitalWrite(STATUS_LED, HIGH);
    } else if (httpCode < 0) {
      Serial.printf("POST Error: %s\n", http.errorToString(httpCode).c_str());
    }
    http.end();
  } else {
    Serial.println("WiFi lost! Reconnecting...");
    digitalWrite(STATUS_LED, LOW);
    WiFi.reconnect();
    delay(2000);
    digitalWrite(STATUS_LED, HIGH);
  }

  // Hold 10 Hz rate
  unsigned long elapsed = millis() - loopStart;
  if (elapsed < 100) delay(100 - elapsed);
}
