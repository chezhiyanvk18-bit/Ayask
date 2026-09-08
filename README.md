# Team AYASK — Industrial Conveyor AI SCADA System
> **Smart India Hackathon (SIH 26008)**  
> **NMDC Iron Ore Conveyor Health Monitoring, Edge AI & Multi-Sensor IoT Digital Twin**

---

## Overview
**AYASK** is a mission-critical SCADA & Edge AI platform designed for heavy-duty iron ore belt conveyors. It synthesizes real-time computer vision (YOLOv8 defect detection) with multi-modal physical IoT telemetry to predict remaining useful life (RUL), detect longitudinal tears, and prevent catastrophic failures.

### Key Capabilities:
- **Vision AI Pipeline (YOLOv8)**: Real-time detection of belt tears, splice separation, damage gouges, mechanical clips, repair patches, and edge sway.
- **Physical IoT Sensor Bridge**: Live ingestion from **MPU-6050** (3-axis vibration), **DS18B20** (waterproof temperature probe), and **HX711 + 10 kg Load Cell** via USB Serial or WiFi.
- **Multi-Point Thermal Monitoring**: Individual live temperatures for `IDLER 01` (42.3°C), `IDLER 02` (51.7°C), `IDLER 03` (46.8°C), and `IDLER 04` (39.4°C).
- **Dual Take-Up Load Cells**: Bilateral strain measurements (`LC-01: 22.6 kN`, `LC-02: 22.4 kN`, Total: `45.0 kN`).
- **Interactive 4-Zone Digital Twin**: Real-time canvas simulation with speed regulation (0–5.0 m/s), ore throughput slider (0–2,500 TPH), and motor start/stop interlocks.
- **Failsafe NC Relay Safety Loop**: Automated motor contactor trips in <120ms upon critical fault injection.
- **Dual Visual Theme**: Professional dark mode (strictly brown & golden yellow, zero vibrant neon) and crisp high-contrast light mode. Zero emojis — 100% SVG vector iconography.

---

## Quickstart Guide

### 1. Clone the Repository
```bash
git clone https://github.com/chezhiyanvk18-bit/Ayask.git
cd Ayask
```

### 2. Install Dependencies
Make sure you have Python 3.10 or higher installed:
```bash
pip install -r requirements.txt
pip install ultralytics
```

### 3. Run the Application Locally
```bash
python server.py
```
Open your browser and navigate to:
**`http://localhost:8000/dashboard`** (or `http://127.0.0.1:8000/dashboard`)

> **Demo Login Credentials:**
> - **Email**: `operator@nmdc.gov.in`
> - **Password**: `Admin@1234`
> *(Or simply click **"Quick Fill"** on the sign-in card)*

---

## One-Click Public Link (For Team & Jury Access)

To share the running application worldwide without setting up port forwarding or ngrok:
- Double-click **`START_PUBLIC_LINK.bat`** (on Windows)
- Or run:
  ```bash
  python start_public_scada.py
  ```
The script will output a secure HTTPS link (e.g. `https://your-tunnel.trycloudflare.com`) that works on any phone, laptop, or tablet.

---

## Hardware Integration (MPU6050 + DS18B20 + 10kg Load Cell)

The system includes ready-to-flash firmware for Arduino and ESP32 microcontrollers.

### 1. Wiring Pinout
| Sensor | Signal | ESP32 GPIO | Arduino Uno/Nano | Notes |
| :--- | :--- | :--- | :--- | :--- |
| **MPU-6050** | `SDA` | GPIO 21 | A4 | I2C Data |
| **MPU-6050** | `SCL` | GPIO 22 | A5 | I2C Clock |
| **DS18B20** | `DATA` | GPIO 4 | D2 | 1-Wire (add 4.7kΩ pull-up to VCC) |
| **HX711** | `DT` | GPIO 18 | D3 | Load Cell Serial Data |
| **HX711** | `SCK` | GPIO 19 | D4 | Load Cell Clock |

### 2. Flash the Microcontroller
1. Open the Arduino IDE.
2. Install libraries via Library Manager: `OneWire`, `DallasTemperature`, `HX711`.
3. Open [`firmware/ayask_wifi_sensors.ino`](firmware/ayask_wifi_sensors.ino).
4. Configure your WiFi credentials (`WIFI_SSID` & `WIFI_PASSWORD`).
5. Select your ESP32 board and COM port, then click **Upload**.

### 3. Connect to Dashboard
- **WiFi Mode**: On the SCADA dashboard, click **`IoT: Sim Mode`**, choose **"Connect via ESP32 WiFi IP"**, enter your ESP32's IP, and click **Connect**.
- **USB Serial Mode**: Plug the ESP32 in via USB, select your COM port at 115200 bps, and click **Connect Port**.
The dashboard will switch from simulation to streaming your **exact live physical sensor measurements**!

---

## Dataset & YOLOv8 Defect Detection

The repository contains 477 annotated conveyor belt inspection frames under `src/data/Conveyor/`:
- Classes: `Damage`, `Splice`, `Clips`, `Repair`, `Loops`, `Splice Number`.
- Fine-tuned weights: `models/belt_defect_yolov8/weights/best.pt` (72.9% Precision, 91.3% mAP on clips/fasteners).

To inspect or retrain:
```bash
python download_conveyor_datasets.py --status
python download_conveyor_datasets.py --train --epochs 15
```

---

## Cloud Deployment (24/7 on Render.com)
The project includes cloud manifests: `requirements.txt`, `Procfile`, and `render.yaml`.
1. Sign in to [Render.com](https://render.com).
2. Click **New +** -> **Web Service**.
3. Select `chezhiyanvk18-bit/Ayask`.
4. Deploy automatically with zero configuration.
