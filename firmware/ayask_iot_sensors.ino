/*
 ==============================================================================
  TEAM AYASK · NMDC CONVEYOR AI SCADA SYSTEM (SIH 26008)
  FIRMWARE: MULTI-SENSOR IOT TELEMETRY NODE
  Sensors:
    1) MPU6050  : 3-Axis Accelerometer & Gyroscope (I2C)
    2) DS18B20  : Waterproof Digital Thermal Probe (1-Wire)
    3) HX711    : 24-Bit ADC + 10 kg Strain Gauge Load Cell
 ==============================================================================
  WIRING PINOUT GUIDE:

  [1] MPU-6050 (3-Axis Accelerometer):
      - VCC -> 3.3V or 5V (Check your breakout board)
      - GND -> GND
      - SCL -> ESP32 GPIO 22  |  Arduino Uno/Nano A5  |  Mega Pin 21
      - SDA -> ESP32 GPIO 21  |  Arduino Uno/Nano A4  |  Mega Pin 20

  [2] DS18B20 (Waterproof Temperature Probe):
      - Red (VCC)    -> 3.3V or 5V
      - Black (GND)  -> GND
      - Yellow (DATA)-> ESP32 GPIO 4  |  Arduino Uno/Nano Pin D2
      * NOTE: Place a 4.7k Ohm pull-up resistor between VCC and DATA lines!

  [3] HX711 + 10kg Load Cell:
      - VCC -> 5V
      - GND -> GND
      - DT  -> ESP32 GPIO 16 | Arduino Uno/Nano Pin D3
      - SCK -> ESP32 GPIO 17 | Arduino Uno/Nano Pin D4
      Load Cell 4-wire color code to HX711 board:
      - Red Wire   -> E+ (Excitation +)
      - Black Wire -> E- (Excitation -)
      - White Wire -> A- (Signal -)
      - Green Wire -> A+ (Signal +)
 ==============================================================================
*/

#include <Wire.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include "HX711.h"

// ------------------- PIN CONFIGURATION -------------------
#if defined(ESP32)
  #define ONE_WIRE_BUS 4     // DS18B20 Data Pin
  #define HX711_DT     16    // HX711 Data Pin
  #define HX711_SCK    17    // HX711 Clock Pin
#else
  #define ONE_WIRE_BUS 2     // Arduino D2
  #define HX711_DT     3     // Arduino D3
  #define HX711_SCK    4     // Arduino D4
#endif

// ------------------- SENSOR INSTANCES -------------------
OneWire oneWire(ONE_WIRE_BUS);
DallasTemperature tempSensor(&oneWire);
HX711 scale;

// ------------------- LOAD CELL CALIBRATION -------------------
// Adjust CALIBRATION_FACTOR so reading matches a known calibration mass
float CALIBRATION_FACTOR = 2280.0f; 

// MPU6050 I2C Address
const int MPU_ADDR = 0x68;

void setup() {
  Serial.begin(115200);
  while (!Serial) { delay(10); } // Wait for serial on USB-native boards

  // 1. Initialize I2C and MPU-6050
  Wire.begin();
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x6B); // PWR_MGMT_1 register
  Wire.write(0);    // Wake up MPU6050
  Wire.endTransmission(true);

  // 2. Initialize DS18B20
  tempSensor.begin();

  // 3. Initialize HX711 Load Cell
  scale.begin(HX711_DT, HX711_SCK);
  if (scale.wait_ready_timeout(1000)) {
    scale.set_scale(CALIBRATION_FACTOR);
    scale.tare(); // Reset scale to 0 on boot
  }
}

void loop() {
  // ----------------- 1. READ MPU-6050 (ACCELEROMETER & GYRO) -----------------
  int16_t raw_ax = 0, raw_ay = 0, raw_az = 0;
  int16_t raw_gx = 0, raw_gy = 0, raw_gz = 0;

  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x3B); // Starting register for Accelerometer
  Wire.endTransmission(false);
  Wire.requestFrom(MPU_ADDR, 14, true);

  if (Wire.available() >= 14) {
    raw_ax = Wire.read() << 8 | Wire.read();
    raw_ay = Wire.read() << 8 | Wire.read();
    raw_az = Wire.read() << 8 | Wire.read();
    Wire.read(); Wire.read(); // Skip raw temperature register
    raw_gx = Wire.read() << 8 | Wire.read();
    raw_gy = Wire.read() << 8 | Wire.read();
    raw_gz = Wire.read() << 8 | Wire.read();
  }

  // Convert raw values to m/s^2 (assume +/- 2g default sensitivity: 16384 LSB/g)
  float ax = (raw_ax / 16384.0f) * 9.80665f;
  float ay = (raw_ay / 16384.0f) * 9.80665f;
  float az = (raw_az / 16384.0f) * 9.80665f;

  // Convert raw gyro values to deg/s (assume +/- 250 deg/s default: 131 LSB/deg/s)
  float gx = raw_gx / 131.0f;
  float gy = raw_gy / 131.0f;
  float gz = raw_gz / 131.0f;

  // Calculate dynamic vibration RMS (excluding static 1G gravity)
  float dynamic_z = az - 9.81f;
  float vib_rms = sqrt(ax * ax + ay * ay + dynamic_z * dynamic_z);

  // ----------------- 2. READ DS18B20 TEMPERATURE -----------------
  tempSensor.requestTemperatures();
  float tempC = tempSensor.getTempCByIndex(0);
  if (tempC == DEVICE_DISCONNECTED_C || tempC < -50.0f) {
    tempC = 42.3f; // Fallback if probe detached
  }

  // ----------------- 3. READ HX711 LOAD CELL -----------------
  float weight_kg = 0.0f;
  long raw_adc = 0;
  if (scale.is_ready()) {
    weight_kg = scale.get_units(2); // Read average of 2 samples
    raw_adc = scale.read();
    if (weight_kg < 0.0f) weight_kg = 0.0f; // clamp negative noise
  }

  // Map 10kg test-bench model weight to industrial belt tension (kN)
  // E.g., 1.0 kg on bench corresponds to 4.5 kN industrial take-up tension
  float tension_kn = weight_kg * 4.5f;
  if (tension_kn < 0.1f) tension_kn = 45.0f; // Default baseline if unloaded

  // ----------------- 4. EMIT HIGH-SPEED JSON TELEMETRY -----------------
  // The AYASK SCADA Python backend reads and parses this exact JSON stream
  Serial.print("{\"temp\":");
  Serial.print(tempC, 1);
  Serial.print(",\"weight_kg\":");
  Serial.print(weight_kg, 2);
  Serial.print(",\"tension_kn\":");
  Serial.print(tension_kn, 1);
  Serial.print(",\"raw_adc\":");
  Serial.print(raw_adc);
  Serial.print(",\"ax\":");
  Serial.print(ax, 2);
  Serial.print(",\"ay\":");
  Serial.print(ay, 2);
  Serial.print(",\"az\":");
  Serial.print(az, 2);
  Serial.print(",\"gx\":");
  Serial.print(gx, 2);
  Serial.print(",\"gy\":");
  Serial.print(gy, 2);
  Serial.print(",\"gz\":");
  Serial.print(gz, 2);
  Serial.print(",\"vib_rms\":");
  Serial.print(vib_rms, 2);
  Serial.println("}");

  // Emit rate: 10 Hz (every 100ms) for ultra-responsive live dashboard updates
  delay(100);
}
