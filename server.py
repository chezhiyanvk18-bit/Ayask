# server.py
# ==============================================================================
# TEAM AYASK · NMDC IRON ORE CONVEYOR HEALTH MONITORING SCADA COMMAND CENTER
# SIH PROBLEM STATEMENT 26008 · MULTI-MODAL IOT & EDGE AI DIGITAL TWIN
# ==============================================================================

import sys
import asyncio

# Critical fix for Windows Python 3.13 asyncio
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import os
import cv2
import json
import time
import math
import random
import re
import hashlib
import secrets
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from pydantic import BaseModel

# Initialize FastAPI App
app = FastAPI(title="Team AYASK - NMDC Belt Monitoring SCADA")

# Ensure static directory exists
os.makedirs("static", exist_ok=True)

# ----------------- STATIC ASSETS & FAVICON -----------------
@app.get("/favicon.ico")
def favicon():
    if os.path.exists("static/ayask_logo.png"):
        return FileResponse("static/ayask_logo.png", media_type="image/png")
    return HTMLResponse(status_code=404)

@app.get("/static/ayask_logo.png")
def logo():
    if os.path.exists("static/ayask_logo.png"):
        return FileResponse("static/ayask_logo.png", media_type="image/png")
    return HTMLResponse(status_code=404)

# ----------------- ATTEMPT TO LOAD ULTRALYTICS YOLO -----------------
try:
    from ultralytics import YOLO
    if os.path.exists("models/belt_defect_yolov8/weights/best.pt"):
        yolo_detector = YOLO("models/belt_defect_yolov8/weights/best.pt")
        yolo_model_name = "YOLOv8 Belt Defect"
    elif os.path.exists("models/conveyor_yolov8n/weights/best.pt"):
        yolo_detector = YOLO("models/conveyor_yolov8n/weights/best.pt")
        yolo_model_name = "YOLOv8 Conveyor"
    elif os.path.exists("yolov8n.pt"):
        yolo_detector = YOLO("yolov8n.pt")
        yolo_model_name = "YOLOv8n Edge"
    else:
        yolo_detector = None
        yolo_model_name = "Model Missing"
    YOLO_AVAILABLE = yolo_detector is not None
except Exception as e:
    yolo_detector = None
    yolo_model_name = "Unavailable"
    YOLO_AVAILABLE = False
    print(f"[!] Ultralytics YOLO init warning: {e}")

# ----------------- AUTH STORE (In-Memory Session Store) -----------------
users_db = {}      # email -> {"name": str, "password_hash": str, "salt": str}
sessions_db = {}   # token -> {"email": str, "name": str, "created": float}

def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()

def create_session(email: str, name: str) -> str:
    token = secrets.token_hex(24)
    sessions_db[token] = {"email": email, "name": name, "created": time.time()}
    return token

# Seed standard industrial demo operator account
_demo_salt = "nmdc_scada_salt_2026"
users_db["operator@nmdc.gov.in"] = {
    "name": "Team AYASK Lead Engineer",
    "password_hash": hash_password("Admin@1234", _demo_salt),
    "salt": _demo_salt
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

class SignupRequest(BaseModel):
    name: str
    email: str
    password: str

class SigninRequest(BaseModel):
    email: str
    password: str

class TokenRequest(BaseModel):
    token: str

class CameraConfigRequest(BaseModel):
    source: str         # "esp32", "webcam", "sim"
    esp32_url: str = ""

class FaultRequest(BaseModel):
    fault_type: str

class TwinStimulateRequest(BaseModel):
    speed_mps: float = 3.5
    throughput_tph: float = 1250.0
    motor_running: bool = True

@app.post("/api/signup")
def signup(req: SignupRequest):
    name = req.name.strip()
    email = req.email.strip().lower()
    password = req.password

    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Please enter your full name.")
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    if email in users_db:
        raise HTTPException(status_code=409, detail="An account with this email already exists. Please sign in instead.")

    salt = secrets.token_hex(16)
    users_db[email] = {"name": name, "password_hash": hash_password(password, salt), "salt": salt}

    log_entry = {
        "timestamp": time.strftime("%H:%M:%S"),
        "event": f"New operator account registered: {name} ({email})",
        "severity": "INFO"
    }
    state.incident_log.insert(0, log_entry)

    token = create_session(email, name)
    return {"token": token, "name": name, "email": email}

@app.post("/api/signin")
def signin(req: SigninRequest):
    email = req.email.strip().lower()
    password = req.password

    user = users_db.get(email)
    if not user or hash_password(password, user["salt"]) != user["password_hash"]:
        raise HTTPException(status_code=401, detail="Incorrect email or password.")

    token = create_session(email, user["name"])
    log_entry = {
        "timestamp": time.strftime("%H:%M:%S"),
        "event": f"Operator signed in: {user['name']} ({email})",
        "severity": "INFO"
    }
    state.incident_log.insert(0, log_entry)
    return {"token": token, "name": user["name"], "email": email}

@app.post("/api/verify_session")
def verify_session(req: TokenRequest):
    session = sessions_db.get(req.token)
    if not session:
        raise HTTPException(status_code=401, detail="Session expired or invalid. Please sign in again.")
    return {"valid": True, "name": session["name"], "email": session["email"]}

@app.post("/api/signout")
def signout(req: TokenRequest):
    sessions_db.pop(req.token, None)
    return {"status": "signed_out"}

# ----------------- SYSTEM STATE & INDUSTRIAL DIGITAL TWIN -----------------
class SystemState:
    def __init__(self):
        self.camera_enabled = False
        self.camera_source = "webcam"    # "esp32", "webcam", "sim"
        self.esp32_url = os.environ.get("ESP32_CAM_URL", "http://192.168.4.1:81/stream")
        self.mirror_view = False
        self.yolo_enabled = True
        self.yolo_confidence = 0.25
        self.device_connected = False
        self.emergency_stop = False
        self.relay_nc_energized = True  # Normally Closed failsafe motor interlock relay
        
        # Real-time Metrics & Status
        self.live_fps = 0.0
        self.detected_objects = []
        self.camera_status = "Standby (Zone 3 Return Belt)"
        
        # Real-Time Belt Health Synthesis (SIH 26008 Specification)
        self.bhi = 98.0
        self.joint_rul_hours = 720.0
        self.speed_mps = 3.5
        self.throughput_tph = 1250.0
        self.status = "LEVEL 1: OPTIMAL"
        self.status_level = 1
        
        # Digital Twin Mechanics Simulation
        self.belt_pos_pct = 0.0
        self.motor_rpm = 1480.0
        self.splice_pass_count = 142
        
        # Multi-Sensor IoT Telemetry Nodes
        self.tension_kn = 45.0
        self.bearing_temp_c = 52.4
        self.vibration_rms = 2.1
        self.vibration_x = 0.42
        self.vibration_y = -0.28
        self.vibration_z = 9.81
        
        # Conveyor Alignment & Material Surface
        self.misalignment_mm = 2.1
        self.surface_material = "NORMAL OPERATING DUST COAT"
        
        # Digital Twin Zone Statuses
        self.zones = {
            "zone1": {"name": "Zone 1: Drive Motor & Head Pulley", "status": "NOMINAL", "level": 1},
            "zone2": {"name": "Zone 2: Carrying Run & Troughing Idlers", "status": "NOMINAL", "level": 1},
            "zone3": {"name": "Zone 3: Return Belt Optical & Vib Scanner", "status": "NOMINAL", "level": 1},
            "zone4": {"name": "Zone 4: Tail Pulley & Gravity Take-Up", "status": "NOMINAL", "level": 1}
        }
        
        # Protocol Link Feeds
        self.modbus_status = "Connected (Port 502 · PLC Node 10.0.4.12)"
        self.opcua_status = "Online (opc.tcp://10.0.4.15:4840)"
        
        self.active_fault = "NONE"
        self.injected_defect = "NONE"
        self.incident_log = [
            {"timestamp": time.strftime("%H:%M:%S"), "event": "SCADA Core System Booted · Team AYASK Engine Online", "severity": "INFO"},
            {"timestamp": time.strftime("%H:%M:%S"), "event": "Modbus TCP Channel Bound to PLC Drive Loop (Port 502)", "severity": "INFO"},
            {"timestamp": time.strftime("%H:%M:%S"), "event": "OPC-UA Security Baseline Handshake Verified", "severity": "INFO"},
            {"timestamp": time.strftime("%H:%M:%S"), "event": "Normally Closed (NC) Safety Interlock Loop Energized (415V)", "severity": "INFO"}
        ]

state = SystemState()

# ----------------- RESILIENT VIDEO GENERATOR & VISION ENGINE -----------------
def generate_frames():
    cap = None
    current_source = None
    current_url = None
    frame_counter = 0
    last_frame_time = time.time()
    last_reconnect_attempt = 0.0
    consecutive_read_failures = 0

    try:
        while True:
            frame_counter += 1
            now = time.time()

            # Measure unthrottled FPS
            dt = now - last_frame_time
            if dt > 0:
                inst_fps = 1.0 / dt
                state.live_fps = round(state.live_fps * 0.85 + inst_fps * 0.15, 1)
            last_frame_time = now

            frame = None

            if state.camera_enabled:
                if current_source != state.camera_source or (state.camera_source == "esp32" and current_url != state.esp32_url):
                    if cap is not None:
                        cap.release()
                        cap = None
                    current_source = state.camera_source
                    current_url = state.esp32_url
                    consecutive_read_failures = 0

                if state.camera_source == "sim":
                    frame = None
                    state.camera_status = "Zone 3 Digital Twin (Simulated)"
                else:
                    if cap is None or not cap.isOpened():
                        if now - last_reconnect_attempt > 1.5:
                            last_reconnect_attempt = now
                            if cap is not None:
                                cap.release()
                                cap = None
                            try:
                                if state.camera_source == "esp32":
                                    state.camera_status = f"Connecting to ESP32 ({state.esp32_url})..."
                                    cap = cv2.VideoCapture(state.esp32_url, cv2.CAP_FFMPEG)
                                else:
                                    state.camera_status = "Initializing Local Webcam (0)..."
                                    cap_backend = cv2.CAP_DSHOW if sys.platform == 'win32' else cv2.CAP_ANY
                                    cap = cv2.VideoCapture(0, cap_backend)

                                if cap is not None and cap.isOpened():
                                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 360)
                                    state.camera_status = "Online (Streaming)"
                                else:
                                    state.camera_status = "Source Offline (Retrying...)"
                            except Exception as e:
                                state.camera_status = f"Connection Error: {str(e)[:24]}"
                                cap = None

                    if cap is not None and cap.isOpened():
                        success, raw_frame = cap.read()
                        if success and raw_frame is not None:
                            consecutive_read_failures = 0
                            frame = cv2.resize(raw_frame, (640, 360))
                            state.camera_status = "Online (Streaming Zone 3)"
                        else:
                            consecutive_read_failures += 1
                            if consecutive_read_failures > 30:
                                state.camera_status = "Signal Lost - Retrying"
                                if cap is not None:
                                    cap.release()
                                    cap = None
                    else:
                        frame = None
            else:
                if cap is not None:
                    cap.release()
                    cap = None
                current_source = None
                state.camera_status = "Standby (Zone 3 Return Belt)"
                frame = None

            # Render Synthetic Frame if camera is offline or in simulation
            is_synthetic = False
            if frame is None:
                is_synthetic = True
                frame = np.zeros((360, 640, 3), dtype=np.uint8)
                frame[:] = (16, 14, 12)

                # SCADA Grid lines
                for x in range(0, 640, 40):
                    cv2.line(frame, (x, 0), (x, 360), (32, 26, 20), 1)
                for y in range(0, 360, 40):
                    cv2.line(frame, (0, y), (640, y), (32, 26, 20), 1)

                # Conveyor Belt graphics
                belt_top = 70
                belt_bot = 290
                cv2.rectangle(frame, (0, belt_top), (640, belt_bot), (28, 22, 18), -1)
                cv2.line(frame, (0, belt_top), (640, belt_top), (110, 85, 55), 2)
                cv2.line(frame, (0, belt_bot), (640, belt_bot), (110, 85, 55), 2)

                # Moving conveyor belt texture and ore lumps
                belt_speed = int((frame_counter * 9) % 90) if state.speed_mps > 0.1 else 0

                for bx in range(-90 + belt_speed, 640, 90):
                    cv2.line(frame, (bx, belt_top), (bx + 30, belt_bot), (48, 36, 26), 2)
                    cv2.circle(frame, (bx + 15, 160), 10, (58, 42, 28), -1)
                    cv2.circle(frame, (bx + 45, 200), 14, (52, 38, 24), -1)

                # Optical laser scanning sweep
                if state.speed_mps > 0.1:
                    sweep_x = int((frame_counter * 7) % 640)
                    cv2.line(frame, (sweep_x, 0), (sweep_x, 360), (212, 175, 55), 1)

                if state.camera_enabled and state.camera_source == "sim":
                    cv2.rectangle(frame, (120, 130), (520, 230), (24, 18, 14), -1)
                    cv2.rectangle(frame, (120, 130), (520, 230), (212, 175, 55), 1)
                    cv2.putText(frame, "ZONE 3 RETURN BELT DIGITAL TWIN", (145, 168),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.60, (245, 238, 225), 2)
                    cv2.putText(frame, "Optical scanning for tears, splice joint gap & wear", (152, 196),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.40, (190, 165, 135), 1)
                elif state.camera_enabled:
                    cv2.rectangle(frame, (110, 130), (530, 230), (24, 18, 14), -1)
                    cv2.rectangle(frame, (110, 130), (530, 230), (168, 72, 37), 1)
                    cv2.putText(frame, "AWAITING VIDEO STREAM SIGNAL", (155, 168),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (245, 238, 225), 2)
                    src_label = f"Target: {state.esp32_url}" if state.camera_source == "esp32" else "Target: Local Webcam (0)"
                    cv2.putText(frame, src_label, (135, 195),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (190, 165, 135), 1)
                    cv2.putText(frame, f"Status: {state.camera_status}", (135, 215),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.38, (212, 175, 55), 1)
                else:
                    cv2.rectangle(frame, (130, 135), (510, 225), (24, 18, 14), -1)
                    cv2.rectangle(frame, (130, 135), (510, 225), (110, 85, 55), 1)
                    cv2.putText(frame, "ZONE 3 CAMERA FEED ON STANDBY", (165, 170),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.58, (245, 238, 225), 2)
                    cv2.putText(frame, "Click [Enable Camera] to initialize optical inspection", (150, 196),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.40, (180, 155, 125), 1)

            if state.mirror_view:
                frame = cv2.flip(frame, 1)

            # YOLOv8 Object & Defect Detection
            detections = []
            if state.yolo_enabled and YOLO_AVAILABLE and yolo_detector is not None:
                try:
                    results = yolo_detector.predict(frame, conf=state.yolo_confidence, verbose=False, imgsz=320)
                    if results and len(results) > 0:
                        frame = results[0].plot()
                        for box in results[0].boxes:
                            cls_id = int(box.cls[0].item())
                            conf_val = float(box.conf[0].item())
                            cls_name = results[0].names.get(cls_id, f"obj_{cls_id}")
                            detections.append({
                                "class": cls_name,
                                "conf": round(conf_val * 100, 1)
                            })
                except Exception:
                    pass

            # Inject Conveyor Specific Defects & Annotations
            h, w, _ = frame.shape
            if state.active_fault == "SPLICE_TEAR":
                cv2.rectangle(frame, (170, 85), (470, 245), (37, 72, 168), 2)
                cv2.rectangle(frame, (170, 58), (470, 85), (37, 72, 168), -1)
                cv2.putText(frame, "CRITICAL: SPLICE JOINT RUPTURE [99.2%]", (178, 77),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.46, (255, 255, 255), 1)
                detections.insert(0, {"class": "Splice Rupture", "conf": 99.2})
            elif state.active_fault == "SURFACE_GOUGE":
                cv2.rectangle(frame, (140, 140), (330, 240), (45, 110, 180), 2)
                cv2.rectangle(frame, (140, 115), (330, 140), (45, 110, 180), -1)
                cv2.putText(frame, "DEFECT: SURFACE GOUGE [88.5%]", (148, 133),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1)
                detections.insert(0, {"class": "Surface Gouge", "conf": 88.5})
            elif state.active_fault == "ORE_DUST":
                cv2.rectangle(frame, (w - 330, 15), (w - 20, 65), (20, 28, 36), -1)
                cv2.rectangle(frame, (w - 330, 15), (w - 20, 65), (212, 175, 55), 1)
                cv2.putText(frame, "SURFACE: IRON ORE LAYER", (w - 320, 35),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (245, 238, 225), 1)
                cv2.putText(frame, "AI FILTER: BENIGN TEAR SUPPRESSED", (w - 320, 55),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (212, 175, 55), 1)
                detections.insert(0, {"class": "Iron Ore Layer (Benign)", "conf": 94.0})
            elif state.active_fault == "MISALIGNMENT":
                cv2.line(frame, (65, 0), (65, h), (37, 72, 168), 3)
                cv2.line(frame, (w - 65, 0), (w - 65, h), (37, 72, 168), 3)
                cv2.putText(frame, "LATERAL BELT SWAY EXCEEDED", (160, 45),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55, (37, 72, 168), 2)
                detections.insert(0, {"class": "Edge Misalignment", "conf": 96.0})

            state.detected_objects = detections[:6]

            # HUD Telemetry Overlay
            cv2.rectangle(frame, (10, 10), (230, 40), (18, 14, 10), -1)
            cv2.rectangle(frame, (10, 10), (230, 40), (70, 52, 35), 1)
            src_str = "ESP32-CAM" if state.camera_source == "esp32" else ("WEBCAM" if state.camera_source == "webcam" else "DIGITAL TWIN")
            mirror_str = "MIRROR" if state.mirror_view else "NORMAL"
            cv2.putText(frame, f"[{src_str}] {state.live_fps:.1f} FPS | {mirror_str}", (18, 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.38, (230, 215, 190), 1)

            cv2.rectangle(frame, (w - 200, 10), (w - 10, 40), (18, 14, 10), -1)
            cv2.rectangle(frame, (w - 200, 10), (w - 10, 40), (70, 52, 35), 1)
            yolo_str = "YOLO: ON" if state.yolo_enabled else "YOLO: OFF"
            cv2.putText(frame, f"{yolo_str} ({len(state.detected_objects)} detected)", (w - 192, 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.38, (212, 175, 55) if state.yolo_enabled else (150, 135, 115), 1)

            _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')

            if is_synthetic or not state.camera_enabled:
                time.sleep(0.033)

    except GeneratorExit:
        pass
    finally:
        if cap is not None and cap.isOpened():
            cap.release()

@app.get("/video_feed")
def video_feed():
    return StreamingResponse(generate_frames(), media_type="multipart/x-mixed-replace; boundary=frame")

# ----------------- TELEMETRY & HARDWARE SIMULATION -----------------
t_step = 0.0
def update_telemetry_step():
    global t_step
    t_step += 0.1

    if state.speed_mps > 0.05:
        state.belt_pos_pct = (state.belt_pos_pct + (state.speed_mps / 3.5) * 1.5) % 100.0
        state.motor_rpm = round(1480.0 * (state.speed_mps / 3.5), 1)
    else:
        state.motor_rpm = 0.0

    base_x = math.sin(t_step * 2.8) * 0.75 + random.uniform(-0.10, 0.10)
    base_y = math.cos(t_step * 2.4) * 0.60 + random.uniform(-0.08, 0.08)
    base_z = 9.81 + math.sin(t_step * 4.2) * 1.20 + random.uniform(-0.18, 0.18)

    if state.active_fault == "SPLICE_TEAR":
        state.tension_kn = max(10.0, state.tension_kn - 1.8)
        state.vibration_rms = min(14.5, state.vibration_rms + 0.6)
        state.bearing_temp_c += random.uniform(0.1, 0.3)
        base_x *= 3.8
        base_y *= 4.2
        base_z += math.sin(t_step * 10) * 5.0
        state.emergency_stop = True
        state.relay_nc_energized = False
        state.speed_mps = max(0.0, state.speed_mps - 0.4)
        state.throughput_tph = max(0.0, state.throughput_tph - 150.0)
        state.joint_rul_hours = max(0.8, state.joint_rul_hours - 15.0)
        state.zones["zone3"]["status"] = "SPLICE RUPTURE DETECTED"
        state.zones["zone3"]["level"] = 3
        state.zones["zone1"]["status"] = "MOTOR INTERLOCK TRIPPED"
        state.zones["zone1"]["level"] = 3

    elif state.active_fault == "BEARING_HOTSPOT":
        state.bearing_temp_c = min(98.5, state.bearing_temp_c + 1.2)
        state.vibration_rms = min(8.8, state.vibration_rms + 0.3)
        base_y *= 3.2
        if state.bearing_temp_c > 85.0:
            state.emergency_stop = True
            state.relay_nc_energized = False
            state.speed_mps = max(0.0, state.speed_mps - 0.4)
            state.throughput_tph = max(0.0, state.throughput_tph - 150.0)
        state.joint_rul_hours = max(6.0, state.joint_rul_hours - 8.0)
        state.zones["zone1"]["status"] = "BEARING HOTSPOT (>85°C)"
        state.zones["zone1"]["level"] = 3

    elif state.active_fault == "TENSION_SURGE":
        state.tension_kn = min(78.0, state.tension_kn + 2.5)
        if state.tension_kn > 65.0:
            state.emergency_stop = True
            state.relay_nc_energized = False
            state.speed_mps = max(0.0, state.speed_mps - 0.4)
            state.throughput_tph = max(0.0, state.throughput_tph - 150.0)
        state.joint_rul_hours = max(18.0, state.joint_rul_hours - 10.0)
        state.zones["zone4"]["status"] = "TENSION OVERLOAD (>65kN)"
        state.zones["zone4"]["level"] = 3

    elif state.active_fault == "MISALIGNMENT":
        state.misalignment_mm = min(62.0, state.misalignment_mm + 2.8)
        base_x += math.copysign(3.5, state.misalignment_mm)
        if abs(state.misalignment_mm) > 40.0:
            state.emergency_stop = True
            state.relay_nc_energized = False
            state.speed_mps = max(0.0, state.speed_mps - 0.4)
            state.throughput_tph = max(0.0, state.throughput_tph - 150.0)
        state.joint_rul_hours = max(28.0, state.joint_rul_hours - 5.0)
        state.zones["zone2"]["status"] = "LATERAL SWAY DRIFT"
        state.zones["zone2"]["level"] = 3

    elif state.active_fault == "SURFACE_GOUGE":
        state.vibration_rms = min(4.8, state.vibration_rms + 0.15)
        state.joint_rul_hours = max(72.0, state.joint_rul_hours - 2.0)
        state.zones["zone3"]["status"] = "SURFACE GOUGE DETECTED"
        state.zones["zone3"]["level"] = 2

    elif state.active_fault == "ORE_DUST":
        state.surface_material = "HEAVY IRON ORE ACCUMULATION (BENIGN - FILTERED)"
        state.zones["zone2"]["status"] = "IRON ORE LAYER (BENIGN)"
        state.zones["zone2"]["level"] = 1

    else:
        state.tension_kn += random.uniform(-0.25, 0.25)
        state.bearing_temp_c += random.uniform(-0.12, 0.12)
        state.misalignment_mm += random.uniform(-0.18, 0.18)
        state.vibration_rms += random.uniform(-0.06, 0.06)
        state.speed_mps = 3.5
        state.throughput_tph = 1250.0
        state.joint_rul_hours = 720.0
        for zkey in state.zones:
            state.zones[zkey]["status"] = "NOMINAL"
            state.zones[zkey]["level"] = 1

    state.tension_kn = max(0.0, min(85.0, state.tension_kn))
    state.bearing_temp_c = max(20.0, min(120.0, state.bearing_temp_c))
    state.vibration_rms = max(0.5, min(16.0, state.vibration_rms))
    state.vibration_x = round(base_x, 2)
    state.vibration_y = round(base_y, 2)
    state.vibration_z = round(base_z, 2)

    pen_tension = abs(state.tension_kn - 45.0) * 1.6
    pen_temp = max(0.0, state.bearing_temp_c - 60.0) * 1.8
    pen_vib = max(0.0, state.vibration_rms - 2.8) * 7.0
    pen_align = max(0.0, abs(state.misalignment_mm) - 25.0) * 1.4
    
    state.bhi = max(0.0, min(100.0, round(100.0 - (pen_tension + pen_temp + pen_vib + pen_align), 1)))

    if state.emergency_stop or state.bhi < 40.0:
        state.status = "LEVEL 3: CRITICAL (RELAY TRIPPED)"
        state.status_level = 3
    elif state.bhi < 75.0:
        state.status = "LEVEL 2: WARNING"
        state.status_level = 2
    else:
        state.status = "LEVEL 1: OPTIMAL"
        state.status_level = 1

# ----------------- REST API ENDPOINTS -----------------
@app.post("/api/inject_fault")
def inject_fault(req: FaultRequest):
    state.active_fault = req.fault_type
    state.injected_defect = req.fault_type
    
    if req.fault_type == "SPLICE_TEAR":
        state.emergency_stop = True
        state.relay_nc_energized = False
        state.tension_kn = 18.5
        state.vibration_rms = 9.8
        state.speed_mps = 0.0
        state.throughput_tph = 0.0
        state.joint_rul_hours = 1.8
        state.bhi = 20.0
        state.status = "LEVEL 3: CRITICAL (RELAY TRIPPED)"
        state.status_level = 3
        msg = "Critical Splice Joint Rupture Detected! Normally Closed (NC) Safety Relay Tripped - Motor Cutoff Engaged"
        sev = "CRITICAL"

    elif req.fault_type == "BEARING_HOTSPOT":
        state.bearing_temp_c = 94.2
        state.vibration_rms = 6.8
        state.emergency_stop = True
        state.relay_nc_energized = False
        state.speed_mps = 0.0
        state.throughput_tph = 0.0
        state.joint_rul_hours = 12.0
        state.bhi = 34.0
        state.status = "LEVEL 3: CRITICAL (OVERHEAT TRIP)"
        state.status_level = 3
        msg = "Drive Pulley Bearing Hotspot Exceeded 90°C! Automated E-Stop Interlock Engaged"
        sev = "CRITICAL"

    elif req.fault_type == "TENSION_SURGE":
        state.tension_kn = 74.5
        state.emergency_stop = True
        state.relay_nc_energized = False
        state.speed_mps = 0.0
        state.throughput_tph = 0.0
        state.joint_rul_hours = 24.0
        state.bhi = 36.0
        state.status = "LEVEL 3: CRITICAL (TENSION OVERLOAD)"
        state.status_level = 3
        msg = "HX711 Strain Gauge Array Detected Tension Surge (>70 kN)! Motor Cutoff Triggered"
        sev = "CRITICAL"

    elif req.fault_type == "MISALIGNMENT":
        state.misalignment_mm = 58.0
        state.emergency_stop = True
        state.relay_nc_energized = False
        state.speed_mps = 0.0
        state.throughput_tph = 0.0
        state.joint_rul_hours = 36.0
        state.bhi = 38.0
        state.status = "LEVEL 3: CRITICAL (SWAY LIMIT TRIP)"
        state.status_level = 3
        msg = "Severe Lateral Belt Sway (>40mm)! Edge Proximity Interlock Activated"
        sev = "CRITICAL"

    elif req.fault_type == "SURFACE_GOUGE":
        state.vibration_rms = 4.4
        state.joint_rul_hours = 96.0
        state.bhi = 65.0
        state.status = "LEVEL 2: WARNING (SCHEDULE REROLL)"
        state.status_level = 2
        msg = "Surface Gouge Defect Flagged by YOLOv8n. Logged Maintenance Ticket with RUL = 96 hrs"
        sev = "WARNING"

    elif req.fault_type == "ORE_DUST":
        state.surface_material = "HEAVY IRON ORE ACCUMULATION (BENIGN - FILTERED)"
        state.bhi = 92.0
        state.joint_rul_hours = 680.0
        state.status = "LEVEL 1: OPTIMAL (ORE DUST FILTERED)"
        state.status_level = 1
        msg = "Heavy Iron Ore Dust Layer Detected. Vision Filter Validated Ore Hemispheres & Suppressed False Alarm"
        sev = "INFO"
    else:
        msg = f"Custom fault mode engaged: {req.fault_type}"
        sev = "WARNING"

    log_entry = {
        "timestamp": time.strftime("%H:%M:%S"),
        "event": msg,
        "severity": sev
    }
    state.incident_log.insert(0, log_entry)
    return {"status": "injected", "fault": req.fault_type, "message": msg}

@app.post("/api/reset")
def reset_system():
    state.active_fault = "NONE"
    state.injected_defect = "NONE"
    state.emergency_stop = False
    state.relay_nc_energized = True
    state.tension_kn = 45.0
    state.bearing_temp_c = 52.4
    state.vibration_rms = 2.10
    state.vibration_x = 0.42
    state.vibration_y = -0.28
    state.vibration_z = 9.81
    state.misalignment_mm = 2.1
    state.speed_mps = 3.5
    state.throughput_tph = 1250.0
    state.surface_material = "NORMAL OPERATING DUST COAT"
    state.joint_rul_hours = 720.0
    state.bhi = 98.0
    state.status = "LEVEL 1: OPTIMAL"
    state.status_level = 1
    
    for zkey in state.zones:
        state.zones[zkey]["status"] = "NOMINAL"
        state.zones[zkey]["level"] = 1

    log_entry = {
        "timestamp": time.strftime("%H:%M:%S"),
        "event": "Safety Interlock Reset by SCADA Operator · Drive Contactor Energized · Baseline Restored",
        "severity": "INFO"
    }
    state.incident_log.insert(0, log_entry)
    return {"status": "reset", "emergency_stop": False, "active_fault": "NONE"}

@app.post("/api/set_twin_params")
def set_twin_params(req: TwinStimulateRequest):
    if not state.emergency_stop:
        state.speed_mps = max(0.0, min(5.0, req.speed_mps))
        state.throughput_tph = max(0.0, min(2500.0, req.throughput_tph))
    return {
        "speed_mps": state.speed_mps,
        "throughput_tph": state.throughput_tph,
        "motor_running": state.speed_mps > 0.1
    }

@app.post("/api/toggle_camera")
def toggle_camera():
    state.camera_enabled = not state.camera_enabled
    log_entry = {
        "timestamp": time.strftime("%H:%M:%S"),
        "event": f"Zone 3 Camera Feed {'ENABLED' if state.camera_enabled else 'PLACED ON STANDBY'}",
        "severity": "INFO"
    }
    state.incident_log.insert(0, log_entry)
    return {"camera_enabled": state.camera_enabled, "source": state.camera_source}

@app.post("/api/set_camera_config")
def set_camera_config(req: CameraConfigRequest):
    if req.source in ["esp32", "webcam", "sim"]:
        state.camera_source = req.source
    if req.esp32_url and req.esp32_url.strip():
        state.esp32_url = req.esp32_url.strip()
    
    log_entry = {
        "timestamp": time.strftime("%H:%M:%S"),
        "event": f"Zone 3 Optical Source changed to {state.camera_source.upper()} ({state.esp32_url if state.camera_source == 'esp32' else 'Index 0'})",
        "severity": "INFO"
    }
    state.incident_log.insert(0, log_entry)
    return {
        "source": state.camera_source,
        "esp32_url": state.esp32_url,
        "camera_enabled": state.camera_enabled
    }

@app.post("/api/toggle_mirror")
def toggle_mirror():
    state.mirror_view = not state.mirror_view
    return {"mirror_view": state.mirror_view}

@app.post("/api/toggle_yolo")
def toggle_yolo():
    state.yolo_enabled = not state.yolo_enabled
    return {"yolo_enabled": state.yolo_enabled}

@app.post("/api/toggle_device")
def toggle_device():
    state.device_connected = not state.device_connected
    log_entry = {
        "timestamp": time.strftime("%H:%M:%S"),
        "event": f"Hardware IoT channel {'CONNECTED (MPU6050 + DS18B20 + HX711)' if state.device_connected else 'SWITCHED TO DIGITAL TWIN'}",
        "severity": "INFO"
    }
    state.incident_log.insert(0, log_entry)
    return {"device_connected": state.device_connected}

# ----------------- WEBSOCKET TELEMETRY DISPATCHER -----------------
@app.websocket("/ws")
async def websocket_telemetry(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            update_telemetry_step()
            payload = {
                "bhi": state.bhi,
                "status": state.status,
                "status_level": state.status_level,
                "emergency_stop": state.emergency_stop,
                "relay_nc_energized": state.relay_nc_energized,
                "active_fault": state.active_fault,
                "joint_rul_hours": round(state.joint_rul_hours, 1),
                "speed_mps": round(state.speed_mps, 1),
                "throughput_tph": round(state.throughput_tph, 0),
                "belt_pos_pct": round(state.belt_pos_pct, 1),
                "motor_rpm": round(state.motor_rpm, 0),
                "tension": round(state.tension_kn, 1),
                "temperature": round(state.bearing_temp_c, 1),
                "vibration": round(state.vibration_rms, 2),
                "vib_x": state.vibration_x,
                "vib_y": state.vibration_y,
                "vib_z": state.vibration_z,
                "misalignment_mm": round(state.misalignment_mm, 1),
                "surface_material": state.surface_material,
                "camera_connected": state.camera_enabled,
                "camera_source": state.camera_source,
                "camera_status": state.camera_status,
                "mirror_view": state.mirror_view,
                "yolo_enabled": state.yolo_enabled,
                "live_fps": state.live_fps,
                "detected_objects": state.detected_objects,
                "device_connected": state.device_connected,
                "zones": state.zones,
                "modbus_status": state.modbus_status,
                "opcua_status": state.opcua_status,
                "incidents": state.incident_log[:14]
            }
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(0.1)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass

# ----------------- UI TEMPLATES (SPACIOUS · BROWN & GOLDEN YELLOW IN DARK MODE) -----------------
LANDING_HTML = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AYASK — NMDC Conveyor AI SCADA Command Center</title>
    <link rel="icon" type="image/png" href="/static/ayask_logo.png">
    <link rel="shortcut icon" href="/static/ayask_logo.png">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        :root[data-theme="dark"] {
            --bg-base: #0e0c0a;
            --bg-panel: #161310;
            --bg-card: #1c1814;
            --bg-input: #120f0c;
            --border: #33281e;
            --border-hover: #543f2a;
            --text-primary: #f5eedf;
            --text-secondary: #c4b5a0;
            --text-muted: #8a7b68;
            --copper: #d4af37;
            --copper-hover: #e5be42;
            --copper-subtle: rgba(212, 175, 55, 0.12);
            --danger: #a84825;
            --success: #d4af37;
            --shadow-float: 0 16px 36px rgba(0, 0, 0, 0.55);
        }
        :root[data-theme="light"] {
            --bg-base: #eef2f7;
            --bg-panel: #ffffff;
            --bg-card: #ffffff;
            --bg-input: #f8fafc;
            --border: #cbd5e1;
            --border-hover: #94a3b8;
            --text-primary: #0f172a;
            --text-secondary: #475569;
            --text-muted: #64748b;
            --copper: #8c5d33;
            --copper-hover: #754b25;
            --copper-subtle: rgba(140, 93, 51, 0.08);
            --danger: #b91c1c;
            --success: #15803d;
            --shadow-float: 0 10px 28px rgba(0, 0, 0, 0.08);
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', sans-serif; transition: background-color 0.25s, border-color 0.25s, color 0.25s; }
        body { background: var(--bg-base); color: var(--text-primary); min-height: 100vh; display: flex; flex-direction: column; }

        .portal-header {
            display: flex; justify-content: space-between; align-items: center;
            padding: 16px 40px; background: var(--bg-panel); border-bottom: 1px solid var(--border);
            box-shadow: 0 4px 20px rgba(0,0,0,0.12);
        }
        .brand-box { display: flex; align-items: center; gap: 16px; }
        .ayask-header-logo {
            height: 52px; width: auto; max-width: 75px; object-fit: contain;
            filter: drop-shadow(0 2px 8px rgba(0,0,0,0.25));
        }
        .brand-text h1 { font-size: 18px; font-weight: 800; letter-spacing: 1.2px; }
        .brand-text p { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }

        .portal-content {
            flex: 1; display: grid; grid-template-columns: 1.15fr 0.85fr;
            max-width: 1320px; width: 100%; margin: 0 auto; padding: 60px 32px; gap: 60px; align-items: center;
        }

        .floating-card {
            background: var(--bg-panel); border: 1px solid var(--border);
            border-radius: 16px; padding: 42px 38px; box-shadow: var(--shadow-float);
            transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1), box-shadow 0.25s;
        }
        .floating-card:hover { transform: translateY(-4px); }

        .spec-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-top: 24px; }
        .spec-item { background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px; padding: 16px 18px; }
        .spec-item .label { font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--text-muted); }
        .spec-item .val { font-size: 14px; font-weight: 600; font-family: 'JetBrains Mono', monospace; margin-top: 6px; }

        .auth-tabs { display: flex; border-bottom: 1px solid var(--border); margin-bottom: 28px; }
        .auth-tab {
            flex: 1; text-align: center; padding: 12px 0; font-size: 14px; font-weight: 600;
            color: var(--text-secondary); cursor: pointer; border-bottom: 2px solid transparent;
        }
        .auth-tab.active { color: var(--copper); border-bottom-color: var(--copper); }

        .form-group { margin-bottom: 18px; }
        .form-group label { display: block; font-size: 11.5px; font-weight: 600; text-transform: uppercase; margin-bottom: 8px; color: var(--text-secondary); }
        .form-group input {
            width: 100%; padding: 13px 16px; background: var(--bg-input);
            border: 1px solid var(--border); border-radius: 10px; color: var(--text-primary); font-size: 13.5px; outline: none;
        }
        .form-group input:focus { border-color: var(--copper); }

        .btn-submit {
            width: 100%; padding: 14px; background: var(--copper); border: none; border-radius: 10px;
            color: #0e0c0a; font-size: 14px; font-weight: 700; cursor: pointer;
            box-shadow: 0 4px 16px rgba(212, 175, 55, 0.25);
            transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .btn-submit:hover { background: var(--copper-hover); transform: translateY(-2px); }

        .btn-quick {
            background: transparent; border: 1px solid var(--copper); color: var(--copper);
            padding: 8px 14px; border-radius: 8px; font-size: 12px; font-weight: 600; cursor: pointer;
            display: flex; align-items: center; gap: 6px;
        }
        .btn-quick:hover { background: var(--copper); color: #0e0c0a; }
    </style>
</head>
<body>
    <header class="portal-header">
        <div class="brand-box">
            <img src="/static/ayask_logo.png" alt="AYASK Logo" class="ayask-header-logo">
            <div class="brand-text">
                <h1>TEAM AYASK · NMDC SCADA</h1>
                <p>SIH 26008 · Heavy Mining Conveyor AI Diagnostic Command Center</p>
            </div>
        </div>
        <button onclick="toggleTheme()" class="btn-quick">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>
            <span id="themeLabel">Light Mode</span>
        </button>
    </header>

    <main class="portal-content">
        <section>
            <div style="display:inline-block; padding:5px 14px; background:var(--copper-subtle); border:1px solid var(--copper); border-radius:8px; font-size:11.5px; font-weight:700; color:var(--copper); margin-bottom:16px;">
                TEAM AYASK · SIH PROBLEM STATEMENT 26008
            </div>
            <h2 style="font-size:38px; font-weight:800; line-height:1.2; margin-bottom:16px;">
                Edge AI & Multi-Modal IoT Conveyor Belt Predictive Maintenance
            </h2>
            <p style="font-size:15px; color:var(--text-secondary); line-height:1.7;">
                Engineered for NMDC iron ore handling plants. Real-time condition synthesis, joint remaining useful life (RUL) estimation,
                failsafe drive motor contactor E-Stop interlock, and Zone 3 return belt vision inspection.
            </p>

            <div class="spec-grid">
                <div class="spec-item">
                    <div class="label">Belt Health Synthesis</div>
                    <div class="val">Live BHI + Joint RUL Tracking</div>
                </div>
                <div class="spec-item">
                    <div class="label">Safety Interlock</div>
                    <div class="val">Normally Closed (NC) PLC Loop</div>
                </div>
                <div class="spec-item">
                    <div class="label">IoT Multi-Sensor</div>
                    <div class="val">MPU6050 + DS18B20 + HX711</div>
                </div>
                <div class="spec-item">
                    <div class="label">Vision Architecture</div>
                    <div class="val">YOLOv8 Edge Real-Time</div>
                </div>
            </div>
        </section>

        <section class="floating-card">
            <div class="auth-tabs">
                <div class="auth-tab active" id="tabIn" onclick="switchAuth('in')">Sign In</div>
                <div class="auth-tab" id="tabUp" onclick="switchAuth('up')">Register Operator</div>
            </div>

            <div id="authErr" style="padding:12px; border-radius:8px; background:rgba(168,72,37,0.16); border:1px solid var(--danger); color:#f5eedf; font-size:12.5px; margin-bottom:16px; display:none;"></div>
            <div id="authSucc" style="padding:12px; border-radius:8px; background:rgba(212,175,55,0.16); border:1px solid var(--copper); color:var(--copper); font-size:12.5px; margin-bottom:16px; display:none;"></div>

            <form onsubmit="submitAuth(event)">
                <div class="form-group" id="nameGrp" style="display:none;">
                    <label>Full Operator Name</label>
                    <input type="text" id="nameIn" placeholder="e.g. S. Ramanathan">
                </div>
                <div class="form-group">
                    <label>Corporate Employee ID / Email</label>
                    <input type="email" id="emailIn" placeholder="operator@nmdc.gov.in" required>
                </div>
                <div class="form-group">
                    <label>Secure Password</label>
                    <input type="password" id="passIn" placeholder="••••••••" required>
                </div>
                <button type="submit" class="btn-submit" id="submitBtn">Sign In to SCADA</button>
            </form>

            <div id="demoRow" style="margin-top:24px; padding:14px; background:var(--copper-subtle); border:1px solid var(--border); border-radius:10px; display:flex; justify-content:space-between; align-items:center; font-size:12px; color:var(--text-secondary);">
                <div><strong>Demo Access:</strong> operator@nmdc.gov.in / Admin@1234</div>
                <button type="button" class="btn-quick" onclick="quickFill()">Quick Fill</button>
            </div>
        </section>
    </main>

    <script>
        let mode = 'in';
        function switchAuth(m) {
            mode = m;
            document.getElementById('tabIn').className = m === 'in' ? 'auth-tab active' : 'auth-tab';
            document.getElementById('tabUp').className = m === 'up' ? 'auth-tab active' : 'auth-tab';
            document.getElementById('nameGrp').style.display = m === 'up' ? 'block' : 'none';
            document.getElementById('submitBtn').innerText = m === 'up' ? 'Register Operator Profile' : 'Sign In to SCADA';
            document.getElementById('demoRow').style.display = m === 'up' ? 'none' : 'flex';
        }
        function quickFill() {
            document.getElementById('emailIn').value = 'operator@nmdc.gov.in';
            document.getElementById('passIn').value = 'Admin@1234';
        }
        async function submitAuth(e) {
            e.preventDefault();
            const err = document.getElementById('authErr');
            const succ = document.getElementById('authSucc');
            err.style.display = 'none'; succ.style.display = 'none';

            const email = document.getElementById('emailIn').value.trim();
            const password = document.getElementById('passIn').value;
            const name = document.getElementById('nameIn').value.trim();
            const url = mode === 'in' ? '/api/signin' : '/api/signup';
            const payload = mode === 'in' ? { email, password } : { name, email, password };

            try {
                const res = await fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const d = await res.json();
                if (!res.ok) {
                    err.innerText = d.detail || 'Authentication failed.';
                    err.style.display = 'block';
                } else {
                    localStorage.setItem('nmdc_token', d.token);
                    localStorage.setItem('nmdc_user_name', d.name);
                    succ.innerText = 'Authorization verified! Redirecting to SCADA...';
                    succ.style.display = 'block';
                    setTimeout(() => window.location.href = '/dashboard', 500);
                }
            } catch(e) {
                err.innerText = 'Server communication error.';
                err.style.display = 'block';
            }
        }
        function applyTheme(t) {
            document.documentElement.setAttribute('data-theme', t);
            localStorage.setItem('nmdc_theme', t);
            document.getElementById('themeLabel').innerText = t === 'dark' ? 'Light Mode' : 'Dark Mode';
        }
        function toggleTheme() {
            const cur = document.documentElement.getAttribute('data-theme') || 'dark';
            applyTheme(cur === 'dark' ? 'light' : 'dark');
        }
        applyTheme(localStorage.getItem('nmdc_theme') || 'dark');
        if (localStorage.getItem('nmdc_token')) {
            fetch('/api/verify_session', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ token: localStorage.getItem('nmdc_token') })
            }).then(r => { if (r.ok) window.location.href = '/dashboard'; });
        }
    </script>
</body>
</html>
"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Team AYASK — NMDC Conveyor AI SCADA Command Center</title>
    <link rel="icon" type="image/png" href="/static/ayask_logo.png">
    <link rel="shortcut icon" href="/static/ayask_logo.png">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        /* ==============================================================================
           AYASK SCADA: ZERO EMOJIS · SPACIOUS EXPANSIVE LAYOUT
           DARK MODE: STRICTLY BROWN & GOLDEN YELLOW PALETTE (ZERO VIBRANT BLUES/REDS)
           LIGHT MODE: CRISP HIGH-CONTRAST INDUSTRIAL PALETTE
           ============================================================================== */
        :root[data-theme="dark"] {
            --bg-deep: #0e0c0a;
            --bg-panel: #161310;
            --bg-card: #1c1814;
            --bg-input: #120f0c;
            --border: #33281e;
            --border-hover: #543f2a;
            --text: #f5eedf;
            --text-dim: #c4b5a0;
            --text-faint: #8a7b68;
            
            /* Gold & Brown Theme Variables */
            --gold: #d4af37;
            --gold-hover: #e5be42;
            --gold-subtle: rgba(212, 175, 55, 0.12);
            --gold-border: #997c22;
            --brown: #8c5a32;
            --brown-hover: #9e693d;
            --brown-deep: #4a2f1b;
            --brown-subtle: rgba(140, 90, 50, 0.14);

            /* Non-vibrant earthy state variables */
            --accent-gold: #d4af37;
            --alert-burnt: #a84825;
            --alert-burnt-bg: rgba(168, 72, 37, 0.18);
            --alert-burnt-border: #7c3116;
            --safe-gold: #d4af37;
            --safe-gold-bg: rgba(212, 175, 55, 0.14);
            --safe-gold-border: #997c22;
            --warn-amber: #c68a3c;
            --warn-amber-bg: rgba(198, 138, 60, 0.14);
            --warn-amber-border: #8c5d22;

            --chart-grid: rgba(212, 175, 55, 0.08);
            --chart-tick: #c4b5a0;
            --shadow-float: 0 16px 40px rgba(0, 0, 0, 0.55);
            --shadow-skate: 0 24px 54px rgba(0, 0, 0, 0.70);
        }

        :root[data-theme="light"] {
            --bg-deep: #eef2f7;
            --bg-panel: #ffffff;
            --bg-card: #ffffff;
            --bg-input: #f8fafc;
            --border: #cbd5e1;
            --border-hover: #94a3b8;
            --text: #0f172a;
            --text-dim: #334155;
            --text-faint: #64748b;
            
            --gold: #8c5d33;
            --gold-hover: #734821;
            --gold-subtle: rgba(140, 93, 51, 0.08);
            --gold-border: #8c5d33;
            --brown: #8c5d33;
            --brown-hover: #734821;
            --brown-deep: #54371c;
            --brown-subtle: rgba(140, 93, 51, 0.08);

            --accent-gold: #8c5d33;
            --alert-burnt: #b91c1c;
            --alert-burnt-bg: rgba(220, 38, 38, 0.10);
            --alert-burnt-border: #fca5a5;
            --safe-gold: #15803d;
            --safe-gold-bg: rgba(22, 163, 74, 0.10);
            --safe-gold-border: #86efac;
            --warn-amber: #b45309;
            --warn-amber-bg: rgba(217, 119, 6, 0.10);
            --warn-amber-border: #fde68a;

            --chart-grid: rgba(15, 23, 42, 0.12);
            --chart-tick: #334155;
            --shadow-float: 0 10px 28px rgba(0, 0, 0, 0.07);
            --shadow-skate: 0 16px 36px rgba(0, 0, 0, 0.12);
        }

        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', sans-serif; transition: background-color 0.22s, border-color 0.22s, color 0.22s; }
        body { background: var(--bg-deep); color: var(--text); min-height: 100vh; display: flex; flex-direction: column; overflow-x: hidden; }

        /* Vectors / SVG Icon helper */
        .scada-icon { width: 17px; height: 17px; display: inline-block; vertical-align: middle; stroke-width: 2; stroke: currentColor; fill: none; stroke-linecap: round; stroke-linejoin: round; }

        /* Floating Topbar Header - Spacious */
        .topbar {
            position: sticky; top: 0; z-index: 100;
            display: flex; align-items: center; justify-content: space-between;
            padding: 16px 36px; background: var(--bg-panel);
            border-bottom: 1px solid var(--border); box-shadow: var(--shadow-float);
            backdrop-filter: blur(14px);
        }
        .ayask-brand { display: flex; align-items: center; gap: 16px; }
        .ayask-header-logo {
            height: 52px; width: auto; max-width: 80px; object-fit: contain;
            filter: drop-shadow(0 2px 8px rgba(0,0,0,0.3));
        }
        .ayask-text h1 { font-size: 17px; font-weight: 800; letter-spacing: 1px; color: var(--gold); }
        .ayask-text p { font-size: 12px; color: var(--text-dim); margin-top: 2px; }

        .topbar-actions { display: flex; align-items: center; gap: 14px; }
        .chip {
            display: flex; align-items: center; gap: 8px; padding: 8px 16px;
            background: var(--bg-card); border: 1px solid var(--border);
            border-radius: 10px; font-size: 13px; font-weight: 600;
        }
        .pulse-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--safe-gold); }
        .pulse-dot.danger { background: var(--alert-burnt); }
        .pulse-dot.warning { background: var(--warn-amber); }

        /* Buttons with generous breathing room */
        .btn-ctrl {
            background: var(--bg-card); border: 1px solid var(--border); color: var(--text);
            padding: 9px 18px; border-radius: 10px; font-size: 13px; font-weight: 600;
            cursor: pointer; display: flex; align-items: center; gap: 8px;
            transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .btn-ctrl:hover {
            border-color: var(--border-hover); transform: translateY(-3px);
            box-shadow: 0 8px 20px rgba(0,0,0,0.3);
        }
        .btn-ctrl:active { transform: translateY(0); }
        .btn-ctrl.btn-danger { background: var(--alert-burnt-bg); border-color: var(--alert-burnt-border); color: var(--alert-burnt); }
        .btn-ctrl.btn-danger:hover { background: var(--alert-burnt); color: #fff; }
        .btn-ctrl.btn-copper { background: var(--gold-subtle); border-color: var(--gold); color: var(--gold); }
        .btn-ctrl.btn-success { background: var(--safe-gold-bg); border-color: var(--safe-gold-border); color: var(--safe-gold); }

        /* Main SCADA Grid - Generous, Uncluttered, Free */
        .scada-container {
            flex: 1; padding: 32px 40px; display: grid; grid-template-columns: 1.16fr 0.84fr;
            gap: 32px; max-width: 1820px; margin: 0 auto; width: 100%;
        }

        /* Floating Spacious Cards */
        .card {
            background: var(--bg-panel); border: 1px solid var(--border);
            border-radius: 16px; overflow: hidden; display: flex; flex-direction: column;
            box-shadow: var(--shadow-float);
            transition: transform 0.28s cubic-bezier(0.16, 1, 0.3, 1), box-shadow 0.28s;
        }
        .card:hover { transform: translateY(-4px); box-shadow: var(--shadow-skate); }

        .card-header {
            padding: 16px 24px; background: var(--bg-card); border-bottom: 1px solid var(--border);
            display: flex; align-items: center; justify-content: space-between;
        }
        .card-title { font-size: 13.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.8px; display: flex; align-items: center; gap: 10px; color: var(--gold); }
        .card-body { padding: 24px 26px; flex: 1; }

        /* KPI Floating Strip - Large & Clear */
        .kpi-strip { display: grid; grid-template-columns: repeat(4, 1fr); gap: 18px; margin-bottom: 24px; }
        .kpi-box {
            background: var(--bg-card); border: 1px solid var(--border); border-radius: 14px;
            padding: 18px 20px; box-shadow: var(--shadow-float);
            transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .kpi-box:hover { transform: translateY(-3px); }
        .kpi-box .kpi-label { font-size: 11px; font-weight: 700; text-transform: uppercase; color: var(--text-dim); margin-bottom: 6px; letter-spacing: 0.5px; }
        .kpi-box .kpi-val { font-size: 27px; font-weight: 800; font-family: 'JetBrains Mono', monospace; color: var(--text); }
        .kpi-box .kpi-sub { font-size: 12px; color: var(--text-faint); margin-top: 6px; }

        /* Video Section - Big & Free */
        .video-box {
            position: relative; width: 100%; height: 390px; background: #000;
            border-radius: 12px; overflow: hidden; border: 1px solid var(--border);
        }
        .video-box img { width: 100%; height: 100%; object-fit: contain; display: block; }
        .video-controls { margin-top: 18px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }

        .detection-badge-row {
            margin-top: 16px; padding: 14px 18px; background: var(--bg-card);
            border: 1px solid var(--border); border-radius: 10px; font-size: 13px;
            display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
        }
        .det-pill {
            background: var(--gold-subtle); border: 1px solid var(--gold); color: var(--gold);
            padding: 4px 10px; border-radius: 8px; font-size: 12px; font-family: 'JetBrains Mono', monospace; font-weight: 700;
        }

        /* Digital Twin Mechanics Simulation - Spacious & Big */
        .digital-twin-container {
            background: var(--bg-card); border: 1px solid var(--border); border-radius: 14px;
            padding: 22px; margin-bottom: 20px; position: relative;
        }
        .digital-twin-canvas-wrap {
            width: 100%; height: 180px; background: var(--bg-input); border-radius: 10px;
            border: 1px solid var(--border); overflow: hidden; position: relative;
        }
        .digital-twin-canvas-wrap canvas { width: 100%; height: 100%; display: block; }

        .stimulation-controls {
            margin-top: 18px; display: grid; grid-template-columns: 1fr 1fr 140px; gap: 18px; align-items: center;
        }
        .stim-slider-wrap label { font-size: 11px; font-weight: 700; color: var(--text-dim); text-transform: uppercase; display: flex; justify-content: space-between; margin-bottom: 6px; }
        .stim-slider-wrap input[type=range] { width: 100%; accent-color: var(--gold); cursor: pointer; height: 6px; }

        .digital-twin-grid {
            display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-top: 16px;
        }
        .zone-card {
            background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px;
            padding: 14px 16px; font-size: 12.5px;
            transition: transform 0.2s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .zone-card:hover { transform: translateY(-2px); }
        .zone-card .z-num { font-size: 10.5px; font-weight: 700; color: var(--text-dim); text-transform: uppercase; }
        .zone-card .z-title { font-weight: 700; margin: 4px 0; }
        .zone-card .z-status { font-size: 11px; font-weight: 600; color: var(--safe-gold); font-family: 'JetBrains Mono', monospace; }

        /* Relay Interlock Panel - Generous */
        .relay-panel {
            background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px;
            padding: 18px 22px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center;
        }
        .relay-status-text { font-size: 14px; font-weight: 800; font-family: 'JetBrains Mono', monospace; margin-top: 4px; }

        /* Fault Buttons Strip - Spacious 3-Column */
        .fault-strip { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-top: 14px; }
        .btn-fault {
            background: var(--bg-card); border: 1px solid var(--border); color: var(--text);
            padding: 14px 16px; border-radius: 10px; font-size: 13px; font-weight: 600;
            cursor: pointer; text-align: left; display: flex; align-items: center; gap: 10px;
            transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .btn-fault:hover { transform: translateY(-3px); border-color: var(--border-hover); box-shadow: 0 8px 20px rgba(0,0,0,0.3); }
        .btn-fault.active-fault {
            background: var(--alert-burnt-bg); border-color: var(--gold); color: var(--gold);
            box-shadow: 0 0 16px rgba(212, 175, 55, 0.35); font-weight: 700;
        }
        .btn-fault.btn-reset-full {
            grid-column: span 3; text-align: center; justify-content: center;
            background: var(--safe-gold-bg); border-color: var(--safe-gold-border); color: var(--safe-gold);
            font-size: 14px; padding: 15px; font-weight: 700;
        }
        .btn-fault.btn-reset-full:hover { background: var(--gold); color: #0e0c0a; }

        /* XYZ Radar Canvas - Spacious */
        .xyz-box { display: grid; grid-template-columns: 1fr 160px; gap: 18px; align-items: center; }
        .xyz-canvas-wrap {
            position: relative; width: 100%; height: 210px; background: var(--bg-card);
            border: 1px solid var(--border); border-radius: 12px; overflow: hidden;
        }
        .xyz-canvas-wrap canvas { width: 100%; height: 100%; display: block; }
        .xyz-meta-item {
            background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px;
            padding: 12px 14px; margin-bottom: 10px;
        }
        .xyz-meta-item .ax { font-size: 10.5px; font-weight: 700; color: var(--text-dim); text-transform: uppercase; }
        .xyz-meta-item .val { font-size: 15px; font-weight: 700; font-family: 'JetBrains Mono', monospace; color: var(--gold); margin-top: 3px; }

        /* Floating Toast Alert */
        .toast-banner {
            position: fixed; top: 85px; left: 50%; transform: translateX(-50%) translateY(-100px);
            background: var(--bg-panel); border: 1.5px solid var(--gold); border-radius: 12px;
            padding: 14px 28px; box-shadow: var(--shadow-skate); z-index: 999;
            font-size: 13.5px; font-weight: 700; color: var(--text);
            display: flex; align-items: center; gap: 12px; pointer-events: none; opacity: 0;
            transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        }
        .toast-banner.show { transform: translateX(-50%) translateY(0); opacity: 1; }

        /* Protocol Feeds & Incidents - Spacious */
        .feed-bar {
            padding: 12px 18px; background: var(--bg-card); border: 1px solid var(--border);
            border-radius: 10px; font-size: 12px; display: flex; justify-content: space-between; margin-bottom: 16px;
            font-family: 'JetBrains Mono', monospace; color: var(--gold);
        }

        table.inc-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
        table.inc-table th { background: var(--bg-card); color: var(--text-dim); text-align: left; padding: 10px 14px; font-weight: 600; border-bottom: 1px solid var(--border); }
        table.inc-table td { padding: 10px 14px; border-bottom: 1px solid var(--border); }
        table.inc-table tr:hover { background: var(--bg-card); }
    </style>
</head>
<body>
    <div class="toast-banner" id="toastAlert">
        <svg class="scada-icon" style="stroke:var(--gold); width:20px; height:20px;" viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        <span id="toastMsg">System Notification</span>
    </div>

    <header class="topbar">
        <div class="ayask-brand">
            <img src="/static/ayask_logo.png" alt="AYASK Logo" class="ayask-header-logo">
            <div class="ayask-text">
                <h1>NMDC CONVEYOR AI SCADA</h1>
                <p>Team AYASK · SIH 26008 · Multi-Modal IoT & Edge AI Digital Twin System</p>
            </div>
        </div>

        <div class="topbar-actions">
            <button class="btn-ctrl btn-copper" onclick="openJuryModal()" title="View SIH 26008 Architecture Defense">
                <svg class="scada-icon" viewBox="0 0 24 24"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
                <span>Jury Briefing</span>
            </button>

            <div class="chip">
                <span class="pulse-dot" id="headerPulse"></span>
                <span id="headerStatus">LEVEL 1: OPTIMAL</span>
            </div>

            <div class="chip">
                <span>BHI:</span>
                <strong id="headerBhi" style="font-family:'JetBrains Mono',monospace; color:var(--gold);">98.0%</strong>
            </div>

            <div class="chip">
                <span>RUL:</span>
                <strong id="headerRul" style="font-family:'JetBrains Mono',monospace; color:var(--safe-gold);">720 hrs</strong>
            </div>

            <button class="btn-ctrl" onclick="toggleTheme()" id="themeBtn" title="Toggle Light / Dark Mode">
                <svg class="scada-icon" id="themeIconSvg" viewBox="0 0 24 24"><circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/></svg>
                <span id="themeLabel">Light Mode</span>
            </button>

            <button class="btn-ctrl btn-danger" onclick="injectFault('SPLICE_TEAR')" title="Manual Emergency Cutoff">
                <svg class="scada-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
                <span>E-STOP TRIP</span>
            </button>

            <button class="btn-ctrl" onclick="signOut()" title="Sign Out">
                <svg class="scada-icon" viewBox="0 0 24 24"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
            </button>
        </div>
    </header>

    <main class="scada-container">
        <!-- LEFT COLUMN: Computer Vision, Digital Twin & XYZ Motion -->
        <section style="display:flex; flex-direction:column; gap:28px;">
            <!-- Zone 3 Camera & Optical Scanner -->
            <div class="card">
                <div class="card-header">
                    <div class="card-title">
                        <svg class="scada-icon" viewBox="0 0 24 24"><path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
                        <span>Vision AI: Zone 3 Return Belt Scanner</span>
                        <span id="camSourcePill" style="font-size:11.5px; padding:3px 10px; border-radius:6px; background:var(--bg-card); border:1px solid var(--border); color:var(--gold);">WEBCAM 0</span>
                    </div>
                    <div style="display:flex; gap:10px; align-items:center;">
                        <span id="fpsReadout" style="font-size:12px; font-family:'JetBrains Mono',monospace; color:var(--text-dim);">0.0 FPS</span>
                        <button class="btn-ctrl btn-copper" onclick="openSettingsModal()">
                            <svg class="scada-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
                            <span>Settings</span>
                        </button>
                    </div>
                </div>
                <div class="card-body">
                    <div class="video-box">
                        <img src="/video_feed" alt="Zone 3 Return Belt Inspection Feed">
                    </div>

                    <div class="video-controls">
                        <div style="display:flex; gap:10px;">
                            <button class="btn-ctrl" id="btnCam" onclick="toggleCam()">
                                <svg class="scada-icon" viewBox="0 0 24 24"><path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
                                <span>Enable Camera</span>
                            </button>
                            <button class="btn-ctrl" id="btnMirror" onclick="toggleMirror()">
                                <svg class="scada-icon" viewBox="0 0 24 24"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>
                                <span>Mirror: OFF</span>
                            </button>
                            <button class="btn-ctrl" id="btnYolo" onclick="toggleYolo()">
                                <svg class="scada-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/></svg>
                                <span>AI Detect: ON</span>
                            </button>
                        </div>
                        <span id="camStatus" style="font-size:12px; color:var(--text-dim);">Status: Standby</span>
                    </div>

                    <div class="detection-badge-row">
                        <strong style="color:var(--text-dim);">Iron Ore Defect Detections:</strong>
                        <span id="detList" style="color:var(--text-faint); font-style:italic;">Scanning nominal return belt surface...</span>
                    </div>
                </div>
            </div>

            <!-- Digital Twin Conveyor Stimulation Visualizer -->
            <div class="card">
                <div class="card-header">
                    <div class="card-title">
                        <svg class="scada-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
                        <span>Conveyor Digital Twin Simulation (Zones 1–4)</span>
                    </div>
                    <span style="font-size:12px; font-family:'JetBrains Mono',monospace; color:var(--gold);" id="twinCycleReadout">Cycle: Splice #142 @ 0.0%</span>
                </div>
                <div class="card-body">
                    <div class="digital-twin-container">
                        <div class="digital-twin-canvas-wrap">
                            <canvas id="digitalTwinCanvas" width="700" height="180"></canvas>
                        </div>

                        <!-- Stimulation Controls -->
                        <div class="stimulation-controls">
                            <div class="stim-slider-wrap">
                                <label>
                                    <span>Belt Speed Regulation</span>
                                    <span id="stimSpeedVal" style="color:var(--gold); font-family:'JetBrains Mono',monospace;">3.5 m/s</span>
                                </label>
                                <input type="range" id="sliderSpeed" min="0" max="5" step="0.1" value="3.5" oninput="stimulateTwin()">
                            </div>
                            <div class="stim-slider-wrap">
                                <label>
                                    <span>Ore Mass Throughput</span>
                                    <span id="stimLoadVal" style="color:var(--gold); font-family:'JetBrains Mono',monospace;">1,250 TPH</span>
                                </label>
                                <input type="range" id="sliderLoad" min="0" max="2500" step="50" value="1250" oninput="stimulateTwin()">
                            </div>
                            <div>
                                <button class="btn-ctrl btn-copper" style="width:100%; justify-content:center; padding:12px 14px;" onclick="toggleMotorTwin()">
                                    <svg class="scada-icon" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                                    <span id="btnMotorLabel">Motor Stop</span>
                                </button>
                            </div>
                        </div>
                    </div>

                    <div class="digital-twin-grid">
                        <div class="zone-card" id="cardZone1">
                            <div class="z-num">Zone 1 · Head</div>
                            <div class="z-title">Drive Motor (415V)</div>
                            <div class="z-status" id="z1Status">NOMINAL</div>
                        </div>
                        <div class="zone-card" id="cardZone2">
                            <div class="z-num">Zone 2 · Carrying</div>
                            <div class="z-title">Troughing Run & Ore</div>
                            <div class="z-status" id="z2Status">NOMINAL</div>
                        </div>
                        <div class="zone-card" id="cardZone3">
                            <div class="z-num">Zone 3 · Return</div>
                            <div class="z-title">Optical AI & MPU6050</div>
                            <div class="z-status" id="z3Status">NOMINAL</div>
                        </div>
                        <div class="zone-card" id="cardZone4">
                            <div class="z-num">Zone 4 · Tail</div>
                            <div class="z-title">Take-Up & HX711</div>
                            <div class="z-status" id="z4Status">NOMINAL</div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Tri-Axial Vibration Radar (MPU6050) -->
            <div class="card">
                <div class="card-header">
                    <div class="card-title">
                        <svg class="scada-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="22" y1="12" x2="18" y2="12"/><line x1="6" y1="12" x2="2" y2="12"/><line x1="12" y1="6" x2="12" y2="2"/><line x1="12" y1="22" x2="12" y2="18"/></svg>
                        <span>Tri-Axial Vibration Harmonics (XYZ Trajectory)</span>
                    </div>
                    <span style="font-size:12px; color:var(--text-dim);">MPU6050 Accelerometer Harvester</span>
                </div>
                <div class="card-body">
                    <div class="xyz-box">
                        <div class="xyz-canvas-wrap">
                            <canvas id="xyzCanvas" width="520" height="240"></canvas>
                        </div>
                        <div>
                            <div class="xyz-meta-item">
                                <div class="ax">X (Lateral)</div>
                                <div class="val" id="valX">+0.00 m/s²</div>
                            </div>
                            <div class="xyz-meta-item">
                                <div class="ax">Y (Longit)</div>
                                <div class="val" id="valY">+0.00 m/s²</div>
                            </div>
                            <div class="xyz-meta-item">
                                <div class="ax">Z (Normal)</div>
                                <div class="val" id="valZ">+9.81 m/s²</div>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </section>

        <!-- RIGHT COLUMN: Telemetry, Fault Injector & Interlock Safety -->
        <section style="display:flex; flex-direction:column; gap:28px;">
            <!-- Real-Time Belt Health Synthesis KPI Strip -->
            <div class="kpi-strip">
                <div class="kpi-box">
                    <div class="kpi-label">Joint RUL</div>
                    <div class="kpi-val" id="kpiRul" style="color:var(--safe-gold);">720 hrs</div>
                    <div class="kpi-sub">Est. Joint Life (~30d)</div>
                </div>
                <div class="kpi-box">
                    <div class="kpi-label">Belt Speed</div>
                    <div class="kpi-val" id="kpiSpeed">3.5 m/s</div>
                    <div class="kpi-sub">Target: 3.5 m/s</div>
                </div>
                <div class="kpi-box">
                    <div class="kpi-label">Throughput</div>
                    <div class="kpi-val" id="kpiLoad">1,250 TPH</div>
                    <div class="kpi-sub">Iron Ore Mass Flow</div>
                </div>
                <div class="kpi-box">
                    <div class="kpi-label">Splice Vib RMS</div>
                    <div class="kpi-val" id="kpiVib">2.10 mm/s</div>
                    <div class="kpi-sub">ISO 10816 Limit: 4.5</div>
                </div>
            </div>

            <!-- Drive Motor Interlock Status (Normally Closed Relay Safety Loop) -->
            <div class="card">
                <div class="card-header">
                    <div class="card-title">
                        <svg class="scada-icon" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
                        <span>Drive Motor Failsafe Interlock (NC Relay Loop)</span>
                    </div>
                    <span style="font-size:12px; color:var(--text-dim);">ISO 13849-1 Cat 4 / PL e</span>
                </div>
                <div class="card-body">
                    <div class="relay-panel">
                        <div>
                            <div style="font-size:11px; font-weight:700; color:var(--text-dim); text-transform:uppercase;">Drive Contactor Circuit State</div>
                            <div class="relay-status-text" id="relayText" style="color:var(--safe-gold);">NC RELAY CLOSED · CONTACTOR ENERGIZED (415V)</div>
                        </div>
                        <div class="chip" id="relayBadge">
                            <span class="pulse-dot" id="relayDot"></span>
                            <span id="relayBadgeText">MOTOR RUNNING</span>
                        </div>
                    </div>
                    <div style="font-size:12.5px; color:var(--text-dim); line-height:1.5;">
                        Failsafe hardware circuit: automated relay contactor breaks within &lt;120ms to prevent catastrophic tear propagation.
                    </div>
                </div>
            </div>

            <!-- SIH Demo Fault Injector (100% Working, Interactive Suite) -->
            <div class="card">
                <div class="card-header">
                    <div class="card-title">
                        <svg class="scada-icon" viewBox="0 0 24 24"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>
                        <span>SIH Fault Injection Suite & Interlock Validation</span>
                    </div>
                    <span style="font-size:12px; font-weight:700; color:var(--gold);" id="activeFaultIndicator">ACTIVE: NONE</span>
                </div>
                <div class="card-body">
                    <div style="font-size:13px; color:var(--text-dim); margin-bottom:12px;">
                        Trigger simulated failure modes to validate automated PLC relay trips, condition synthesis, and vision alerts:
                    </div>
                    <div class="fault-strip">
                        <button class="btn-fault" id="btnFault_SPLICE_TEAR" onclick="injectFault('SPLICE_TEAR')">
                            <svg class="scada-icon" style="stroke:var(--alert-burnt);" viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
                            <span>Splice Joint Tear</span>
                        </button>
                        <button class="btn-fault" id="btnFault_BEARING_HOTSPOT" onclick="injectFault('BEARING_HOTSPOT')">
                            <svg class="scada-icon" style="stroke:var(--warn-amber);" viewBox="0 0 24 24"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/></svg>
                            <span>Bearing Hotspot</span>
                        </button>
                        <button class="btn-fault" id="btnFault_TENSION_SURGE" onclick="injectFault('TENSION_SURGE')">
                            <svg class="scada-icon" style="stroke:var(--gold);" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
                            <span>Tension Surge</span>
                        </button>
                        <button class="btn-fault" id="btnFault_MISALIGNMENT" onclick="injectFault('MISALIGNMENT')">
                            <svg class="scada-icon" style="stroke:var(--brown);" viewBox="0 0 24 24"><polyline points="17 11 21 7 17 3"/><line x1="21" y1="7" x2="9" y2="7"/><polyline points="7 21 3 17 7 13"/><line x1="3" y1="17" x2="15" y2="17"/></svg>
                            <span>Lateral Sway</span>
                        </button>
                        <button class="btn-fault" id="btnFault_SURFACE_GOUGE" onclick="injectFault('SURFACE_GOUGE')">
                            <svg class="scada-icon" style="stroke:var(--warn-amber);" viewBox="0 0 24 24"><path d="M2 12h20M7 8l5 8M17 8l-5 8"/></svg>
                            <span>Surface Gouge</span>
                        </button>
                        <button class="btn-fault" id="btnFault_ORE_DUST" onclick="injectFault('ORE_DUST')">
                            <svg class="scada-icon" style="stroke:var(--brown-hover);" viewBox="0 0 24 24"><path d="M3 17l6-6 4 4 8-8"/><polyline points="17 7 21 7 21 11"/></svg>
                            <span>Ore Dust Layer</span>
                        </button>
                        <button class="btn-fault btn-reset-full" onclick="resetSystem()">
                            <svg class="scada-icon" style="stroke:currentColor;" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><polyline points="9 12 11 14 15 10"/></svg>
                            <span>Reset System & Restore Motor Contactor</span>
                        </button>
                    </div>
                </div>
            </div>

            <!-- Multi-Modal Telemetry Chart -->
            <div class="card">
                <div class="card-header">
                    <div class="card-title">
                        <svg class="scada-icon" viewBox="0 0 24 24"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>
                        <span>IoT Sensor Streams: HX711 · DS18B20 · MPU6050</span>
                    </div>
                    <span style="font-size:12px; color:var(--text-dim);">Live Telemetry Trends</span>
                </div>
                <div class="card-body">
                    <div style="position:relative; width:100%; height:210px;">
                        <canvas id="teleChart"></canvas>
                    </div>
                </div>
            </div>

            <!-- SCADA Command Center & Safety Event Logs -->
            <div class="card">
                <div class="card-header">
                    <div class="card-title">
                        <svg class="scada-icon" viewBox="0 0 24 24"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1" ry="1"/></svg>
                        <span>Industrial Event Audit Log & Modbus Feeds</span>
                    </div>
                    <button class="btn-ctrl" onclick="toggleDev()" id="btnHwChannel" style="font-size:11.5px; padding:4px 10px;">
                        Hardware: Sim
                    </button>
                </div>
                <div class="card-body" style="padding:18px;">
                    <div class="feed-bar">
                        <span id="modbusText">Modbus TCP: Port 502 · PLC Node 10.0.4.12</span>
                        <span id="opcuaText">OPC-UA: opc.tcp://10.0.4.15:4840</span>
                    </div>
                    <div style="max-height:190px; overflow-y:auto; border:1px solid var(--border); border-radius:10px;">
                        <table class="inc-table">
                            <thead>
                                <tr>
                                    <th>Timestamp</th>
                                    <th>Event Description</th>
                                    <th>Severity</th>
                                </tr>
                            </thead>
                            <tbody id="incTbody">
                                <tr><td colspan="3" style="text-align:center; color:var(--text-dim);">Loading audit events...</td></tr>
                            </tbody>
                        </table>
                    </div>
                </div>
            </div>
        </section>
    </main>

    <!-- Camera Settings Modal -->
    <div id="settingsModal" style="position:fixed; inset:0; background:rgba(0,0,0,0.70); z-index:1000; display:none; align-items:center; justify-content:center;">
        <div class="card" style="max-width:500px; width:100%; padding:28px; box-shadow:var(--shadow-skate);">
            <h3 style="font-size:16px; font-weight:800; margin-bottom:18px; display:flex; align-items:center; gap:10px; color:var(--gold);">
                <svg class="scada-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
                <span>Zone 3 Vision AI Configuration</span>
            </h3>
            <div style="margin-bottom:16px;">
                <label style="display:block; font-size:11.5px; font-weight:700; color:var(--text-dim); text-transform:uppercase; margin-bottom:8px;">Camera Capture Source</label>
                <select id="selSource" onchange="sourceChange()" style="width:100%; padding:12px; background:var(--bg-input); border:1px solid var(--border); border-radius:10px; color:var(--text); outline:none;">
                    <option value="webcam">Local USB / Integrated Webcam (Index 0)</option>
                    <option value="esp32">ESP32-CAM (EP-32 Module MJPEG Stream)</option>
                    <option value="sim">Zone 3 Conveyor Digital Twin (Simulation)</option>
                </select>
            </div>
            <div id="esp32Grp" style="margin-bottom:16px;">
                <label style="display:block; font-size:11.5px; font-weight:700; color:var(--text-dim); text-transform:uppercase; margin-bottom:8px;">ESP32-CAM Stream URL</label>
                <input type="text" id="inpUrl" placeholder="http://192.168.4.1:81/stream" style="width:100%; padding:12px; background:var(--bg-input); border:1px solid var(--border); border-radius:10px; color:var(--text); outline:none;">
                <div style="font-size:11.5px; color:var(--text-dim); margin-top:6px;">Default AP: <code>http://192.168.4.1:81/stream</code>. Or enter local IP.</div>
            </div>
            <div style="display:flex; justify-content:flex-end; gap:12px; margin-top:18px;">
                <button class="btn-ctrl" onclick="closeSettingsModal()">Cancel</button>
                <button class="btn-ctrl btn-copper" onclick="applySettings()">Save & Connect</button>
            </div>
        </div>
    </div>

    <!-- Jury Briefing & SIH 26008 Defense Modal -->
    <div id="juryModal" style="position:fixed; inset:0; background:rgba(0,0,0,0.80); z-index:1001; display:none; align-items:center; justify-content:center;">
        <div class="card" style="max-width:760px; width:100%; max-height:85vh; overflow-y:auto; padding:32px; box-shadow:var(--shadow-skate);">
            <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--border); padding-bottom:14px; margin-bottom:18px;">
                <div>
                    <h3 style="font-size:19px; font-weight:800; color:var(--gold);">SIH 26008 · Jury Architecture & Defense Briefing</h3>
                    <p style="font-size:12.5px; color:var(--text-dim); margin-top:4px;">Team AYASK · NMDC Conveyor Joint Rupture & Predictive Health System</p>
                </div>
                <button class="btn-ctrl" onclick="closeJuryModal()">Close</button>
            </div>

            <div style="display:flex; flex-direction:column; gap:18px; font-size:13.5px; line-height:1.7; color:var(--text);">
                <div style="background:var(--bg-card); border:1px solid var(--border); border-radius:10px; padding:16px;">
                    <strong style="color:var(--gold); font-size:13px; text-transform:uppercase;">1. Problem Statement 26008 Scope</strong>
                    <p style="color:var(--text-dim); margin-top:6px;">
                        Steel cord conveyor belts in iron ore mines span up to 5km and cost ₹1.5 Crores/km. A single splice joint separation or longitudinal rip causes complete plant downtime, multi-million rupee losses, and serious safety hazards. AYASK provides edge-computed multi-modal predictive interlocks.
                    </p>
                </div>

                <div style="background:var(--bg-card); border:1px solid var(--border); border-radius:10px; padding:16px;">
                    <strong style="color:var(--gold); font-size:13px; text-transform:uppercase;">2. Multi-Modal Fusion Formulation</strong>
                    <p style="color:var(--text-dim); margin-top:6px;">
                        Composite Belt Health Index (BHI) fuses Return-Belt YOLOv8 defect confidence with dynamic physical sensor metrics:<br>
                        <code>BHI = 100 - [ 0.40·P_vision + 0.25·P_tension + 0.20·P_vib + 0.15·P_temp ]</code><br>
                        Joint RUL is synthesized via non-linear degradation estimation: <code>RUL(t) = RUL_0 · exp(-λ·Vib_RMS - μ·Drift)</code>.
                    </p>
                </div>

                <div style="background:var(--bg-card); border:1px solid var(--border); border-radius:10px; padding:16px;">
                    <strong style="color:var(--gold); font-size:13px; text-transform:uppercase;">3. Failsafe Drive Contactor Interlock (NC Loop)</strong>
                    <p style="color:var(--text-dim); margin-top:6px;">
                        Wired into the Schneider TeSys D65 drive contactor coil in a Normally Closed (NC) loop. When critical splice rupture or severe sway is flagged, the relay circuit de-energizes in &lt;120ms (ISO 13849-1 Cat 4 / PL e compliant), cutting power before the belt splits.
                    </p>
                </div>

                <div style="background:var(--bg-card); border:1px solid var(--border); border-radius:10px; padding:16px;">
                    <strong style="color:var(--gold); font-size:13px; text-transform:uppercase;">4. Hardware Node Mapping</strong>
                    <p style="color:var(--text-dim); margin-top:6px;">
                        • <strong>MPU6050 (I2C 0x68)</strong>: Tri-axial acceleration & RMS vibration harmonics.<br>
                        • <strong>DS18B20 (1-Wire GPIO 4)</strong>: Pulley bearing friction heat tracking.<br>
                        • <strong>HX711 (Pins 5/6)</strong>: 24-bit strain gauge dynamic tension measurement.<br>
                        • <strong>ESP32-CAM (HTTP MJPEG)</strong>: Zone 3 return belt high-speed optical scanning.
                    </p>
                </div>
            </div>
        </div>
    </div>

    <script>
        const token = localStorage.getItem('nmdc_token');
        if (!token) window.location.href = '/';
        function signOut() {
            fetch('/api/signout', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ token: token || '' })
            }).finally(() => {
                localStorage.removeItem('nmdc_token');
                window.location.href = '/';
            });
        }

        // ===================== TOAST NOTIFICATION =====================
        let toastTimer;
        function showToast(msg) {
            const toast = document.getElementById('toastAlert');
            document.getElementById('toastMsg').innerText = msg;
            toast.classList.add('show');
            clearTimeout(toastTimer);
            toastTimer = setTimeout(() => toast.classList.remove('show'), 3200);
        }

        // ===================== CHART.JS INITIALIZATION =====================
        const cCtx = document.getElementById('teleChart').getContext('2d');
        const bLen = 40;
        const teleChart = new Chart(cCtx, {
            type: 'line',
            data: {
                labels: Array.from({length: bLen}, (_, i) => `${bLen - i}s`),
                datasets: [
                    { label: 'Tension (kN)', data: Array(bLen).fill(45), borderColor: '#d4af37', borderWidth: 2, pointRadius: 0, tension: 0.3 },
                    { label: 'Bearing Temp (°C)', data: Array(bLen).fill(52.4), borderColor: '#a86134', borderWidth: 1.8, pointRadius: 0, tension: 0.3 },
                    { label: 'Vibration RMS (mm/s)', data: Array(bLen).fill(2.1), borderColor: '#e6cc85', borderWidth: 1.8, pointRadius: 0, tension: 0.3 }
                ]
            },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                scales: {
                    x: { display: false },
                    y: {
                        grid: { color: 'rgba(212,175,55,0.08)' },
                        ticks: { color: '#c4b5a0', font: { size: 11, family: 'Inter' } }
                    }
                },
                plugins: { legend: { labels: { color: '#f5eedf', font: { size: 11.5, family: 'Inter' }, boxWidth: 14 } } }
            }
        });

        function updateChartTheme() {
            if (!teleChart || !teleChart.options) return;
            const isDark = (document.documentElement.getAttribute('data-theme') || 'dark') === 'dark';
            if (isDark) {
                // Strict Gold & Brown palette in Dark Mode
                teleChart.data.datasets[0].borderColor = '#d4af37'; // Gold
                teleChart.data.datasets[1].borderColor = '#a86134'; // Umber Brown
                teleChart.data.datasets[2].borderColor = '#e6cc85'; // Golden Sand
                teleChart.options.scales.y.grid.color = 'rgba(212,175,55,0.08)';
                teleChart.options.scales.y.ticks.color = '#c4b5a0';
                teleChart.options.plugins.legend.labels.color = '#f5eedf';
            } else {
                // Perfect Light Mode
                teleChart.data.datasets[0].borderColor = '#8c5d33';
                teleChart.data.datasets[1].borderColor = '#b91c1c';
                teleChart.data.datasets[2].borderColor = '#15803d';
                teleChart.options.scales.y.grid.color = 'rgba(15,23,42,0.12)';
                teleChart.options.scales.y.ticks.color = '#334155';
                teleChart.options.plugins.legend.labels.color = '#0f172a';
            }
            teleChart.update('none');
        }

        // ===================== XYZ RADAR CANVAS =====================
        const xyzCanvas = document.getElementById('xyzCanvas');
        const xyzCtx = xyzCanvas.getContext('2d');
        const trail = [];
        function drawXyz(x, y, z) {
            if (!xyzCtx) return;
            const w = xyzCanvas.width, h = xyzCanvas.height;
            const isDark = (document.documentElement.getAttribute('data-theme') || 'dark') === 'dark';
            xyzCtx.clearRect(0, 0, w, h);
            const cx = w / 2, cy = h / 2;

            xyzCtx.lineWidth = 1;
            const ringCol = isDark ? 'rgba(212,175,55,0.10)' : 'rgba(15,23,42,0.14)';
            const axisCol = isDark ? 'rgba(212,175,55,0.22)' : 'rgba(15,23,42,0.28)';

            for (let r = 30; r <= 110; r += 28) {
                xyzCtx.beginPath(); xyzCtx.strokeStyle = ringCol; xyzCtx.arc(cx, cy, r, 0, Math.PI * 2); xyzCtx.stroke();
            }
            xyzCtx.beginPath(); xyzCtx.strokeStyle = axisCol;
            xyzCtx.moveTo(cx, 12); xyzCtx.lineTo(cx, h - 12);
            xyzCtx.moveTo(24, cy); xyzCtx.lineTo(w - 24, cy);
            xyzCtx.stroke();

            const px = cx + (x || 0) * 26.0;
            const py = cy - (y || 0) * 26.0;
            trail.push({x: px, y: py});
            if (trail.length > 20) trail.shift();

            for (let i = 0; i < trail.length; i++) {
                const alpha = (i + 1) / trail.length * 0.45;
                xyzCtx.beginPath();
                xyzCtx.fillStyle = isDark ? `rgba(212, 175, 55, ${alpha})` : `rgba(140, 93, 51, ${alpha})`;
                xyzCtx.arc(trail[i].x, trail[i].y, 3, 0, Math.PI * 2); xyzCtx.fill();
            }

            const zNorm = Math.abs(z - 9.81);
            const radius = Math.max(6, Math.min(15, 7 + zNorm * 2.5));
            xyzCtx.beginPath();
            xyzCtx.strokeStyle = isDark ? 'rgba(212, 175, 55, 0.45)' : 'rgba(140, 93, 51, 0.4)';
            xyzCtx.lineWidth = 1.5;
            xyzCtx.arc(px, py, radius + 5 + zNorm * 3, 0, Math.PI * 2); xyzCtx.stroke();

            xyzCtx.beginPath();
            xyzCtx.fillStyle = isDark ? '#d4af37' : '#8c5d33';
            xyzCtx.arc(px, py, radius, 0, Math.PI * 2); xyzCtx.fill();

            document.getElementById('valX').innerText = `${(x >= 0 ? '+' : '') + x.toFixed(2)} m/s²`;
            document.getElementById('valY').innerText = `${(y >= 0 ? '+' : '') + y.toFixed(2)} m/s²`;
            document.getElementById('valZ').innerText = `${(z >= 0 ? '+' : '') + z.toFixed(2)} m/s²`;
        }

        // ===================== DIGITAL TWIN CANVAS SIMULATION =====================
        const dtCanvas = document.getElementById('digitalTwinCanvas');
        const dtCtx = dtCanvas.getContext('2d');
        let twinSpliceAngle = 0;

        function renderDigitalTwin(posPct, speed, isRunning, fault) {
            if (!dtCtx) return;
            const w = dtCanvas.width, h = dtCanvas.height;
            const isDark = (document.documentElement.getAttribute('data-theme') || 'dark') === 'dark';
            dtCtx.clearRect(0, 0, w, h);

            const leftX = 65, rightX = w - 65, centerY = h / 2, r = 38;

            // Pulleys in Brown/Gold
            const pulleyFill = isDark ? '#261e17' : '#e2e8f0';
            const pulleyStroke = isDark ? '#6e543e' : '#94a3b8';
            dtCtx.lineWidth = 2.5;

            // Tail Pulley (Left)
            dtCtx.beginPath(); dtCtx.fillStyle = pulleyFill; dtCtx.strokeStyle = pulleyStroke;
            dtCtx.arc(leftX, centerY, r, 0, Math.PI * 2); dtCtx.fill(); dtCtx.stroke();

            // Head Drive Pulley (Right)
            dtCtx.beginPath();
            dtCtx.fillStyle = isRunning ? (isDark ? '#33271d' : '#cbd5e1') : (isDark ? '#3d2016' : '#fee2e2');
            dtCtx.strokeStyle = isRunning ? (isDark ? '#d4af37' : 'var(--gold)') : (isDark ? '#a84825' : '#b91c1c');
            dtCtx.arc(rightX, centerY, r, 0, Math.PI * 2); dtCtx.fill(); dtCtx.stroke();

            // Spinning spokes
            if (isRunning) twinSpliceAngle += (speed / 3.5) * 0.08;
            for (let a = 0; a < Math.PI * 2; a += Math.PI / 2) {
                dtCtx.beginPath(); dtCtx.strokeStyle = isDark ? '#7a5e45' : '#64748b'; dtCtx.lineWidth = 1.5;
                dtCtx.moveTo(rightX, centerY);
                dtCtx.lineTo(rightX + Math.cos(twinSpliceAngle + a) * (r - 2), centerY + Math.sin(twinSpliceAngle + a) * (r - 2));
                dtCtx.stroke();
            }

            // Continuous Rubber Belt
            const beltCol = isDark ? '#221b15' : '#334155';
            dtCtx.lineWidth = 7; dtCtx.strokeStyle = beltCol;
            dtCtx.beginPath(); dtCtx.moveTo(leftX, centerY - r); dtCtx.lineTo(rightX, centerY - r); dtCtx.stroke();
            dtCtx.beginPath(); dtCtx.moveTo(rightX, centerY + r); dtCtx.lineTo(leftX, centerY + r); dtCtx.stroke();
            dtCtx.beginPath(); dtCtx.arc(leftX, centerY, r, Math.PI / 2, Math.PI * 1.5); dtCtx.stroke();
            dtCtx.beginPath(); dtCtx.arc(rightX, centerY, r, Math.PI * 1.5, Math.PI / 2); dtCtx.stroke();

            // Iron Ore Lumps on Carrying Run (Brown)
            if (isRunning && speed > 0.1) {
                const oreOffset = (Date.now() / 28 * (speed / 3.5)) % 44;
                for (let ox = leftX + 15 + oreOffset; ox < rightX - 15; ox += 44) {
                    dtCtx.beginPath(); dtCtx.fillStyle = isDark ? '#6e4726' : '#8c5d33';
                    dtCtx.arc(ox, centerY - r - 5, 4, 0, Math.PI * 2); dtCtx.fill();
                }
            }

            // Zone 3 Optical AI Scanner Beam (Golden Yellow)
            const scanX = w / 2;
            dtCtx.beginPath();
            const beamGrad = dtCtx.createLinearGradient(scanX, centerY + r - 16, scanX, centerY + r + 16);
            beamGrad.addColorStop(0, 'rgba(212, 175, 55, 0)');
            beamGrad.addColorStop(0.5, isDark ? 'rgba(212, 175, 55, 0.45)' : 'rgba(140, 93, 51, 0.40)');
            beamGrad.addColorStop(1, 'rgba(212, 175, 55, 0)');
            dtCtx.fillStyle = beamGrad;
            dtCtx.fillRect(scanX - 18, centerY + r - 12, 36, 24);

            // Scanner sensor head
            dtCtx.fillStyle = isDark ? '#d4af37' : '#8c5d33';
            dtCtx.fillRect(scanX - 8, centerY + r + 12, 16, 7);

            // Travelling Splice Joint
            let sx, sy;
            const topLen = rightX - leftX;
            if (posPct < 45) {
                const f = posPct / 45;
                sx = leftX + f * topLen;
                sy = centerY - r;
            } else if (posPct < 55) {
                const f = (posPct - 45) / 10;
                const a = -Math.PI / 2 + f * Math.PI;
                sx = rightX + Math.cos(a) * r;
                sy = centerY + Math.sin(a) * r;
            } else if (posPct < 90) {
                const f = (posPct - 55) / 35;
                sx = rightX - f * topLen;
                sy = centerY + r;
            } else {
                const f = (posPct - 90) / 10;
                const a = Math.PI / 2 + f * Math.PI;
                sx = leftX + Math.cos(a) * r;
                sy = centerY + Math.sin(a) * r;
            }

            // Draw Splice Joint in Gold (or Burnt Red/Brown if torn)
            dtCtx.beginPath();
            const isTear = fault === 'SPLICE_TEAR';
            dtCtx.fillStyle = isTear ? (isDark ? '#a84825' : '#b91c1c') : (isDark ? '#d4af37' : '#15803d');
            dtCtx.arc(sx, sy, isTear ? 7 : 5, 0, Math.PI * 2);
            dtCtx.fill();
            if (isTear) {
                dtCtx.strokeStyle = isDark ? '#d4af37' : '#ef4444'; dtCtx.lineWidth = 2;
                dtCtx.stroke();
            }

            // Descriptive Labels
            dtCtx.fillStyle = isDark ? '#c4b5a0' : '#64748b';
            dtCtx.font = '11px Inter';
            dtCtx.fillText('Zone 4: Tail & Take-Up', 20, 22);
            dtCtx.fillText('Zone 2: Carrying Run & Ore', w / 2 - 60, 22);
            dtCtx.fillText('Zone 1: Drive Pulley', w - 145, 22);
            dtCtx.fillText('Zone 3: Return Optical Scanner', w / 2 - 70, h - 10);
        }

        // ===================== THEME TOGGLE =====================
        function applyTheme(t) {
            document.documentElement.setAttribute('data-theme', t);
            localStorage.setItem('nmdc_theme', t);
            const isDark = t === 'dark';
            document.getElementById('themeLabel').innerText = isDark ? 'Light Mode' : 'Dark Mode';
            updateChartTheme();
            drawXyz(0, 0, 9.81);
            renderDigitalTwin(0, 3.5, true, 'NONE');
        }
        function toggleTheme() {
            const cur = document.documentElement.getAttribute('data-theme') || 'dark';
            applyTheme(cur === 'dark' ? 'light' : 'dark');
        }

        // ===================== FAULT BUTTON HIGHLIGHTING =====================
        function updateFaultButtonsUI(active) {
            document.querySelectorAll('.fault-strip .btn-fault').forEach(b => {
                b.classList.remove('active-fault');
            });
            const indicator = document.getElementById('activeFaultIndicator');
            if (active && active !== 'NONE') {
                const btn = document.getElementById('btnFault_' + active);
                if (btn) btn.classList.add('active-fault');
                indicator.innerText = 'ACTIVE: ' + active.replace('_', ' ');
                indicator.style.color = 'var(--gold)';
            } else {
                indicator.innerText = 'ACTIVE: NONE';
                indicator.style.color = 'var(--gold)';
            }
        }

        // ===================== WEBSOCKET =====================
        let ws;
        function connectWs() {
            const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            ws = new WebSocket(`${proto}//${window.location.host}/ws`);

            ws.onmessage = function(e) {
                const d = JSON.parse(e.data);

                // Header
                document.getElementById('headerBhi').innerText = `${d.bhi.toFixed(1)}%`;
                document.getElementById('headerRul').innerText = `${d.joint_rul_hours.toFixed(0)} hrs`;
                const hStat = document.getElementById('headerStatus');
                const hDot = document.getElementById('headerPulse');
                hStat.innerText = d.status;
                hDot.className = 'pulse-dot' + (d.status_level === 3 ? ' danger' : (d.status_level === 2 ? ' warning' : ''));

                // KPIs
                document.getElementById('kpiRul').innerText = `${d.joint_rul_hours.toFixed(0)} hrs`;
                document.getElementById('kpiRul').style.color = d.joint_rul_hours < 48 ? 'var(--alert-burnt)' : 'var(--safe-gold)';
                document.getElementById('kpiSpeed').innerText = `${d.speed_mps.toFixed(1)} m/s`;
                document.getElementById('kpiLoad').innerText = `${d.throughput_tph.toFixed(0)} TPH`;
                document.getElementById('kpiVib').innerText = `${d.vibration.toFixed(2)} mm/s`;

                // Relay NC Status
                const rText = document.getElementById('relayText');
                const rDot = document.getElementById('relayDot');
                const rBadgeText = document.getElementById('relayBadgeText');
                if (d.relay_nc_energized && !d.emergency_stop) {
                    rText.innerText = 'NC RELAY CLOSED · CONTACTOR ENERGIZED (415V)';
                    rText.style.color = 'var(--safe-gold)';
                    rDot.className = 'pulse-dot';
                    rBadgeText.innerText = 'MOTOR RUNNING';
                    document.getElementById('btnMotorLabel').innerText = 'Motor Stop';
                } else {
                    rText.innerText = 'NC RELAY OPEN · E-STOP INTERLOCK TRIPPED';
                    rText.style.color = 'var(--alert-burnt)';
                    rDot.className = 'pulse-dot danger';
                    rBadgeText.innerText = 'MOTOR STOPPED';
                    document.getElementById('btnMotorLabel').innerText = 'Motor Start';
                }

                // Digital Twin Canvas Update
                renderDigitalTwin(d.belt_pos_pct, d.speed_mps, !d.emergency_stop && d.speed_mps > 0.1, d.active_fault);
                document.getElementById('twinCycleReadout').innerText = `Splice #142 @ ${d.belt_pos_pct.toFixed(1)}% · ${d.motor_rpm.toFixed(0)} RPM`;

                // Digital Twin Zones
                if (d.zones) {
                    document.getElementById('z1Status').innerText = d.zones.zone1.status;
                    document.getElementById('z1Status').style.color = d.zones.zone1.level === 3 ? 'var(--alert-burnt)' : 'var(--safe-gold)';
                    document.getElementById('z2Status').innerText = d.zones.zone2.status;
                    document.getElementById('z2Status').style.color = d.zones.zone2.level === 3 ? 'var(--alert-burnt)' : 'var(--safe-gold)';
                    document.getElementById('z3Status').innerText = d.zones.zone3.status;
                    document.getElementById('z3Status').style.color = d.zones.zone3.level === 3 ? 'var(--alert-burnt)' : (d.zones.zone3.level === 2 ? 'var(--warn-amber)' : 'var(--safe-gold)');
                    document.getElementById('z4Status').innerText = d.zones.zone4.status;
                    document.getElementById('z4Status').style.color = d.zones.zone4.level === 3 ? 'var(--alert-burnt)' : 'var(--safe-gold)';
                }

                // Active Fault highlight
                updateFaultButtonsUI(d.active_fault);

                // Chart & XYZ
                teleChart.data.datasets[0].data.push(d.tension); teleChart.data.datasets[0].data.shift();
                teleChart.data.datasets[1].data.push(d.temperature); teleChart.data.datasets[1].data.shift();
                teleChart.data.datasets[2].data.push(d.vibration); teleChart.data.datasets[2].data.shift();
                teleChart.update('none');
                drawXyz(d.vib_x, d.vib_y, d.vib_z);

                // Video HUD
                document.getElementById('fpsReadout').innerText = `${d.live_fps.toFixed(1)} FPS`;
                document.getElementById('camStatus').innerText = `Status: ${d.camera_status}`;
                document.getElementById('camSourcePill').innerText = d.camera_source.toUpperCase();

                // Detections
                const detEl = document.getElementById('detList');
                if (d.detected_objects && d.detected_objects.length > 0) {
                    detEl.innerHTML = d.detected_objects.map(o => `<span class="det-pill">${o.class} (${o.conf}%)</span>`).join(' ');
                } else {
                    detEl.innerHTML = `<span style="color:var(--text-faint); font-style:italic;">Scanning nominal return belt surface...</span>`;
                }

                // Incidents
                if (d.incidents) {
                    const tb = document.getElementById('incTbody');
                    tb.innerHTML = d.incidents.map(i => {
                        let c = 'var(--text-dim)';
                        if (i.severity === 'CRITICAL') c = 'var(--alert-burnt)';
                        else if (i.severity === 'WARNING') c = 'var(--warn-amber)';
                        else if (i.severity === 'INFO') c = 'var(--safe-gold)';
                        return `<tr><td>${i.timestamp}</td><td>${i.event}</td><td style="color:${c}; font-weight:700;">${i.severity}</td></tr>`;
                    }).join('');
                }

                document.getElementById('btnHwChannel').innerText = `Hardware: ${d.device_connected ? 'LIVE MPU6050' : 'SIMULATION'}`;
            };
            ws.onclose = () => setTimeout(connectWs, 2000);
        }

        // ===================== ACTIONS =====================
        function injectFault(type) {
            updateFaultButtonsUI(type);
            showToast('Fault Injected: ' + type.replace('_', ' ') + ' — Triggering PLC Contactor Trip');
            fetch('/api/inject_fault', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ fault_type: type })
            });
        }
        function resetSystem() {
            updateFaultButtonsUI('NONE');
            showToast('Interlock Reset: Drive Contactor Energized · Baselines Restored');
            fetch('/api/reset', { method: 'POST' });
        }
        function toggleCam() { fetch('/api/toggle_camera', { method: 'POST' }); }
        function toggleMirror() { fetch('/api/toggle_mirror', { method: 'POST' }); }
        function toggleYolo() { fetch('/api/toggle_yolo', { method: 'POST' }); }
        function toggleDev() { fetch('/api/toggle_device', { method: 'POST' }); }

        // Digital Twin Stimulation
        function stimulateTwin() {
            const spd = parseFloat(document.getElementById('sliderSpeed').value);
            const load = parseFloat(document.getElementById('sliderLoad').value);
            document.getElementById('stimSpeedVal').innerText = `${spd.toFixed(1)} m/s`;
            document.getElementById('stimLoadVal').innerText = `${load.toFixed(0)} TPH`;
            fetch('/api/set_twin_params', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ speed_mps: spd, throughput_tph: load })
            });
        }
        function toggleMotorTwin() {
            const curSpd = parseFloat(document.getElementById('sliderSpeed').value);
            const newSpd = curSpd > 0.1 ? 0.0 : 3.5;
            document.getElementById('sliderSpeed').value = newSpd;
            stimulateTwin();
        }

        // Modals
        function openSettingsModal() { document.getElementById('settingsModal').style.display = 'flex'; }
        function closeSettingsModal() { document.getElementById('settingsModal').style.display = 'none'; }
        function openJuryModal() { document.getElementById('juryModal').style.display = 'flex'; }
        function closeJuryModal() { document.getElementById('juryModal').style.display = 'none'; }
        function sourceChange() {
            document.getElementById('esp32Grp').style.display = document.getElementById('selSource').value === 'esp32' ? 'block' : 'none';
        }
        function applySettings() {
            const s = document.getElementById('selSource').value;
            const u = document.getElementById('inpUrl').value.trim();
            fetch('/api/set_camera_config', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ source: s, esp32_url: u })
            }).then(() => closeSettingsModal());
        }

        // INITIALIZE ALL
        applyTheme(localStorage.getItem('nmdc_theme') || 'dark');
        connectWs();
    </script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def landing():
    return HTMLResponse(content=LANDING_HTML)

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(content=DASHBOARD_HTML)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    # In cloud environments (Render, Railway, Heroku), bind to 0.0.0.0
    # Locally on Windows, bind to 127.0.0.1 so Uvicorn shows clickable http://127.0.0.1:8000 (0.0.0.0 throws ERR_ADDRESS_INVALID in Windows Chrome)
    is_cloud = "PORT" in os.environ
    host = "0.0.0.0" if is_cloud else "127.0.0.1"

    print("\n" + "=" * 68)
    print("  AYASK SCADA Server Started Successfully!")
    print(f"  Open in your browser: http://localhost:{port} or http://127.0.0.1:{port}")
    print("  (Note: Do NOT type 0.0.0.0 in Windows Chrome)")
    print("=" * 68 + "\n")

    uvicorn.run(app, host=host, port=port)
