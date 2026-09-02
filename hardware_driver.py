# hardware_driver.py
import os
import glob
import time
import math
import RPi.GPIO as GPIO
from mpu6050 import mpu6050

class HardwareManager:
    def __init__(self, relay_pin=17, hx_dt=5, hx_sck=6):
        # 1. Setup GPIO Mode
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        
        # 2. Setup Emergency Motor Cutoff Relay
        self.relay_pin = relay_pin
        GPIO.setup(self.relay_pin, GPIO.OUT)
        GPIO.output(self.relay_pin, GPIO.HIGH) # HIGH = Motor ON (Normally Closed)

        # 3. Setup MPU6050 Accelerometer
        try:
            self.mpu = mpu6050(0x68)
            print("[✓] MPU6050 Vibration Sensor initialized.")
        except Exception as e:
            print(f"[!] MPU6050 Init Error: {e}")
            self.mpu = None

        # 4. Setup DS18B20 1-Wire Temperature Sensor
        self.temp_sensor_path = self._get_ds18b20_path()

        # 5. Setup HX711 Load Cell Pins
        self.hx_dt = hx_dt
        self.hx_sck = hx_sck
        GPIO.setup(self.hx_dt, GPIO.IN)
        GPIO.setup(self.hx_sck, GPIO.OUT)
        self.load_calibration_factor = 420.0 # Adjust with a known test weight

    def _get_ds18b20_path(self):
        base_dir = '/sys/bus/w1/devices/'
        folders = glob.glob(base_dir + '28*')
        if folders:
            print(f"[✓] DS18B20 Temperature Sensor detected: {folders[0]}")
            return folders[0] + '/w1_slave'
        print("[!] DS18B20 not detected on GPIO 4 (1-Wire).")
        return None

    def read_vibration_rms(self):
        """Reads MPU6050 accelerations and calculates total vibration magnitude (RMS proxy)."""
        if not self.mpu:
            return 0.0
        try:
            accel = self.mpu.get_accel_data()
            # Total acceleration magnitude in m/s^2
            magnitude = math.sqrt(accel['x']**2 + accel['y']**2 + accel['z']**2)
            # Subtract gravity (9.8 m/s^2) to isolate dynamic vibration
            dynamic_vib = abs(magnitude - 9.81)
            return round(dynamic_vib, 2)
        except Exception:
            return 0.0

    def read_temperature(self):
        """Reads DS18B20 temperature in Celsius."""
        if not self.temp_sensor_path or not os.path.exists(self.temp_sensor_path):
            return 25.0
        try:
            with open(self.temp_sensor_path, 'r') as f:
                lines = f.readlines()
            if lines[0].strip().endswith("YES"):
                temp_output = lines[1].find('t=')
                if temp_output != -1:
                    temp_string = lines[1].strip()[temp_output + 2:]
                    return round(float(temp_string) / 1000.0, 1)
        except Exception:
            pass
        return 25.0

    def read_load_tension(self):
        """Direct bit-bang read for HX711 24-bit ADC."""
        count = 0
        try:
            GPIO.output(self.hx_sck, GPIO.LOW)
            timeout = 1000
            while GPIO.input(self.hx_dt) == 1 and timeout > 0:
                timeout -= 1
            if timeout <= 0:
                return 0.0

            for _ in range(24):
                GPIO.output(self.hx_sck, GPIO.HIGH)
                count = count << 1
                GPIO.output(self.hx_sck, GPIO.LOW)
                if GPIO.input(self.hx_dt):
                    count += 1

            # 25th pulse (Gain 128)
            GPIO.output(self.hx_sck, GPIO.HIGH)
            GPIO.output(self.hx_sck, GPIO.LOW)

            # Two's complement conversion
            if count & 0x800000:
                count -= 0x1000000

            # Convert to tension in kg/kN
            weight_kg = abs(count) / self.load_calibration_factor / 1000.0
            return round(weight_kg, 2)
        except Exception:
            return 0.0

    def trip_motor_emergency(self, trip=True):
        """Triggers the physical relay to cut power to the drive motor."""
        if trip:
            GPIO.output(self.relay_pin, GPIO.LOW) # Cuts Motor Circuit
            return True
        else:
            GPIO.output(self.relay_pin, GPIO.HIGH)
            return False

    def cleanup(self):
        GPIO.cleanup()