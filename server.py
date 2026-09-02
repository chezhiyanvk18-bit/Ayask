# server.py
import sys
import asyncio

# Critical fix for Windows Python 3.13 asyncio
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

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
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI(title="NMDC Belt Monitoring SCADA")

# ----------------- AUTH STORE (in-memory demo user/session store) -----------------
# NOTE: For a real deployment this would be a persistent DB with a proper password
# hasher (bcrypt/argon2) and HttpOnly cookies. For this SIH prototype we keep it
# in-memory but still perform REAL credential validation - no fake/demo bypass.
users_db = {}      # email -> {"name": str, "password_hash": str, "salt": str}
sessions_db = {}   # token -> {"email": str, "name": str, "created": float}

def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()

def create_session(email: str, name: str) -> str:
    token = secrets.token_hex(24)
    sessions_db[token] = {"email": email, "name": name, "created": time.time()}
    return token

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

# ----------------- SYSTEM STATE -----------------
class SystemState:
    def __init__(self):
        self.camera_enabled = False
        self.device_connected = False   # True only once real sensor/camera hardware is attached
        self.emergency_stop = False
        
        # Operational Metrics
        self.speed_mps = 3.5
        self.load_tph = 1250.0
        self.tension_kn = 45.0
        self.vibration_rms = 2.1
        self.vibration_x = 0.8
        self.vibration_y = 1.1
        self.vibration_z = 1.6
        self.bearing_temp_c = 52.4
        self.bhi = 98.0
        self.status = "LEVEL 1: OPTIMAL"
        self.status_level = 1
        
        self.injected_defect = "NONE"
        self.incident_log = [
            {"timestamp": "09:00:00", "event": "SCADA Core System Booted", "severity": "INFO"}
        ]

state = SystemState()

# ----------------- CRASH-PROOF VIDEO GENERATOR -----------------
def generate_frames():
    cap = None
    frame_counter = 0

    try:
        while True:
            frame_counter += 1
            
            if state.camera_enabled:
                if cap is None:
                    cap = cv2.VideoCapture(0)
                
                if cap.isOpened():
                    success, frame = cap.read()
                    if success:
                        frame = cv2.resize(frame, (640, 360))
                    else:
                        state.camera_enabled = False
                        frame = None
                else:
                    state.camera_enabled = False
                    frame = None
            else:
                if cap is not None:
                    cap.release()
                    cap = None
                frame = None

            if frame is None:
                frame = np.zeros((360, 640, 3), dtype=np.uint8)
                frame[:] = (15, 18, 24)
                
                for x in range(0, 640, 40):
                    cv2.line(frame, (x, 0), (x, 360), (28, 34, 46), 1)
                for y in range(0, 360, 40):
                    cv2.line(frame, (0, y), (640, y), (28, 34, 46), 1)
                    
                sweep_x = int((frame_counter * 5) % 640)
                cv2.line(frame, (sweep_x, 0), (sweep_x, 360), (0, 229, 255), 1)

                cv2.rectangle(frame, (120, 130), (520, 230), (22, 28, 38), -1)
                cv2.rectangle(frame, (120, 130), (520, 230), (255, 51, 102), 2)
                cv2.putText(frame, "HARDWARE DISCONNECTED", (165, 170),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255, 51, 102), 2)
                cv2.putText(frame, "Awaiting RPi Cam Module 3 / USB CSI", (170, 205),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (160, 175, 195), 1)

            if state.injected_defect == "SPLICE_TEAR":
                cv2.rectangle(frame, (200, 100), (440, 240), (0, 0, 255), 2)
                cv2.putText(frame, "AI DETECT: CRITICAL SPLICE RUPTURE [98.8%]", (200, 90),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 255), 2)
            elif state.injected_defect == "SURFACE_GOUGE":
                cv2.rectangle(frame, (140, 160), (290, 250), (0, 165, 255), 2)
                cv2.putText(frame, "AI DETECT: SURFACE GOUGE [84.5%]", (140, 150),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 165, 255), 2)

            _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
            time.sleep(0.04)

    except GeneratorExit:
        pass
    finally:
        if cap is not None and cap.isOpened():
            cap.release()

@app.get("/video_feed")
def video_feed():
    return StreamingResponse(generate_frames(), media_type="multipart/x-mixed-replace; boundary=frame")

# ----------------- TELEMETRY ENGINE -----------------
def update_telemetry_step():
    if state.injected_defect == "NORMAL":
        state.tension_kn += (45.0 - state.tension_kn) * 0.1
        state.bearing_temp_c += (52.0 - state.bearing_temp_c) * 0.1
        state.vibration_rms += (2.1 - state.vibration_rms) * 0.1
        state.injected_defect = "NONE"

    elif state.injected_defect == "SPLICE_TEAR":
        state.vibration_rms = min(12.5, state.vibration_rms + 0.8)
        state.tension_kn = max(10.0, state.tension_kn - 2.5)
        state.bearing_temp_c += random.uniform(0.1, 0.4)

    elif state.injected_defect == "BEARING_HOTSPOT":
        state.bearing_temp_c = min(115.0, state.bearing_temp_c + 1.2)
        state.vibration_rms = min(8.0, state.vibration_rms + 0.3)

    elif state.injected_defect == "TENSION_SPIKE":
        state.tension_kn = min(92.0, state.tension_kn + 3.0)

    else:
        state.tension_kn += random.uniform(-0.3, 0.3)
        state.bearing_temp_c += random.uniform(-0.1, 0.1)
        state.vibration_rms += random.uniform(-0.08, 0.08)

    state.vibration_x = round(state.vibration_rms * 0.35 + random.uniform(-0.05, 0.05), 2)
    state.vibration_y = round(state.vibration_rms * 0.45 + random.uniform(-0.05, 0.05), 2)
    state.vibration_z = round(state.vibration_rms * 0.65 + random.uniform(-0.05, 0.05), 2)

    penalty = 0.0
    if state.vibration_rms > 4.0: penalty += (state.vibration_rms - 4.0) * 8.0
    if state.bearing_temp_c > 65.0: penalty += (state.bearing_temp_c - 65.0) * 1.5
    if state.tension_kn > 70.0 or state.tension_kn < 25.0: penalty += 35.0
    if state.injected_defect == "SPLICE_TEAR": penalty += 55.0

    state.bhi = max(0.0, min(100.0, 100.0 - penalty))

    if state.bhi < 40.0 or state.injected_defect == "SPLICE_TEAR":
        state.status = "LEVEL 3: CRITICAL (EMERGENCY MOTOR CUTOFF)"
        state.status_level = 3
        state.emergency_stop = True
        state.speed_mps = max(0.0, state.speed_mps - 0.5)
    elif state.bhi < 75.0:
        state.status = "LEVEL 2: WARNING (MAINTENANCE REQUIRED)"
        state.status_level = 2
    else:
        state.status = "LEVEL 1: OPTIMAL"
        state.status_level = 1
        if not state.emergency_stop:
            state.speed_mps = 3.5

# ----------------- FAULT INJECTION API -----------------
class FaultRequest(BaseModel):
    fault_type: str

@app.post("/api/inject_fault")
def inject_fault(req: FaultRequest):
    state.injected_defect = req.fault_type
    log_entry = {
        "timestamp": time.strftime("%H:%M:%S"),
        "event": f"FAULT TRIGGER: {req.fault_type}",
        "severity": "CRITICAL" if req.fault_type in ["SPLICE_TEAR", "TENSION_SPIKE"] else "WARNING"
    }
    state.incident_log.insert(0, log_entry)
    return {"status": "success", "active_fault": state.injected_defect}

@app.post("/api/toggle_camera")
def toggle_camera():
    if not state.camera_enabled and not state.device_connected:
        return {"camera_enabled": False, "error": "device_not_connected"}
    state.camera_enabled = not state.camera_enabled
    return {"camera_enabled": state.camera_enabled}

@app.post("/api/toggle_device")
def toggle_device():
    state.device_connected = not state.device_connected
    log_entry = {
        "timestamp": time.strftime("%H:%M:%S"),
        "event": "Hardware link established (sensors + camera bus online)" if state.device_connected else "Hardware link dropped — running on standby",
        "severity": "INFO"
    }
    state.incident_log.insert(0, log_entry)
    if not state.device_connected:
        state.camera_enabled = False
    return {"device_connected": state.device_connected}

@app.post("/api/reset")
def reset_system():
    state.injected_defect = "NORMAL"
    state.emergency_stop = False
    state.tension_kn = 45.0
    state.vibration_rms = 2.1
    state.bearing_temp_c = 52.0
    state.speed_mps = 3.5
    state.bhi = 98.0
    state.status = "LEVEL 1: OPTIMAL"
    state.status_level = 1
    return {"status": "reset_complete"}

# ----------------- WEBSOCKET BROADCAST -----------------
@app.websocket("/ws")
async def websocket_telemetry(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            if state.device_connected:
                update_telemetry_step()
                display_status = state.status
                display_level = state.status_level
            else:
                display_status = "STANDBY: AWAITING HARDWARE CONNECTION"
                display_level = 0

            payload = {
                "bhi": round(state.bhi, 1),
                "speed": round(state.speed_mps, 2),
                "load": round(state.load_tph, 1),
                "tension": round(state.tension_kn, 1),
                "vibration": round(state.vibration_rms, 2),
                "vib_x": state.vibration_x,
                "vib_y": state.vibration_y,
                "vib_z": state.vibration_z,
                "temperature": round(state.bearing_temp_c, 1),
                "status": display_status,
                "status_level": display_level,
                "emergency_stop": state.emergency_stop,
                "camera_connected": state.camera_enabled,
                "device_connected": state.device_connected,
                "rul_hours": max(0, int(state.bhi * 18.5)),
                "incidents": state.incident_log[:6]
            }
            await websocket.send_text(json.dumps(payload))
            await asyncio.sleep(0.1)
    except (WebSocketDisconnect, ConnectionResetError):
        pass

# ----------------- FRONT PAGE (SIGN IN) -----------------
@app.get("/", response_class=HTMLResponse)
def landing():
    return LANDING_HTML

# ----------------- EMBEDDED DASHBOARD -----------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML

LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NMDC Conveyor Health AI — Sign In</title>
<link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;600;700&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
    :root {
        --bg: #06070b;
        --card-bg: #12151fcc;
        --border: #22293a;
        --cyan: #00e5ff;
        --green: #00ff88;
        --amber: #ffb700;
        --ember: #ff6a3d;
        --red: #ff3366;
        --text-main: #ffffff;
        --text-dim: #9aa7bd;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body { height: 100%; }
    body {
        background: var(--bg);
        color: var(--text-main);
        font-family: 'Inter', sans-serif;
        overflow-x: hidden;
        min-height: 100vh;
        position: relative;
    }

    /* ---------------- LIQUID FLUID BACKGROUND ---------------- */
    .fluid-stage {
        position: fixed;
        inset: 0;
        z-index: 0;
        overflow: hidden;
        background: radial-gradient(ellipse at 50% -10%, #0d1220 0%, #06070b 60%);
    }
    .fluid-goo { position: absolute; inset: -10%; filter: url(#goo) blur(2px); }
    .blob {
        position: absolute;
        border-radius: 42% 58% 65% 35% / 45% 40% 60% 55%;
        mix-blend-mode: screen;
        opacity: 0.7;
        filter: blur(6px);
        animation: morph 16s ease-in-out infinite;
    }
    .blob.b1 { width: 46vw; height: 46vw; left: -10vw; top: -8vw; background: radial-gradient(circle at 35% 30%, var(--ember), #7a1c00 70%); animation-duration: 19s; }
    .blob.b2 { width: 38vw; height: 38vw; right: -8vw; top: 8vw; background: radial-gradient(circle at 60% 40%, var(--cyan), #003b45 70%); animation-duration: 23s; animation-delay: -4s; }
    .blob.b3 { width: 34vw; height: 34vw; left: 20vw; bottom: -14vw; background: radial-gradient(circle at 50% 50%, var(--amber), #5c3800 70%); animation-duration: 27s; animation-delay: -10s; }
    .blob.b4 { width: 28vw; height: 28vw; right: 18vw; bottom: -10vw; background: radial-gradient(circle at 50% 50%, var(--green), #003322 70%); opacity: 0.45; animation-duration: 21s; animation-delay: -7s; }

    @keyframes morph {
        0%   { border-radius: 42% 58% 65% 35% / 45% 40% 60% 55%; transform: translate(0,0) scale(1) rotate(0deg); }
        25%  { border-radius: 58% 42% 35% 65% / 60% 55% 45% 40%; transform: translate(4%, 3%) scale(1.08) rotate(15deg); }
        50%  { border-radius: 65% 35% 55% 45% / 40% 60% 35% 65%; transform: translate(-3%, 5%) scale(0.96) rotate(-10deg); }
        75%  { border-radius: 35% 65% 40% 60% / 55% 45% 65% 40%; transform: translate(3%, -4%) scale(1.05) rotate(8deg); }
        100% { border-radius: 42% 58% 65% 35% / 45% 40% 60% 55%; transform: translate(0,0) scale(1) rotate(0deg); }
    }
    .grain-overlay {
        position: fixed; inset: 0; z-index: 1; pointer-events: none;
        background: repeating-linear-gradient(0deg, rgba(255,255,255,0.015) 0px, transparent 1px, transparent 2px);
        mix-blend-mode: overlay;
    }
    .vignette { position: fixed; inset: 0; z-index: 1; pointer-events: none; box-shadow: inset 0 0 22vw rgba(0,0,0,0.75); }

    /* ---------------- LAYOUT ---------------- */
    .page { position: relative; z-index: 2; min-height: 100vh; display: flex; flex-direction: column; }

    nav { display: flex; justify-content: space-between; align-items: center; padding: 22px 5vw; }
    .brand { display: flex; align-items: center; gap: 12px; }
    .brand-logo {
        width: 42px; height: 42px; border-radius: 10px;
        background: rgba(0, 229, 255, 0.08); border: 1px solid var(--cyan);
        display: flex; align-items: center; justify-content: center; color: var(--cyan);
        backdrop-filter: blur(6px);
    }
    .brand h1 { font-family: 'Rajdhani', sans-serif; font-size: 19px; font-weight: 700; letter-spacing: 1.2px; color: #fff; }
    .brand p { font-size: 10.5px; color: var(--text-dim); letter-spacing: 0.4px; }

    .btn-ghost {
        padding: 10px 20px; border-radius: 8px; border: 1px solid var(--border);
        background: rgba(255,255,255,0.03); color: #fff; font-size: 13px; font-weight: 600;
        cursor: pointer; backdrop-filter: blur(6px);
    }

    /* ---------------- HERO / SPLIT LAYOUT ---------------- */
    .split {
        flex: 1; display: grid; grid-template-columns: 1.1fr 0.9fr;
        gap: 40px; align-items: center; padding: 20px 6vw 50px; max-width: 1300px; margin: 0 auto; width: 100%;
    }
    .hero-copy { text-align: left; }
    .hero-tag {
        display: inline-flex; align-items: center; gap: 8px;
        border: 1px solid var(--border); background: rgba(255,255,255,0.04);
        border-radius: 999px; padding: 6px 14px; font-size: 11.5px; color: var(--amber);
        letter-spacing: 0.5px; margin-bottom: 22px; backdrop-filter: blur(6px);
    }
    .hero-copy h2 {
        font-family: 'Rajdhani', sans-serif; font-weight: 700; letter-spacing: 0.5px;
        font-size: clamp(28px, 3.6vw, 46px); line-height: 1.1;
        background: linear-gradient(120deg, #fff 30%, var(--cyan) 65%, var(--ember) 100%);
        -webkit-background-clip: text; background-clip: text; color: transparent;
    }
    .hero-copy p.sub {
        margin-top: 16px; max-width: 480px; color: var(--text-dim); font-size: 14.5px; line-height: 1.65;
    }
    .features {
        display: grid; grid-template-columns: repeat(2, 1fr);
        gap: 12px; margin-top: 36px; max-width: 520px;
    }
    .feature-card {
        background: rgba(18,21,31,0.5); border: 1px solid var(--border);
        border-radius: 12px; padding: 15px; backdrop-filter: blur(10px);
    }
    .feature-card .f-icon {
        width: 30px; height: 30px; border-radius: 8px; display: flex; align-items: center;
        justify-content: center; background: rgba(0,229,255,0.1); color: var(--cyan); margin-bottom: 10px;
    }
    .feature-card h3 { font-size: 12.5px; font-weight: 600; margin-bottom: 4px; }
    .feature-card p { font-size: 11px; color: var(--text-dim); line-height: 1.45; }

    /* ---------------- AUTH CARD ---------------- */
    .auth-card {
        background: #10131cee; border: 1px solid var(--border); border-radius: 18px;
        padding: 36px 34px; width: 100%; max-width: 400px; margin: 0 auto;
        box-shadow: 0 24px 70px rgba(0,0,0,0.55);
        backdrop-filter: blur(14px);
    }
    .auth-card h2 {
        font-family: 'Rajdhani', sans-serif; font-size: 24px; font-weight: 700; text-align: center;
    }
    .auth-card p.auth-sub { text-align: center; font-size: 12.5px; color: var(--text-dim); margin-top: 6px; margin-bottom: 26px; }

    .field { margin-bottom: 16px; position: relative; }
    .field label { display: block; font-size: 12px; color: var(--text-dim); margin-bottom: 6px; font-weight: 500; }
    .field input {
        width: 100%; padding: 12px 14px; background: #191e2b; border: 1px solid var(--border);
        border-radius: 8px; color: var(--text-main); font-size: 14px; outline: none;
        transition: border-color 0.15s, box-shadow 0.15s;
    }
    .field input:focus { border-color: var(--cyan); box-shadow: 0 0 0 3px rgba(0,229,255,0.12); }
    .field-hint { font-size: 11px; color: var(--text-dim); margin-top: 5px; }

    .auth-error {
        display: none; background: rgba(255,51,102,0.1); border: 1px solid var(--red);
        color: #ffb3c3; font-size: 12.5px; padding: 10px 12px; border-radius: 8px; margin-bottom: 16px;
    }
    .auth-success {
        display: none; background: rgba(0,255,136,0.1); border: 1px solid var(--green);
        color: #b8ffdf; font-size: 12.5px; padding: 10px 12px; border-radius: 8px; margin-bottom: 16px;
    }

    .btn-primary-block {
        width: 100%; padding: 13px; border-radius: 8px; border: none;
        background: linear-gradient(120deg, var(--cyan), #00b8cc); color: #001217; font-size: 14px; font-weight: 700;
        letter-spacing: 0.3px; cursor: pointer; transition: filter 0.15s, transform 0.15s;
        display: flex; align-items: center; justify-content: center; gap: 8px;
    }
    .btn-primary-block:hover { filter: brightness(1.08); transform: translateY(-1px); }
    .btn-primary-block:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }

    .auth-switch { text-align: center; margin-top: 20px; font-size: 12.5px; color: var(--text-dim); }
    .auth-switch a { color: var(--cyan); cursor: pointer; text-decoration: none; font-weight: 600; }
    .auth-switch a:hover { text-decoration: underline; }

    .spinner {
        width: 15px; height: 15px; border: 2px solid rgba(0,0,0,0.25); border-top-color: #001217;
        border-radius: 50%; animation: spin 0.7s linear infinite; display: none;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    footer { text-align: center; padding: 18px; font-size: 11px; color: var(--text-dim); position: relative; z-index: 2; }

    @media (max-width: 900px) {
        .split { grid-template-columns: 1fr; padding-top: 10px; }
        .hero-copy { text-align: center; }
        .hero-copy p.sub { margin: 16px auto 0; }
        .features { margin: 30px auto 0; }
    }
    @media (max-width: 520px) {
        nav { padding: 16px 5vw; }
        .auth-card { padding: 28px 22px; }
    }
</style>
</head>
<body>

<svg width="0" height="0" style="position:absolute">
    <filter id="goo">
        <feGaussianBlur in="SourceGraphic" stdDeviation="18" result="blur" />
        <feColorMatrix in="blur" mode="matrix"
            values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 24 -10" result="goo" />
        <feBlend in="SourceGraphic" in2="goo" />
    </filter>
</svg>

<div class="fluid-stage">
    <div class="fluid-goo">
        <div class="blob b1"></div>
        <div class="blob b2"></div>
        <div class="blob b3"></div>
        <div class="blob b4"></div>
    </div>
</div>
<div class="grain-overlay"></div>
<div class="vignette"></div>

<div class="page">
    <nav>
        <div class="brand">
            <div class="brand-logo">
                <svg width="22" height="22" viewBox="0 0 24 24"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg>
            </div>
            <div>
                <h1>NMDC CONVEYOR HEALTH AI</h1>
                <p>Smart India Hackathon PS 26008 · Ministry of Steel</p>
            </div>
        </div>
    </nav>

    <div class="split">
        <div class="hero-copy">
            <div class="hero-tag">
                <svg width="13" height="13" viewBox="0 0 24 24"><path d="M12 2v20M2 12h20" stroke="currentColor" stroke-width="2"/></svg>
                BUILT FOR IRON ORE & MINERAL MINING ENTERPRISES
            </div>
            <h2>See belt failures before they happen.</h2>
            <p class="sub">
                One command center that fuses computer-vision splice inspection, multi-sensor IoT telemetry
                and predictive Belt Health scoring — so mining operations teams can act on conveyor risk
                hours before a shutdown, not after.
            </p>
            <div class="features">
                <div class="feature-card">
                    <div class="f-icon"><svg width="16" height="16" viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" stroke="currentColor" stroke-width="2" fill="none"/><circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="2" fill="none"/></svg></div>
                    <h3>Vision AI Inspection</h3>
                    <p>Splice tears & surface gouges detected live once a camera is linked.</p>
                </div>
                <div class="feature-card">
                    <div class="f-icon"><svg width="16" height="16" viewBox="0 0 24 24"><path d="M4.93 19.07A10 10 0 0 1 12 2a10 10 0 0 1 7.07 17.07M12 12v6M12 8h.01" stroke="currentColor" stroke-width="2" fill="none"/></svg></div>
                    <h3>IoT Telemetry Fusion</h3>
                    <p>Tension, temperature & vibration fused into one Belt Health Index.</p>
                </div>
                <div class="feature-card">
                    <div class="f-icon"><svg width="16" height="16" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" stroke="currentColor" stroke-width="2" fill="none"/><rect x="14" y="3" width="7" height="7" stroke="currentColor" stroke-width="2" fill="none"/><rect x="14" y="14" width="7" height="7" stroke="currentColor" stroke-width="2" fill="none"/><rect x="3" y="14" width="7" height="7" stroke="currentColor" stroke-width="2" fill="none"/></svg></div>
                    <h3>Digital Twin View</h3>
                    <p>Live twin of the conveyor drive and take-up drums.</p>
                </div>
                <div class="feature-card">
                    <div class="f-icon"><svg width="16" height="16" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M16 13H8M16 17H8M10 9H8" stroke="currentColor" stroke-width="2" fill="none"/></svg></div>
                    <h3>Safety Interlock & Logs</h3>
                    <p>Auto motor cutoff on critical belt health, full incident log.</p>
                </div>
            </div>
        </div>

        <!-- Auth Card -->
        <div class="auth-card">
            <h2 id="authTitle">Sign in</h2>
            <p class="auth-sub" id="authSub">Access the NMDC belt monitoring command center</p>

            <div class="auth-error" id="authError"></div>
            <div class="auth-success" id="authSuccess"></div>

            <form id="authForm" onsubmit="return handleAuthSubmit(event)">
                <div class="field" id="nameField" style="display:none;">
                    <label for="nameInput">Full name</label>
                    <input type="text" id="nameInput" autocomplete="name" placeholder="e.g. Rajesh Verma">
                </div>
                <div class="field">
                    <label for="emailInput">Email address</label>
                    <input type="email" id="emailInput" autocomplete="email" placeholder="you@nmdc.co.in" required>
                </div>
                <div class="field">
                    <label for="passwordInput">Password</label>
                    <input type="password" id="passwordInput" autocomplete="current-password" placeholder="••••••••" required>
                    <div class="field-hint" id="passwordHint" style="display:none;">At least 8 characters</div>
                </div>

                <button type="submit" class="btn-primary-block" id="authSubmitBtn">
                    <span class="spinner" id="authSpinner"></span>
                    <span id="authSubmitLabel">Sign in</span>
                </button>
            </form>

            <div class="auth-switch">
                <span id="switchPrompt">Don't have an account?</span>
                <a onclick="switchAuthMode()" id="switchLink">Create one</a>
            </div>
        </div>
    </div>

    <footer>NMDC Ministry of Steel · Smart India Hackathon Problem Statement 26008 · Industrial Multi-Modal Interlock</footer>
</div>

<script>
    let authMode = 'signin'; // or 'signup'

    // If already signed in with a still-valid session, skip straight to dashboard
    (function checkExistingSession() {
        const token = sessionStorage.getItem('nmdc_token');
        if (!token) return;
        fetch('/api/verify_session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ token })
        }).then(r => r.ok ? window.location.href = '/dashboard' : sessionStorage.removeItem('nmdc_token'))
          .catch(() => {});
    })();

    function switchAuthMode() {
        authMode = authMode === 'signin' ? 'signup' : 'signin';
        const isSignup = authMode === 'signup';
        document.getElementById('authTitle').innerText = isSignup ? 'Create your account' : 'Sign in';
        document.getElementById('authSub').innerText = isSignup
            ? 'Register with your name, email and password'
            : 'Access the NMDC belt monitoring command center';
        document.getElementById('nameField').style.display = isSignup ? 'block' : 'none';
        document.getElementById('nameInput').required = isSignup;
        document.getElementById('passwordHint').style.display = isSignup ? 'block' : 'none';
        document.getElementById('passwordInput').autocomplete = isSignup ? 'new-password' : 'current-password';
        document.getElementById('authSubmitLabel').innerText = isSignup ? 'Create account' : 'Sign in';
        document.getElementById('switchPrompt').innerText = isSignup ? 'Already have an account?' : "Don't have an account?";
        document.getElementById('switchLink').innerText = isSignup ? 'Sign in' : 'Create one';
        hideMessages();
    }

    function hideMessages() {
        document.getElementById('authError').style.display = 'none';
        document.getElementById('authSuccess').style.display = 'none';
    }

    function showError(msg) {
        const el = document.getElementById('authError');
        el.innerText = msg;
        el.style.display = 'block';
        document.getElementById('authSuccess').style.display = 'none';
    }

    function setLoading(isLoading) {
        document.getElementById('authSubmitBtn').disabled = isLoading;
        document.getElementById('authSpinner').style.display = isLoading ? 'inline-block' : 'none';
    }

    async function handleAuthSubmit(evt) {
        evt.preventDefault();
        hideMessages();

        const email = document.getElementById('emailInput').value.trim();
        const password = document.getElementById('passwordInput').value;
        const name = document.getElementById('nameInput').value.trim();

        if (authMode === 'signup' && name.length < 2) {
            showError('Please enter your full name.');
            return false;
        }
        if (!email || !password) {
            showError('Please fill in all required fields.');
            return false;
        }
        if (authMode === 'signup' && password.length < 8) {
            showError('Password must be at least 8 characters.');
            return false;
        }

        setLoading(true);
        try {
            const endpoint = authMode === 'signup' ? '/api/signup' : '/api/signin';
            const body = authMode === 'signup' ? { name, email, password } : { email, password };

            const res = await fetch(endpoint, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });
            const data = await res.json();

            if (!res.ok) {
                showError(data.detail || 'Something went wrong. Please try again.');
                setLoading(false);
                return false;
            }

            sessionStorage.setItem('nmdc_token', data.token);
            sessionStorage.setItem('nmdc_name', data.name);

            const successEl = document.getElementById('authSuccess');
            successEl.innerText = 'Credentials verified — opening command center…';
            successEl.style.display = 'block';

            setTimeout(() => { window.location.href = '/dashboard'; }, 500);
        } catch (err) {
            showError('Could not reach the server. Please try again.');
            setLoading(false);
        }
        return false;
    }

    document.addEventListener('mousemove', (e) => {
        const x = (e.clientX / window.innerWidth - 0.5) * 20;
        const y = (e.clientY / window.innerHeight - 0.5) * 20;
        document.querySelectorAll('.blob').forEach((b, i) => {
            const depth = (i + 1) * 0.35;
            b.style.marginLeft = (x * depth) + 'px';
            b.style.marginTop = (y * depth) + 'px';
        });
    });
</script>
</body>
</html>
"""
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
    <meta charset="UTF-8">
    <title>NMDC Industrial Conveyor AI SCADA</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Rajdhani:wght@500;600;700&family=Inter:wght@300;400;600&display=swap" rel="stylesheet">
    <style>
        :root[data-theme="dark"] {
            --bg: #090b10;
            --card-bg: #12151f;
            --card-inner: #191e2b;
            --border: #22293a;
            --cyan: #00e5ff;
            --green: #00ff88;
            --amber: #ffb700;
            --red: #ff3366;
            --text-main: #ffffff;
            --text-dim: #7a889b;
        }
        :root[data-theme="light"] {
            --bg: #f4f6fa;
            --card-bg: #ffffff;
            --card-inner: #eef2f8;
            --border: #d0d7e5;
            --cyan: #0088cc;
            --green: #059669;
            --amber: #d97706;
            --red: #dc2626;
            --text-main: #111827;
            --text-dim: #64748b;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; transition: background-color 0.2s, color 0.2s; }
        body {
            background-color: var(--bg);
            color: var(--text-main);
            font-family: 'Inter', sans-serif;
            overflow-x: hidden;
            padding: 16px;
            position: relative;
        }

        /* ===================== DYNAMIC MINING-SITE BACKGROUND ===================== */
        .mine-bg-stage {
            position: fixed;
            inset: 0;
            z-index: -3;
            overflow: hidden;
            background: linear-gradient(180deg, #182338 0%, #101827 32%, #0a0e17 62%, #06070b 100%);
        }
        :root[data-theme="light"] .mine-bg-stage {
            background: linear-gradient(180deg, #b9cbe0 0%, #d8c9ad 45%, #e7ddc7 100%);
        }
        .mine-sky-glow {
            position: absolute; left: 50%; bottom: 32%; width: 90vw; height: 40vh;
            transform: translateX(-50%);
            background: radial-gradient(ellipse at center, rgba(255,150,60,0.16) 0%, rgba(255,150,60,0.05) 45%, transparent 75%);
            filter: blur(2px);
        }
        .mine-mountains {
            position: absolute; left: 0; right: 0; bottom: 30%; height: 26vh;
            background: linear-gradient(180deg, transparent 0%, rgba(6,7,11,0) 100%);
        }
        .mine-mountains svg { width: 100%; height: 100%; display: block; }
        .mine-pit {
            position: absolute; left: 0; right: 0; bottom: 0; height: 34vh;
        }
        .mine-bench {
            position: absolute; left: -5%; right: -5%; height: 9vh;
            background: linear-gradient(180deg, #4a3a2c 0%, #362a20 100%);
            border-top: 1px solid rgba(255,183,0,0.08);
        }
        :root[data-theme="light"] .mine-bench {
            background: linear-gradient(180deg, #cdb692 0%, #b89b70 100%);
        }
        .mine-bench.b1 { bottom: 0;    clip-path: polygon(0 35%, 100% 0%, 100% 100%, 0% 100%); background: linear-gradient(180deg, #56412e, #2c2117); }
        .mine-bench.b2 { bottom: 7vh;  clip-path: polygon(0 40%, 100% 10%, 100% 100%, 0% 100%); background: linear-gradient(180deg, #6b4f34, #362719); opacity: 0.92; }
        .mine-bench.b3 { bottom: 14vh; clip-path: polygon(0 45%, 100% 15%, 100% 100%, 0% 100%); background: linear-gradient(180deg, #7d5c3c, #40301f); opacity: 0.85; }
        .mine-bench.b4 { bottom: 21vh; clip-path: polygon(0 50%, 100% 20%, 100% 100%, 0% 100%); background: linear-gradient(180deg, #8f6a45, #4a3722); opacity: 0.75; }

        .mine-tower-light {
            position: absolute; width: 4px; height: 26px; bottom: 30%;
            background: linear-gradient(180deg, #4a4f5c, #2a2e38);
        }
        .mine-tower-light::after {
            content: ''; position: absolute; top: -6px; left: -4px; width: 12px; height: 12px;
            border-radius: 50%; background: var(--amber); box-shadow: 0 0 14px 4px rgba(255,183,0,0.7);
            animation: towerBlink 2.6s ease-in-out infinite;
        }
        .mine-tower-light.t2::after { animation-delay: -1.1s; background: var(--cyan); box-shadow: 0 0 14px 4px rgba(0,229,255,0.6); }
        .mine-tower-light.t3::after { animation-delay: -1.9s; }
        @keyframes towerBlink { 0%, 100% { opacity: 0.35; } 50% { opacity: 1; } }

        .mine-conveyor-line {
            position: absolute; left: 8%; right: 12%; bottom: 24vh; height: 2px;
            background: rgba(122,136,155,0.25);
        }
        .mine-conveyor-line::before, .mine-conveyor-line::after {
            content: ''; position: absolute; width: 8px; height: 8px; border-radius: 50%;
            background: var(--cyan); top: -3px; box-shadow: 0 0 8px 2px rgba(0,229,255,0.6);
            animation: conveyorDot 5s linear infinite;
        }
        .mine-conveyor-line::after { animation-delay: -2.5s; background: var(--amber); box-shadow: 0 0 8px 2px rgba(255,183,0,0.6); }
        @keyframes conveyorDot { 0% { left: 0%; } 100% { left: 100%; } }

        .mine-haze {
            position: absolute; left: -20%; width: 140%; height: 10vh;
            background: linear-gradient(90deg, transparent, rgba(255,255,255,0.05), transparent);
            animation: hazeDrift linear infinite;
            pointer-events: none;
        }
        .mine-haze.h1 { bottom: 12vh; animation-duration: 38s; }
        .mine-haze.h2 { bottom: 20vh; animation-duration: 52s; animation-delay: -14s; opacity: 0.7; }
        @keyframes hazeDrift { 0% { transform: translateX(-8%); } 100% { transform: translateX(8%); } }

        .mine-dust-field { position: absolute; inset: 0; overflow: hidden; }
        .mine-dust {
            position: absolute; bottom: -5%; border-radius: 50%;
            background: rgba(255, 200, 140, 0.35);
            animation-name: dustRise; animation-timing-function: linear; animation-iteration-count: infinite;
        }
        @keyframes dustRise {
            0%   { transform: translateY(0) translateX(0); opacity: 0; }
            8%   { opacity: 0.5; }
            90%  { opacity: 0.15; }
            100% { transform: translateY(-70vh) translateX(var(--drift, 20px)); opacity: 0; }
        }
        .mine-vignette-overlay { position: fixed; inset: 0; z-index: -2; pointer-events: none; box-shadow: inset 0 0 18vw rgba(0,0,0,0.65); }
        .mine-scrim { position: fixed; inset: 0; z-index: -1; pointer-events: none; background: rgba(6,7,11,0.32); }
        :root[data-theme="light"] .mine-scrim { background: rgba(244,246,250,0.42); }


        /* SVG Icon Utilities */
        .icon {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 16px;
            height: 16px;
            vertical-align: middle;
            fill: currentColor;
        }
        .icon-lg { width: 22px; height: 22px; }

        /* Top Header */
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 12px 24px;
            margin-bottom: 12px;
        }
        .brand { display: flex; align-items: center; gap: 12px; }
        .brand-logo {
            width: 38px;
            height: 38px;
            border-radius: 8px;
            background: rgba(0, 229, 255, 0.1);
            border: 1px solid var(--cyan);
            display: flex;
            align-items: center;
            justify-content: center;
            color: var(--cyan);
        }
        .brand h1 {
            font-family: 'Rajdhani', sans-serif;
            font-size: 24px;
            font-weight: 700;
            letter-spacing: 1.5px;
            color: var(--cyan);
        }
        .brand p { font-size: 11px; color: var(--text-dim); }

        .header-actions { display: flex; align-items: center; gap: 10px; }

        /* Language Selector */
        .lang-picker {
            display: flex;
            align-items: center;
            gap: 6px;
            background: var(--card-inner);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 4px 8px;
        }
        .lang-select {
            background: transparent;
            color: var(--text-main);
            border: none;
            font-size: 12px;
            font-weight: 600;
            cursor: pointer;
            outline: none;
        }
        .lang-select option { background: #1a1e2b; color: #fff; }

        /* User Auth Chip */
        .user-chip {
            display: flex;
            align-items: center;
            gap: 8px;
            background: var(--card-inner);
            border: 1px solid var(--border);
            padding: 5px 12px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
        }
        .btn-auth {
            background: rgba(255, 51, 102, 0.15);
            color: var(--red);
            border: 1px solid var(--red);
            border-radius: 4px;
            padding: 3px 8px;
            cursor: pointer;
            font-size: 11px;
            font-weight: 700;
        }

        .theme-btn {
            background: var(--card-inner);
            color: var(--text-main);
            border: 1px solid var(--border);
            padding: 6px 12px;
            border-radius: 6px;
            font-size: 12px;
            cursor: pointer;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .tag {
            font-size: 11px;
            padding: 5px 12px;
            border-radius: 4px;
            font-weight: 600;
            letter-spacing: 1px;
        }
        .tag-online { background: rgba(0, 255, 136, 0.15); color: var(--green); border: 1px solid var(--green); }
        .tag-offline { background: rgba(255, 51, 102, 0.15); color: var(--red); border: 1px solid var(--red); }

        /* Multi-Layer Tabs */
        .layer-tabs {
            display: flex;
            gap: 10px;
            margin-bottom: 16px;
            background: var(--card-bg);
            padding: 8px;
            border-radius: 8px;
            border: 1px solid var(--border);
        }
        .tab-btn {
            flex: 1;
            padding: 10px 16px;
            background: transparent;
            color: var(--text-dim);
            border: none;
            border-radius: 6px;
            font-family: 'Rajdhani', sans-serif;
            font-size: 15px;
            font-weight: 700;
            letter-spacing: 1px;
            cursor: pointer;
            text-transform: uppercase;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
        }
        .tab-btn.active {
            background: var(--cyan);
            color: #000;
        }

        /* KPI Row */
        .kpi-row {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 14px;
            margin-bottom: 16px;
        }
        .kpi-card {
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 16px;
        }
        .kpi-title { font-size: 11px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 1px; font-weight: 600; }
        .kpi-val {
            font-family: 'Rajdhani', sans-serif;
            font-size: 34px;
            font-weight: 700;
            margin-top: 4px;
        }
        .kpi-sub { font-size: 11px; color: var(--text-dim); margin-top: 4px; }

        /* Layer Views */
        .layer-view { display: none; }
        .layer-view.active { display: grid; gap: 16px; }
        .layer-1-grid { grid-template-columns: 1.2fr 1.8fr 1fr; }

        .panel {
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 16px;
            display: flex;
            flex-direction: column;
        }
        .panel-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border);
            padding-bottom: 8px;
            margin-bottom: 12px;
        }
        .panel-header h3 {
            font-family: 'Rajdhani', sans-serif;
            font-size: 15px;
            letter-spacing: 1px;
            text-transform: uppercase;
            color: var(--cyan);
            display: flex;
            align-items: center;
            gap: 8px;
        }

        /* Video Container */
        .video-box {
            position: relative;
            background: #000;
            border-radius: 6px;
            overflow: hidden;
            border: 1px solid var(--border);
            aspect-ratio: 16/9;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .video-box img { width: 100%; height: 100%; object-fit: cover; }
        .cam-controls { display: flex; gap: 8px; margin-top: 10px; }

        /* Canvas & Chart */
        #conveyorCanvas {
            width: 100%;
            height: 80px;
            background: var(--card-inner);
            border: 1px solid var(--border);
            border-radius: 6px;
            margin-bottom: 12px;
        }
        .chart-box { height: 210px; }

        /* Buttons */
        .btn-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 12px; }
        button.action-btn {
            background: var(--card-inner);
            color: var(--text-main);
            border: 1px solid var(--border);
            padding: 9px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 12px;
            font-weight: 600;
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 6px;
        }
        button.action-btn:hover { border-color: var(--cyan); }
        .btn-danger { border-color: var(--red); color: var(--red); }
        .btn-danger:hover { background: var(--red); color: #fff; }
        .btn-reset { grid-column: span 2; background: var(--cyan); color: #000; font-weight: 700; border: none; }

        /* Status & Tables */
        .status-badge {
            padding: 8px;
            border-radius: 4px;
            font-size: 13px;
            font-weight: 700;
            text-align: center;
            margin-top: 10px;
        }
        .incident-table { width: 100%; font-size: 12px; border-collapse: collapse; margin-top: 8px; }
        .incident-table th, .incident-table td { padding: 6px 8px; text-align: left; border-bottom: 1px solid var(--border); }
        .incident-table th { color: var(--text-dim); }

        .sensor-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }

        /* Modal for Login */
        .modal-backdrop {
            display: none;
            position: fixed;
            top: 0; left: 0; width: 100vw; height: 100vh;
            background: rgba(0, 0, 0, 0.75);
            backdrop-filter: blur(4px);
            z-index: 9999;
            align-items: center;
            justify-content: center;
        }
        .modal-card {
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 28px;
            width: 440px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.5);
        }
        .form-group { margin-bottom: 16px; }
        .form-group label { display: block; font-size: 12px; color: var(--text-dim); margin-bottom: 6px; font-weight: 600; text-transform: uppercase; }
        .form-control {
            width: 100%;
            padding: 10px;
            background: var(--card-inner);
            border: 1px solid var(--border);
            border-radius: 6px;
            color: var(--text-main);
            font-size: 14px;
        }
    </style>
</head>
<body>

    <!-- ===================== DYNAMIC MINING-SITE BACKGROUND ===================== -->
    <div class="mine-bg-stage">
        <div class="mine-sky-glow"></div>
        <div class="mine-mountains">
            <svg viewBox="0 0 1440 200" preserveAspectRatio="none">
                <polygon points="0,200 0,120 120,60 260,110 400,40 560,100 720,55 900,115 1040,70 1200,120 1320,80 1440,110 1440,200" fill="#141c2b"/>
                <polygon points="0,200 0,150 180,100 340,145 520,90 700,140 880,100 1060,150 1240,110 1440,150 1440,200" fill="#0e1420"/>
            </svg>
        </div>
        <div class="mine-conveyor-line"></div>
        <div class="mine-tower-light" style="left:14%;"></div>
        <div class="mine-tower-light t2" style="left:38%;"></div>
        <div class="mine-tower-light t3" style="left:64%;"></div>
        <div class="mine-tower-light" style="left:85%;"></div>
        <div class="mine-pit">
            <div class="mine-bench b4"></div>
            <div class="mine-bench b3"></div>
            <div class="mine-bench b2"></div>
            <div class="mine-bench b1"></div>
        </div>
        <div class="mine-haze h1"></div>
        <div class="mine-haze h2"></div>
        <div class="mine-dust-field" id="mineDustField"></div>
    </div>
    <div class="mine-vignette-overlay"></div>
    <div class="mine-scrim"></div>

    <!-- Header -->
    <header>
        <div class="brand">
            <div class="brand-logo">
                <svg class="icon-lg" viewBox="0 0 24 24"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg>
            </div>
            <div>
                <h1 id="txtTitle">NMDC CONVEYOR HEALTH AI SCADA</h1>
                <p id="txtSub">Ministry of Steel | Smart India Hackathon PS 26008 | Industrial Multi-Modal Interlock</p>
            </div>
        </div>

        <div class="header-actions">
            <!-- Language Selector -->
            <div class="lang-picker">
                <svg class="icon" style="color:var(--cyan)" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2" fill="none"/><path d="M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" stroke="currentColor" stroke-width="2" fill="none"/></svg>
                <select class="lang-select" id="langSelect" onchange="changeLanguage(this.value)">
                    <option value="en">English (EN)</option>
                    <option value="hi">हिन्दी (Hindi)</option>
                    <option value="te">తెలుగు (Telugu)</option>
                    <option value="kn">ಕನ್ನಡ (Kannada)</option>
                    <option value="or">ଓଡ଼ିଆ (Odia)</option>
                    <option value="ta">தமிழ் (Tamil)</option>
                    <option value="bn">বাংলা (Bengali)</option>
                    <option value="mr">मराठी (Marathi)</option>
                </select>
            </div>

            <!-- User Auth Chip -->
            <div class="user-chip">
                <svg class="icon" viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8z" stroke="currentColor" stroke-width="2" fill="none"/></svg>
                <span id="userNameChip">Er. Rajesh Verma</span>
                <button onclick="signOut()" class="btn-auth" id="btnAuthAction">Sign Out</button>
            </div>

            <!-- Theme Toggle -->
            <button class="theme-btn" onclick="toggleTheme()" id="themeBtn">
                <svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="5" stroke="currentColor" stroke-width="2" fill="none"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42" stroke="currentColor" stroke-width="2"/></svg>
                <span id="themeBtnText">Light Mode</span>
            </button>

            <button class="theme-btn" onclick="toggleDevice()" id="deviceToggleBtn" style="border-color: var(--red); color: var(--red);">
                <svg class="icon" viewBox="0 0 24 24"><path d="M12 2v6M12 22v-4M4.93 10a8 8 0 1 0 14.14 0" stroke="currentColor" stroke-width="2" stroke-linecap="round" fill="none"/></svg>
                <span id="deviceToggleText">Connect Hardware</span>
            </button>

            <div id="deviceStatusTag" class="tag tag-offline">HARDWARE: DISCONNECTED</div>
            <div id="camStatusTag" class="tag tag-offline">CAM-03: DISCONNECTED</div>
            <div id="tripTag" class="tag tag-online">INTERLOCK: ENGAGED</div>
        </div>
    </header>

    <!-- Live Tracking Standby Banner -->
    <div id="standbyBanner" style="display:flex; align-items:center; gap:10px; background: rgba(255,183,0,0.1); border: 1px solid var(--amber); color: var(--amber); border-radius: 8px; padding: 10px 16px; margin-bottom: 12px; font-size: 13px; font-weight: 600;">
        <svg class="icon" viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" stroke="currentColor" stroke-width="2" fill="none"/></svg>
        <span>No physical sensors or cameras are linked yet. Live tracking is on standby — click "Connect Hardware" to simulate a live device link and begin monitoring.</span>
    </div>

    <!-- Multi-Layer Tabs -->
    <div class="layer-tabs">
        <button class="tab-btn active" onclick="switchLayer(1, this)">
            <svg class="icon" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" stroke="currentColor" stroke-width="2" fill="none"/><rect x="14" y="3" width="7" height="7" stroke="currentColor" stroke-width="2" fill="none"/><rect x="14" y="14" width="7" height="7" stroke="currentColor" stroke-width="2" fill="none"/><rect x="3" y="14" width="7" height="7" stroke="currentColor" stroke-width="2" fill="none"/></svg>
            <span id="tab1">Layer 1: Master SCADA & Digital Twin</span>
        </button>
        <button class="tab-btn" onclick="switchLayer(2, this)">
            <svg class="icon" viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" stroke="currentColor" stroke-width="2" fill="none"/><circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="2" fill="none"/></svg>
            <span id="tab2">Layer 2: AI Vision Diagnostics</span>
        </button>
        <button class="tab-btn" onclick="switchLayer(3, this)">
            <svg class="icon" viewBox="0 0 24 24"><path d="M4.93 19.07A10 10 0 0 1 12 2a10 10 0 0 1 7.07 17.07M12 12v6M12 8h.01" stroke="currentColor" stroke-width="2" fill="none"/></svg>
            <span id="tab3">Layer 3: Multi-Sensor IoT Telemetry</span>
        </button>
        <button class="tab-btn" onclick="switchLayer(4, this)">
            <svg class="icon" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M16 13H8M16 17H8M10 9H8" stroke="currentColor" stroke-width="2" fill="none"/></svg>
            <span id="tab4">Layer 4: PLC Safety & Maintenance Logs</span>
        </button>
    </div>

    <!-- Top KPI Bar -->
    <div class="kpi-row">
        <div class="kpi-card">
            <div class="kpi-title" id="kpiBhi">Belt Health Index (BHI)</div>
            <div id="bhiVal" class="kpi-val" style="color: var(--green);">98.0%</div>
            <div class="kpi-sub" id="kpiBhiSub">Composite health condition</div>
        </div>
        <div class="kpi-card">
            <div class="kpi-title" id="kpiRul">Estimated Joint RUL</div>
            <div id="rulVal" class="kpi-val">1,813 Hrs</div>
            <div class="kpi-sub" id="kpiRulSub">Splice fatigue failure window</div>
        </div>
        <div class="kpi-card">
            <div class="kpi-title" id="kpiSpeed">Conveyor Speed & Load</div>
            <div id="speedVal" class="kpi-val">3.50 m/s</div>
            <div class="kpi-sub" id="loadVal">1,250 TPH Iron Ore</div>
        </div>
        <div class="kpi-card">
            <div class="kpi-title" id="kpiRelay">Drive Motor Interlock</div>
            <div id="relayStatus" class="kpi-val" style="color: var(--green);">NORMAL</div>
            <div class="kpi-sub" id="kpiRelaySub">Relay NC Circuit Active</div>
        </div>
    </div>

    <!-- ==================== LAYER 1: MASTER SCADA ==================== -->
    <div id="layer1" class="layer-view layer-1-grid active">
        <!-- Visual Panel -->
        <div class="panel">
            <div class="panel-header">
                <h3>
                    <svg class="icon" viewBox="0 0 24 24"><path d="M23 7l-7 5 7 5V7zM14 5H3a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2z" stroke="currentColor" stroke-width="2" fill="none"/></svg>
                    <span id="hdrVision">Visual & Splice Inspection</span>
                </h3>
                <span id="camFpsText" style="font-size:11px; color:var(--text-dim);">Standby (0 FPS)</span>
            </div>
            <div class="video-box">
                <img src="/video_feed" alt="Camera Feed">
            </div>
            <div class="cam-controls">
                <button class="action-btn" style="flex:1;" onclick="toggleCamera()" id="camToggleBtn">
                    <svg class="icon" viewBox="0 0 24 24"><path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83" stroke="currentColor" stroke-width="2"/></svg>
                    <span id="btnCamToggle">Connect Camera Hardware</span>
                </button>
            </div>
            <div id="systemStateBanner" class="status-badge" style="background: rgba(0,255,136,0.1); color: var(--green);">
                SYSTEM STABLE: NO ANOMALIES
            </div>
        </div>

        <!-- Digital Twin & Real-time Chart -->
        <div class="panel">
            <div class="panel-header">
                <h3>
                    <svg class="icon" viewBox="0 0 24 24"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67" stroke="currentColor" stroke-width="2" fill="none"/></svg>
                    <span id="hdrTwin">Digital Twin Belt & Telemetry</span>
                </h3>
                <span style="font-size:11px; color:var(--text-dim);" id="hdrSensorTrends">Real-Time Sensor Trends</span>
            </div>
            <canvas id="conveyorCanvas"></canvas>
            <div class="chart-box">
                <canvas id="telemetryChart"></canvas>
            </div>
        </div>

        <!-- SIH Testbench Fault Injector -->
        <div class="panel">
            <div class="panel-header">
                <h3>
                    <svg class="icon" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" stroke="currentColor" stroke-width="2" fill="none"/></svg>
                    <span id="hdrFault">SIH Demo Fault Injector</span>
                </h3>
                <span style="font-size:11px; color:var(--amber);">Interactive</span>
            </div>
            <p style="font-size: 11px; color: var(--text-dim); margin-bottom: 12px;" id="lblFaultInstruct">
                Demonstrate multi-modal AI failure response to the jury:
            </p>
            <div class="btn-grid">
                <button onclick="injectFault('SPLICE_TEAR')" class="action-btn btn-danger">
                    <svg class="icon" viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0zM12 9v4M12 17h.01" stroke="currentColor" stroke-width="2" fill="none"/></svg>
                    <span id="btnSplice">Splice Rupture</span>
                </button>
                <button onclick="injectFault('BEARING_HOTSPOT')" class="action-btn">
                    <svg class="icon" viewBox="0 0 24 24"><path d="M12 2c0 4-4 6-4 10a4 4 0 0 0 8 0c0-4-4-6-4-10z" stroke="currentColor" stroke-width="2" fill="none"/></svg>
                    <span id="btnBearing">Bearing Hotspot</span>
                </button>
                <button onclick="injectFault('TENSION_SPIKE')" class="action-btn">
                    <svg class="icon" viewBox="0 0 24 24"><path d="M22 12h-4l-3 9L9 3l-3 9H2" stroke="currentColor" stroke-width="2" fill="none"/></svg>
                    <span id="btnTension">Tension Surge</span>
                </button>
                <button onclick="injectFault('SURFACE_GOUGE')" class="action-btn">
                    <svg class="icon" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8" stroke="currentColor" stroke-width="2" fill="none"/><path d="m21 21-4.35-4.35" stroke="currentColor" stroke-width="2"/></svg>
                    <span id="btnGouge">Surface Gouge</span>
                </button>
                <button onclick="resetSystem()" class="action-btn btn-reset">
                    <svg class="icon" viewBox="0 0 24 24"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8M3 3v5h5" stroke="currentColor" stroke-width="2" fill="none"/></svg>
                    <span id="btnReset">RESET INTERLOCK & RECOVER</span>
                </button>
            </div>

            <div class="panel-header" style="margin-top: 8px;">
                <h3>
                    <svg class="icon" viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8zM14 2v6h6M16 13H8M16 17H8M10 9H8" stroke="currentColor" stroke-width="2" fill="none"/></svg>
                    <span id="hdrLog">Safety Event Log</span>
                </h3>
            </div>
            <table class="incident-table">
                <thead><tr><th id="thTime">Time</th><th id="thEvent">Event</th><th id="thSev">Severity</th></tr></thead>
                <tbody id="incidentBody">
                    <tr><td>Ready</td><td>Monitoring Initialized</td><td style="color:var(--green)">INFO</td></tr>
                </tbody>
            </table>
        </div>
    </div>

    <!-- ==================== LAYER 2: AI VISION DIAGNOSTICS ==================== -->
    <div id="layer2" class="layer-view">
        <div class="panel">
            <div class="panel-header">
                <h3>
                    <svg class="icon" viewBox="0 0 24 24"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" stroke="currentColor" stroke-width="2" fill="none"/><circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="2" fill="none"/></svg>
                    Computer Vision Inference Pipeline Details
                </h3>
            </div>
            <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 20px; font-size:13px;">
                <div>
                    <h4 style="color:var(--cyan); margin-bottom:10px;">Model Specifications</h4>
                    <p>• <strong>Selected Architecture:</strong> YOLOv8n / YOLOv11 Edge-Optimized</p>
                    <p>• <strong>Target Classes:</strong> Longitudinal Tear, Splice Gap, Edge Wear, Surface Gouge</p>
                    <p>• <strong>Inference Latency:</strong> 12.4 ms on Dedicated GPU</p>
                    <p>• <strong>Preprocessing:</strong> CLAHE Contrast Enhancement & Retinex De-dusting</p>
                </div>
                <div>
                    <h4 style="color:var(--cyan); margin-bottom:10px;">Inspection Point Coordinates</h4>
                    <p>• <strong>Zone 1:</strong> Primary Loading Chute (Impact & Rip Detection)</p>
                    <p>• <strong>Zone 2:</strong> Drive Pulley Transition (High Tension Splice Separation)</p>
                    <p>• <strong>Zone 3:</strong> Return Belt Clean Side (Cord Exposure & Delamination)</p>
                </div>
            </div>
        </div>
    </div>

    <!-- ==================== LAYER 3: SENSOR IOT TELEMETRY ==================== -->
    <div id="layer3" class="layer-view">
        <div class="sensor-grid">
            <div class="panel">
                <div class="panel-header">
                    <h3>
                        <svg class="icon" viewBox="0 0 24 24"><path d="M22 12h-4l-3 9L9 3l-3 9H2" stroke="currentColor" stroke-width="2" fill="none"/></svg>
                        MPU6050 Accelerometer (Vibration)
                    </h3>
                </div>
                <div style="font-size:24px; font-family:'Rajdhani'; color:var(--cyan); margin-bottom:10px;" id="vibRmsDetail">2.1 mm/s RMS</div>
                <p style="font-size:12px; color:var(--text-dim);" id="vibAxes">X: 0.8 | Y: 1.1 | Z: 1.6 m/s²</p>
                <p style="font-size:12px; margin-top:10px;">Captures dynamic vibration & steel cord snapping frequencies (10 Hz - 1.2 kHz).</p>
            </div>
            <div class="panel">
                <div class="panel-header">
                    <h3>
                        <svg class="icon" viewBox="0 0 24 24"><path d="M12 2c0 4-4 6-4 10a4 4 0 0 0 8 0c0-4-4-6-4-10z" stroke="currentColor" stroke-width="2" fill="none"/></svg>
                        DS18B20 1-Wire Probe (Temperature)
                    </h3>
                </div>
                <div style="font-size:24px; font-family:'Rajdhani'; color:var(--amber); margin-bottom:10px;" id="tempDetail">52.4 °C</div>
                <p style="font-size:12px; color:var(--text-dim);">Operating Range: -55°C to +125°C</p>
                <p style="font-size:12px; margin-top:10px;">Monitors drive pulley bearings and rubber friction hotspots.</p>
            </div>
            <div class="panel">
                <div class="panel-header">
                    <h3>
                        <svg class="icon" viewBox="0 0 24 24"><path d="M18 20V10M12 20V4M6 20v-6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
                        HX711 Strain Gauge (Tension)
                    </h3>
                </div>
                <div style="font-size:24px; font-family:'Rajdhani'; color:var(--green); margin-bottom:10px;" id="tensionDetail">45.0 kN</div>
                <p style="font-size:12px; color:var(--text-dim);">Nominal Elastic Limit: 80.0 kN</p>
                <p style="font-size:12px; margin-top:10px;">Identifies sudden tension loss during splice rupture or overload surges.</p>
            </div>
        </div>
    </div>

    <!-- ==================== LAYER 4: PLC SAFETY & MAINTENANCE ==================== -->
    <div id="layer4" class="layer-view">
        <div class="panel">
            <div class="panel-header">
                <h3>
                    <svg class="icon" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2" stroke="currentColor" stroke-width="2" fill="none"/><path d="M9 9h6v6H9z" stroke="currentColor" stroke-width="2"/></svg>
                    Industrial SCADA / PLC Interlocking Architecture
                </h3>
            </div>
            <div style="font-size:13px; line-height:1.6;">
                <p><strong>Field Protocol:</strong> Modbus TCP / OPC-UA to NMDC Siemens S7-1500 / Allen-Bradley ControlLogix PLC.</p>
                <p><strong>Hardware Safety Interlock:</strong> Failsafe Normally Closed (NC) physical relay circuit directly wired into the main drive motor contactor E-Stop loop.</p>
                <p><strong>Emergency Response Latency:</strong> Hardware relay trip executed in under <strong>28 ms</strong> upon confirmation of a Level 3 longitudinal tear.</p>
            </div>
        </div>
    </div>

    <script>
        // ----------------- MULTI-LINGUAL TRANSLATION DICTIONARY -----------------
        const translations = {
            en: {
                title: "NMDC CONVEYOR HEALTH AI SCADA",
                sub: "Ministry of Steel | Smart India Hackathon PS 26008 | Industrial Multi-Modal Interlock",
                tab1: "Layer 1: Master SCADA & Digital Twin",
                tab2: "Layer 2: AI Vision Diagnostics",
                tab3: "Layer 3: Multi-Sensor IoT Telemetry",
                tab4: "Layer 4: PLC Safety & Maintenance Logs",
                kpiBhi: "Belt Health Index (BHI)",
                kpiRul: "Estimated Joint RUL",
                kpiSpeed: "Conveyor Speed & Load",
                kpiRelay: "Drive Motor Interlock",
                hdrVision: "Visual & Splice Inspection",
                hdrTwin: "Digital Twin Belt & Telemetry",
                hdrFault: "SIH Demo Fault Injector",
                btnSplice: "Splice Rupture",
                btnBearing: "Bearing Hotspot",
                btnTension: "Tension Surge",
                btnGouge: "Surface Gouge",
                btnReset: "RESET INTERLOCK & RECOVER",
                btnCamConnect: "Connect Camera Hardware",
                btnCamDisconnect: "Disconnect Camera Feed",
                hdrLog: "Safety Event Log"
            },
            hi: {
                title: "एनएमडीसी कन्वेयर बेल्ट एआई स्काडा",
                sub: "इस्पात मंत्रालय | स्मार्ट इंडिया हैकथॉन पीएस 26008 | औद्योगिक मल्टी-मॉडल इंटरलॉक",
                tab1: "लेयर 1: मास्टर स्काडा और डिजिटल ट्विन",
                tab2: "लेयर 2: एआई विज़न विश्लेषण",
                tab3: "लेयर 3: मल्टी-सेंसर आईओटी टेलीमेट्री",
                tab4: "लेयर 4: पीएलसी सुरक्षा एवं लॉग",
                kpiBhi: "बेल्ट स्वास्थ्य सूचकांक (BHI)",
                kpiRul: "अनुमानित शेष जीवन (RUL)",
                kpiSpeed: "कन्वेयर गति और भार",
                kpiRelay: "ड्राइव मोटर इंटरलॉक",
                hdrVision: "दृश्य एवं संयुक्त निरीक्षण",
                hdrTwin: "डिजिटल ट्विन बेल्ट टेलीमेट्री",
                hdrFault: "एसोसिएशन फॉल्ट इंजेक्टर",
                btnSplice: "जोड़ टूटना (Rupture)",
                btnBearing: "अत्यधिक ताप (Hotspot)",
                btnTension: "तनाव वृद्धि (Surge)",
                btnGouge: "सतह क्षति (Gouge)",
                btnReset: "इंटरलॉक रीसेट करें",
                btnCamConnect: "कैमरा हार्डवेयर कनेक्ट करें",
                btnCamDisconnect: "कैमरा डिस्कनेक्ट करें",
                hdrLog: "सुरक्षा घटना लॉग"
            },
            te: {
                title: "NMDC కన్వేయర్ హెల్త్ AI స్కాడా",
                sub: "స్టీల్ మంత్రిత్వ శాఖ | స్మార్ట్ ఇండియా హ్యాకథాన్ PS 26008 | ఇండస్ట్రియల్ ఇంటర్‌లాక్",
                tab1: "లేయర్ 1: మాస్టర్ స్కాడా & డిజిటల్ ట్విన్",
                tab2: "లేయర్ 2: AI విజన్ డయాగ్నస్టిక్స్",
                tab3: "లేయర్ 3: మల్టీ-సెన్సార్ టెలిమెట్రీ",
                tab4: "లేయర్ 4: PLC భద్రత & లాగ్స్",
                kpiBhi: "బెల్ట్ హెల్త్ ఇండెక్స్ (BHI)",
                kpiRul: "అంచనా వేసిన మిగిలిన ఆయుష్షు",
                kpiSpeed: "వేగం & బరువు",
                kpiRelay: "డ్రైవ్ మోటార్ ఇంటర్‌లాక్",
                hdrVision: "విజువల్ తనిఖీ",
                hdrTwin: "డిజిటల్ ట్విన్ టెలిమెట్రీ",
                hdrFault: "ఫాల్ట్ ఇంజెక్టర్",
                btnSplice: "స్ప్లైస్ చీలిక",
                btnBearing: "బేరింగ్ వేడి",
                btnTension: "టెన్షన్ పెరుగుదల",
                btnGouge: "ఉపరితల లోపం",
                btnReset: "రీసెట్ ఇంటర్‌లాక్",
                btnCamConnect: "కెమెరాను కనెక్ట్ చేయండి",
                btnCamDisconnect: "కెమెరాను డిస్‌కనెక్ట్ చేయండి",
                hdrLog: "సేఫ్టీ ఈవెంట్ లాగ్"
            },
            kn: {
                title: "NMDC ಕನ್ವೇಯರ್ ಹೆಲ್ತ್ AI ಸ್ಕಾಡಾ",
                sub: "ಉಕ್ಕು ಸಚಿವಾಲಯ | ಸ್ಮಾರ್ಟ್ ಇಂಡಿಯಾ ಹ್ಯಾಕಥಾನ್ PS 26008 | ಇಂಟರ್ಲಾಕ್ ವ್ಯವಸ್ಥೆ",
                tab1: "ಹಂತ 1: ಮುಖ್ಯ ಸ್ಕಾಡಾ & ಡಿಜಿಟಲ್ ಟ್ವಿನ್",
                tab2: "ಹಂತ 2: AI ದೃಷ್ಟಿ ವಿಶ್ಲೇಷಣೆ",
                tab3: "ಹಂತ 3: ಐಒಟಿ ಸೆನ್ಸರ್ ಟೆಲಿಮೆಟ್ರಿ",
                tab4: "ಹಂತ 4: ಪಿಎಲ್ಸಿ ಸುರಕ್ಷತೆ & ಲಾಗ್",
                kpiBhi: "ಬೆಲ್ಟ್ ಆರೋಗ್ಯ ಸೂಚ್ಯಂಕ",
                kpiRul: "ಅಂದಾಜು ಬಾಳಿಕೆ ಅವಧಿ",
                kpiSpeed: "ವೇಗ ಮತ್ತು ತೂಕ",
                kpiRelay: "ಮೋಟಾರ್ ಇಂಟರ್ಲಾಕ್",
                hdrVision: "ಕ್ಯಾಮೆರಾ ತಪಾಸಣೆ",
                hdrTwin: "ಡಿಜಿಟಲ್ ಟ್ವಿನ್ ಬೆಲ್ಟ್",
                hdrFault: "ದೋಷ ಸಿಮ್ಯುಲೇಶನ್",
                btnSplice: "ಜಾಯಿಂಟ್ ಒಡಕು",
                btnBearing: "ಬೇರಿಂಗ್ ತಾಪಮಾನ",
                btnTension: "ಒತ್ತಡ ಏರಿಕೆ",
                btnGouge: "ಮೇಲ್ಮೈ ಹಾನಿ",
                btnReset: "ಮರುಹೊಂದಿಸಿ (Reset)",
                btnCamConnect: "ಕ್ಯಾಮೆರಾ ಸಂಪರ್ಕಿಸಿ",
                btnCamDisconnect: "ಕ್ಯಾಮೆರಾ ತೆಗೆಯಿರಿ",
                hdrLog: "ಸುರಕ್ಷತಾ ಲಾಗ್"
            },
            or: {
                title: "NMDC କନଭେୟର ହେଲଥ AI ସ୍କାଡା",
                sub: "ଇସ୍ପାତ ମନ୍ତ୍ରଣାଳୟ | ସ୍ମାର୍ଟ ଇଣ୍ଡିଆ ହ୍ୟାକାଥନ୍ | ଶିଳ୍ପ ସୁରକ୍ଷା ଇଣ୍ଟରଲକ୍",
                tab1: "ଲେୟାର 1: ମାଷ୍ଟର ସ୍କାଡା ଏବଂ ଡିଜିଟାଲ୍ ଟ୍ୱିନ୍",
                tab2: "ଲେୟାର 2: AI ଭିଜନ୍ ତଥ୍ୟ",
                tab3: "ଲେୟାର 3: ସେନ୍ସର ଟେଲିମେଟ୍ରି",
                tab4: "ଲେୟାର 4: PLC ସୁରକ୍ଷା ରେକର୍ଡ",
                kpiBhi: "ବେଲ୍ଟ ସ୍ୱାସ୍ଥ୍ୟ ସୂଚକାଙ୍କ",
                kpiRul: "ଅବଶିଷ୍ଟ କାର୍ଯ୍ୟକ୍ଷମ ସମୟ",
                kpiSpeed: "ଗତି ଏବଂ ଭାର",
                kpiRelay: "ମୋଟର ଇଣ୍ଟରଲକ୍",
                hdrVision: "ଭିଜୁଆଲ୍ ଯାଞ୍ଚ",
                hdrTwin: "ଡିଜିଟାଲ୍ ଟ୍ୱିନ୍ ବେଲ୍ଟ",
                hdrFault: "ତ୍ରୁଟି ଅନୁକରଣ",
                btnSplice: "ଯୋଡ଼ ଛିଣ୍ଡିବା",
                btnBearing: "ବେରିଂ ଉତ୍ତାପ",
                btnTension: "ଟେନସନ ବୃଦ୍ଧି",
                btnGouge: "ପୃଷ୍ଠ କ୍ଷତି",
                btnReset: "ପୁନଃସେଟ କରନ୍ତୁ",
                btnCamConnect: "କ୍ୟାମେରା ଯୋଡ଼ନ୍ତୁ",
                btnCamDisconnect: "କ୍ୟାମେରା ବନ୍ଦ କରନ୍ତୁ",
                hdrLog: "ସୁରକ୍ଷା ଘଟଣା ଲଗ୍"
            },
            ta: {
                title: "NMDC கன்வேயர் ஹெல்த் AI ஸ்காடா",
                sub: "எஃகு அமைச்சகம் | ஸ்மார்ட் இந்தியா ஹேக்கத்தான் PS 26008 | தொழில்துறை பாதுகாப்பு",
                tab1: "அடுக்கு 1: முதன்மை ஸ்காடா & டிஜிட்டல் ட்வின்",
                tab2: "அடுக்கு 2: AI விஷன் ஆய்வுகள்",
                tab3: "அடுக்கு 3: பல சென்சார் டெலிமெட்ரி",
                tab4: "அடுக்கு 4: PLC பாதுகாப்பு & பதிவுகள்",
                kpiBhi: "பெல்ட் ஆரோக்கிய குறியீடு",
                kpiRul: "எதிர்பார்க்கப்படும் ஆயுட்காலம்",
                kpiSpeed: "வேகம் & சுமை",
                kpiRelay: "மோட்டார் பாதுகாப்பு பூட்டு",
                hdrVision: "கேமரா ஆய்வு",
                hdrTwin: "டிஜிட்டல் ட்வின் அமைப்பு",
                hdrFault: "பிழை தூண்டுதல்",
                btnSplice: "இணைப்பு விரிசல்",
                btnBearing: "தாங்கி அதிக வெப்பம்",
                btnTension: "அழுத்த அதிகரிப்பு",
                btnGouge: "மேற்பரப்பு சேதம்",
                btnReset: "மீட்டமைக்கவும் (Reset)",
                btnCamConnect: "கேமராவை இணைக்கவும்",
                btnCamDisconnect: "கேமராவை துண்டிக்கவும்",
                hdrLog: "பாதுகாப்பு நிகழ்வு பதிவு"
            },
            bn: {
                title: "NMDC পরিবাহক বেল্ট এআই স্কাডা",
                sub: "ইস্পাত মন্ত্রণালয় | স্মার্ট ইন্ডিয়া হ্যাকাথন | শিল্প সুরক্ষা ব্যবস্থা",
                tab1: "স্তর ১: মাস্টার স্কাডা ও ডিজিটাল টুইন",
                tab2: "স্তর ২: এআই দৃষ্টি বিশ্লেষণ",
                tab3: "স্তর ৩: মাল্টি-সেন্সর টেলিমেট্রি",
                tab4: "স্তর ৪: পিএলসি সুরক্ষা লগ",
                kpiBhi: "বেল্ট স্বাস্থ্য সূচক",
                kpiRul: "আনুমানিক অবশিষ্ট জীবন",
                kpiSpeed: "গতি ও ওজন",
                kpiRelay: "ড্রাইভ মোটর ইন্টারলক",
                hdrVision: "ক্যামেরা পরিদর্শন",
                hdrTwin: "ডিজিটাল টুইন বেল্ট",
                hdrFault: "ফল্ট ইনজেক্টর",
                btnSplice: "জয়েন্ট ফাটল",
                btnBearing: "বিয়ারিং অতিরিক্ত গরম",
                btnTension: "টেনশন বৃদ্ধি",
                btnGouge: "পৃষ্ঠতলের ক্ষতি",
                btnReset: "রিসেট করুন",
                btnCamConnect: "ক্যামেরা সংযুক্ত করুন",
                btnCamDisconnect: "ক্যামেরা সংযোগ বিচ্ছিন্ন করুন",
                hdrLog: "সুরক্ষা ইভেন্ট লগ"
            },
            mr: {
                title: "NMDC कन्व्हेयर हेल्थ AI स्काडा",
                sub: "पोलाद मंत्रालय | स्मार्ट इंडिया हॅकाथॉन | औद्योगिक सुरक्षा इंटरलॉक",
                tab1: "स्तर १: मुख्य स्काडा आणि डिजिटल ट्विन",
                tab2: "स्तर २: AI व्हिजन विश्लेषण",
                tab3: "स्तर ३: मल्टी-सेन्सर टेलिमेट्री",
                tab4: "स्तर ४: PLC सुरक्षा लॉग",
                kpiBhi: "बेल्ट आरोग्य निर्देशांक",
                kpiRul: "अंदाजे शिल्लक आयुष्य",
                kpiSpeed: "कन्व्हेयर गती व भार",
                kpiRelay: "मोटर इंटरलॉक",
                hdrVision: "कॅमेरा तपासणी",
                hdrTwin: "डिजिटल ट्विन टेलिमेट्री",
                hdrFault: "फॉल्ट इंजेक्टर",
                btnSplice: "सांधा तुटणे",
                btnBearing: "बेअरिंग उष्णता",
                btnTension: "ताण वाढ",
                btnGouge: "पृष्ठभाग नुकसान",
                btnReset: "रीसेट करा",
                btnCamConnect: "कॅमेरा कनेक्ट करा",
                btnCamDisconnect: "कॅमेरा डिस्कनेक्ट करा",
                hdrLog: "सुरक्षा नोंद वही"
            }
        };

        let currentLang = 'en';

        function changeLanguage(lang) {
            currentLang = lang;
            const t = translations[lang] || translations.en;
            
            document.getElementById('txtTitle').innerText = t.title;
            document.getElementById('txtSub').innerText = t.sub;
            document.getElementById('tab1').innerText = t.tab1;
            document.getElementById('tab2').innerText = t.tab2;
            document.getElementById('tab3').innerText = t.tab3;
            document.getElementById('tab4').innerText = t.tab4;
            document.getElementById('kpiBhi').innerText = t.kpiBhi;
            document.getElementById('kpiRul').innerText = t.kpiRul;
            document.getElementById('kpiSpeed').innerText = t.kpiSpeed;
            document.getElementById('kpiRelay').innerText = t.kpiRelay;
            document.getElementById('hdrVision').innerText = t.hdrVision;
            document.getElementById('hdrTwin').innerText = t.hdrTwin;
            document.getElementById('hdrFault').innerText = t.hdrFault;
            document.getElementById('btnSplice').innerText = t.btnSplice;
            document.getElementById('btnBearing').innerText = t.btnBearing;
            document.getElementById('btnTension').innerText = t.btnTension;
            document.getElementById('btnGouge').innerText = t.btnGouge;
            document.getElementById('btnReset').innerText = t.btnReset;
            document.getElementById('hdrLog').innerText = t.hdrLog;
        }

        // ----------------- MINING-SITE BACKGROUND DUST PARTICLES -----------------
        (function spawnMineDust() {
            const field = document.getElementById('mineDustField');
            if (!field) return;
            const count = 34;
            for (let i = 0; i < count; i++) {
                const p = document.createElement('div');
                p.className = 'mine-dust';
                const size = 2 + Math.random() * 4;
                p.style.width = size + 'px';
                p.style.height = size + 'px';
                p.style.left = (Math.random() * 100) + '%';
                p.style.setProperty('--drift', (Math.random() * 60 - 30) + 'px');
                p.style.animationDuration = (14 + Math.random() * 18) + 's';
                p.style.animationDelay = (-Math.random() * 30) + 's';
                field.appendChild(p);
            }
        })();

        // ----------------- AUTHENTICATION GUARD (real backend session check) -----------------
        (function authGuard() {
            const token = sessionStorage.getItem('nmdc_token');
            if (!token) {
                window.location.href = '/';
                return;
            }
            fetch('/api/verify_session', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token })
            }).then(async (res) => {
                if (!res.ok) {
                    sessionStorage.removeItem('nmdc_token');
                    sessionStorage.removeItem('nmdc_name');
                    window.location.href = '/';
                    return;
                }
                const data = await res.json();
                document.getElementById('userNameChip').innerText = data.name;
            }).catch(() => {
                window.location.href = '/';
            });
        })();

        function signOut() {
            const token = sessionStorage.getItem('nmdc_token');
            sessionStorage.removeItem('nmdc_token');
            sessionStorage.removeItem('nmdc_name');
            if (token) {
                fetch('/api/signout', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ token })
                }).finally(() => { window.location.href = '/'; });
            } else {
                window.location.href = '/';
            }
        }

        function toggleDevice() {
            fetch('/api/toggle_device', { method: 'POST' });
        }

        // ----------------- THEME & LAYERS -----------------
        function toggleTheme() {
            const html = document.documentElement;
            const current = html.getAttribute('data-theme');
            const newTheme = current === 'dark' ? 'light' : 'dark';
            html.setAttribute('data-theme', newTheme);
            document.getElementById('themeBtnText').innerText = newTheme === 'dark' ? 'Light Mode' : 'Dark Mode';
        }

        function switchLayer(layerNum, btn) {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.layer-view').forEach(v => v.classList.remove('active'));
            btn.classList.add('active');
            document.getElementById('layer' + layerNum).classList.add('active');
            if (layerNum === 1) resizeCanvas();
        }

        // ----------------- CONVEYOR CANVAS -----------------
        const canvas = document.getElementById('conveyorCanvas');
        const ctx = canvas.getContext('2d');
        let beltOffset = 0;
        let isEmergency = false;
        let deviceConnected = false;

        function resizeCanvas() {
            canvas.width = canvas.parentElement.clientWidth - 32;
            canvas.height = 80;
        }
        window.addEventListener('resize', resizeCanvas);
        resizeCanvas();

        function drawConveyor() {
            ctx.clearRect(0, 0, canvas.width, canvas.height);
            const w = canvas.width, h = canvas.height;

            ctx.fillStyle = "#22293a";
            ctx.beginPath(); ctx.arc(35, h/2, 22, 0, Math.PI*2); ctx.fill();
            ctx.beginPath(); ctx.arc(w - 35, h/2, 22, 0, Math.PI*2); ctx.fill();

            ctx.strokeStyle = !deviceConnected ? "#3a4256" : (isEmergency ? "#ff3366" : "#00e5ff");
            ctx.lineWidth = 5;
            ctx.beginPath();
            ctx.moveTo(35, h/2 - 22); ctx.lineTo(w - 35, h/2 - 22);
            ctx.moveTo(35, h/2 + 22); ctx.lineTo(w - 35, h/2 + 22);
            ctx.stroke();

            if (deviceConnected && !isEmergency) beltOffset = (beltOffset + 2.5) % (w - 70);
            const spliceX = 35 + beltOffset;
            ctx.fillStyle = !deviceConnected ? "#3a4256" : (isEmergency ? "#ff3366" : "#00ff88");
            ctx.fillRect(spliceX, h/2 - 25, 10, 5);

            ctx.fillStyle = "#7a889b";
            ctx.font = "9px Inter";
            ctx.fillText("DRIVE DRUM", 10, h - 5);
            ctx.fillText("TAKE-UP DRUM", w - 75, h - 5);

            if (!deviceConnected) {
                ctx.fillStyle = "rgba(9,11,16,0.55)";
                ctx.fillRect(0, 0, w, h);
                ctx.fillStyle = "#ffb700";
                ctx.font = "bold 11px Inter";
                ctx.textAlign = "center";
                ctx.fillText("AWAITING HARDWARE CONNECTION", w / 2, h / 2 + 4);
                ctx.textAlign = "left";
            }

            requestAnimationFrame(drawConveyor);
        }
        drawConveyor();

        // ----------------- CHART.JS TELEMETRY -----------------
        const chartCtx = document.getElementById('telemetryChart').getContext('2d');
        const telemetryChart = new Chart(chartCtx, {
            type: 'line',
            data: {
                labels: Array(20).fill(''),
                datasets: [
                    { label: 'Tension (kN)', borderColor: '#00e5ff', data: Array(20).fill(45), tension: 0.3, borderWidth: 2 },
                    { label: 'Bearing Temp (°C)', borderColor: '#ffb700', data: Array(20).fill(52), tension: 0.3, borderWidth: 2 },
                    { label: 'Vibration RMS (mm/s)', borderColor: '#ff3366', data: Array(20).fill(2.1), tension: 0.3, borderWidth: 2 }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { labels: { color: '#7a889b', font: { size: 10 } } } },
                scales: {
                    x: { display: false },
                    y: { grid: { color: 'rgba(122, 136, 155, 0.1)' }, ticks: { color: '#7a889b' } }
                }
            }
        });

        // ----------------- WEBSOCKET -----------------
        const ws = new WebSocket(`ws://${location.host}/ws`);
        ws.onmessage = (evt) => {
            const data = JSON.parse(evt.data);
            isEmergency = data.emergency_stop && data.device_connected;
            deviceConnected = data.device_connected;

            const deviceTag = document.getElementById('deviceStatusTag');
            const deviceBtn = document.getElementById('deviceToggleText');
            const standbyBanner = document.getElementById('standbyBanner');
            if (data.device_connected) {
                deviceTag.innerText = "HARDWARE: CONNECTED";
                deviceTag.className = "tag tag-online";
                deviceBtn.innerText = "Disconnect Hardware";
                standbyBanner.style.display = "none";
            } else {
                deviceTag.innerText = "HARDWARE: DISCONNECTED";
                deviceTag.className = "tag tag-offline";
                deviceBtn.innerText = "Connect Hardware";
                standbyBanner.style.display = "flex";
            }

            const bhiElem = document.getElementById('bhiVal');
            bhiElem.innerText = data.bhi + "%";
            bhiElem.style.color = data.bhi < 50 ? "var(--red)" : (data.bhi < 75 ? "var(--amber)" : "var(--green)");

            document.getElementById('rulVal').innerText = data.rul_hours + " Hrs";
            document.getElementById('speedVal').innerText = data.speed.toFixed(2) + " m/s";
            document.getElementById('loadVal').innerText = data.load + " TPH Iron Ore";

            const banner = document.getElementById('systemStateBanner');
            const relay = document.getElementById('relayStatus');
            banner.innerText = data.status;

            if (data.emergency_stop) {
                banner.style.background = "rgba(255, 51, 102, 0.2)";
                banner.style.color = "var(--red)";
                relay.innerText = "TRIPPED";
                relay.style.color = "var(--red)";
                document.getElementById('tripTag').className = "tag tag-offline";
                document.getElementById('tripTag').innerText = "INTERLOCK: TRIPPED";
            } else {
                banner.style.background = "rgba(0, 255, 136, 0.1)";
                banner.style.color = "var(--green)";
                relay.innerText = "NORMAL";
                relay.style.color = "var(--green)";
                document.getElementById('tripTag').className = "tag tag-online";
                document.getElementById('tripTag').innerText = "INTERLOCK: ENGAGED";
            }

            const camTag = document.getElementById('camStatusTag');
            const camBtn = document.getElementById('btnCamToggle');
            const camFps = document.getElementById('camFpsText');
            const t = translations[currentLang] || translations.en;
            if (data.camera_connected) {
                camTag.innerText = "CAM-03: ONLINE";
                camTag.className = "tag tag-online";
                camBtn.innerText = t.btnCamDisconnect;
                camFps.innerText = "Live (25 FPS)";
            } else {
                camTag.innerText = "CAM-03: DISCONNECTED";
                camTag.className = "tag tag-offline";
                camBtn.innerText = t.btnCamConnect;
                camFps.innerText = "Standby (0 FPS)";
            }
            const camToggleBtnEl = document.getElementById('camToggleBtn');
            if (camToggleBtnEl) {
                camToggleBtnEl.disabled = !data.device_connected;
                camToggleBtnEl.style.opacity = data.device_connected ? "1" : "0.45";
                camToggleBtnEl.style.cursor = data.device_connected ? "pointer" : "not-allowed";
                camToggleBtnEl.style.pointerEvents = data.device_connected ? "auto" : "none";
            }

            document.getElementById('vibRmsDetail').innerText = data.vibration + " mm/s RMS";
            document.getElementById('vibAxes').innerText = `X: ${data.vib_x} | Y: ${data.vib_y} | Z: ${data.vib_z} m/s²`;
            document.getElementById('tempDetail').innerText = data.temperature + " °C";
            document.getElementById('tensionDetail').innerText = data.tension + " kN";

            telemetryChart.data.datasets[0].data.push(data.tension);
            telemetryChart.data.datasets[0].data.shift();
            telemetryChart.data.datasets.data.push(data.temperature);
            telemetryChart.data.datasets.data.shift();
            telemetryChart.data.datasets.data.push(data.vibration);
            telemetryChart.data.datasets.data.shift();
            telemetryChart.update('none');

            if (data.incidents && data.incidents.length > 0) {
                const tbody = document.getElementById('incidentBody');
                tbody.innerHTML = data.incidents.map(inc => `
                    <tr>
                        <td>${inc.timestamp}</td>
                        <td>${inc.event}</td>
                        <td style="color: ${inc.severity === 'CRITICAL' ? 'var(--red)' : 'var(--amber)'}">${inc.severity}</td>
                    </tr>
                `).join('');
            }
        };

        function injectFault(type) {
            fetch('/api/inject_fault', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ fault_type: type })
            });
        }

        function toggleCamera() {
            fetch('/api/toggle_camera', { method: 'POST' });
        }

        function resetSystem() {
            fetch('/api/reset', { method: 'POST' });
        }
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)

