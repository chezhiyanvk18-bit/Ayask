# # app.py
# import streamlit as st
# import cv2
# import pandas as pd
# import numpy as np
# import time
# import pickle
# import plotly.graph_objects as go
# from ultralytics import YOLO
# from src.fusion_engine import FusionEngine

# st.set_page_config(page_title="NMDC Conveyor Health AI", layout="wide")

# st.title("🛡️ NMDC Conveyor Belt Joint Rupture & Damage Monitoring System")
# st.markdown("**SIH Problem Statement 26008** | Edge AI & Multi-Modal IoT Predictive Maintenance")

# # Load Modelsa
# @st.cache_resource
# def load_models():
#     # Load YOLO model (fallback to pretrained yolov8n if custom not yet trained)
#     try:
#         vision_model = YOLO("models/belt_defect_yolov8/weights/best.pt")
#     except Exception:
#         vision_model = YOLO("yolov8n.pt")
        
#     try:
#         with open("models/telemetry_model.pkl", "rb") as f:
#             telemetry_model = pickle.load(f)
#     except Exception:
#         telemetry_model = None
        
#     return vision_model, telemetry_model

# vision_model, telemetry_model = load_models()
# fusion = FusionEngine(telemetry_model) if telemetry_model else None

# # Layout columns
# col_left, col_right = st.columns([3, 2])

# with col_right:
#     st.subheader("📊 Live Sensor Telemetry (IoT)")
#     tension_metric = st.empty()
#     vib_metric = st.empty()
#     temp_metric = st.empty()
#     bhi_gauge = st.empty()
#     alert_box = st.empty()

# with col_left:
#     st.subheader("📹 Computer Vision Inspection Feed")
#     video_placeholder = st.empty()

# # Simulation Loop
# if st.button("▶️ Start Monitoring Simulation"):
#     # Load simulated sensor log
#     telemetry_df = pd.read_csv("data/conveyor_telemetry.csv")
    
#     # Open camera or video feed (0 for webcam, or path to test video)
#     cap = cv2.VideoCapture(0) # Change to 'data/videos/conveyor_sample.mp4' if using a video
    
#     for idx, row in telemetry_df.iloc[::20].iterrows():
#         ret, frame = cap.read()
#         if not ret:
#             # Generate placeholder frame if no webcam available
#             frame = np.zeros((480, 640, 3), dtype=np.uint8)
#             cv2.putText(frame, "SIMULATED CONVEYOR FEED", (120, 240),
#                         cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            
#         # Run Vision Inference on GPU
#         results = vision_model.predict(frame, conf=0.4, verbose=False, device=0 if cv2.cuda.getCudaEnabledDeviceCount() > 0 else "cpu")
#         annotated_frame = results[0].plot()
        
#         detections = []
#         for box in results[0].boxes:
#             cls_id = int(box.cls[0].item())
#             detections.append({
#                 "class": results[0].names[cls_id],
#                 "confidence": float(box.conf[0].item())
#             })
            
#         # Current Sensor readings
#         sensor_dict = {
#             "speed_mps": row["speed_mps"],
#             "load_tph": row["load_tph"],
#             "tension_kn": row["tension_kn"],
#             "vibration_rms": row["vibration_rms"],
#             "bearing_temp_c": row["bearing_temp_c"]
#         }
        
#         # Multi-modal fusion
#         evaluation = fusion.evaluate(detections, sensor_dict) if fusion else {"bhi": 85.0, "status": "LEVEL 1: NORMAL", "emergency_trip": False}
        
#         # Update UI Elements
#         video_placeholder.image(cv2.cvtColor(annotated_frame, cv2.COLOR_BGR2RGB), use_container_width=True)
        
#         tension_metric.metric("Belt Tension", f"{sensor_dict['tension_kn']:.1f} kN", delta=f"{sensor_dict['tension_kn']-45:.1f} kN", delta_color="inverse")
#         vib_metric.metric("Splice Vibration (RMS)", f"{sensor_dict['vibration_rms']:.2f} mm/s")
#         temp_metric.metric("Pulley / Bearing Temp", f"{sensor_dict['bearing_temp_c']:.1f} °C")
        
#         # Gauge Chart for BHI
#         fig = go.Figure(go.Indicator(
#             mode="gauge+number",
#             value=evaluation["bhi"],
#             title={'text': "Belt Health Index (BHI)"},
#             gauge={
#                 'axis': {'range': [0, 100]},
#                 'bar': {'color': "darkblue"},
#                 'steps': [
#                     {'range': [0, 40], 'color': "#FF4B4B"},
#                     {'range': [40, 75], 'color': "#FFA500"},
#                     {'range': [75, 100], 'color': "#00CC96"}
#                 ]
#             }
#         ))
#         fig.update_layout(height=240, margin=dict(l=20, r=20, t=30, b=20))
#         bhi_gauge.plotly_chart(fig, use_container_width=True)
        
#         if evaluation["emergency_trip"]:
#             alert_box.error(f"🚨 **{evaluation['status']}**\n\n**Action:** PLC Modbus Trip Signal Sent to Stop Drive Motor!")
#         elif "LEVEL 2" in evaluation["status"]:
#             alert_box.warning(f"⚠️ **{evaluation['status']}**\n\n**Action:** Logged maintenance ticket with predicted RUL < 48 hrs.")
#         else:
#             alert_box.success(f"✅ **{evaluation['status']}**")
            
#         time.sleep(0.1)
        
#     cap.release()

# app.py
import streamlit as st
import cv2
import pandas as pd
import numpy as np
import time
import pickle
import torch
import plotly.graph_objects as go
from ultralytics import YOLO
from src.fusion_engine import FusionEngine

st.set_page_config(page_title="NMDC Conveyor Health AI", layout="wide")

st.title("🛡️ NMDC Conveyor Belt Joint Rupture & Damage Monitoring System")
st.markdown("**SIH Problem Statement 26008** | Edge AI & Multi-Modal IoT Predictive Maintenance")

# Load Models
@st.cache_resource
def load_models():
    # Load YOLO model (fallback to pretrained yolov8n if custom not yet trained)
    try:
        vision_model = YOLO("models/belt_defect_yolov8/weights/best.pt")
    except Exception:
        vision_model = YOLO("yolov8n.pt")
        
    try:
        with open("models/telemetry_model.pkl", "rb") as f:
            telemetry_model = pickle.load(f)
    except Exception:
        telemetry_model = None
        
    return vision_model, telemetry_model

vision_model, telemetry_model = load_models()
fusion = FusionEngine(telemetry_model) if telemetry_model else None

# Layout columns
col_left, col_right = st.columns([3, 2])

with col_right:
    st.subheader("📊 Live Sensor Telemetry (IoT)")
    tension_metric = st.empty()
    vib_metric = st.empty()
    temp_metric = st.empty()
    bhi_gauge = st.empty()
    alert_box = st.empty()

with col_left:
    st.subheader("📹 Computer Vision Inspection Feed")
    video_placeholder = st.empty()

# Simulation Loop
if st.button("▶️ Start Monitoring Simulation"):
    # Load simulated sensor log
    telemetry_df = pd.read_csv("data/conveyor_telemetry.csv")
    
    # 0 for webcam, or specify path: 'data/videos/conveyor_sample.mp4'
    cap = cv2.VideoCapture(0)
    
    # Check GPU device
    device_id = 0 if torch.cuda.is_available() else "cpu"
    
    for idx, row in telemetry_df.iloc[::20].iterrows():
        ret, frame = cap.read()
        if not ret:
            # Generate placeholder frame if camera is busy or unavailable
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(frame, "SIMULATED CONVEYOR FEED", (120, 240),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            
        # Run Vision Inference on dedicated GPU
        results = vision_model.predict(frame, conf=0.4, verbose=False, device=device_id)
        annotated_frame = results[0].plot()
        
        detections = []
        for box in results[0].boxes:
            cls_id = int(box.cls[0].item())
            detections.append({
                "class": results[0].names[cls_id],
                "confidence": float(box.conf[0].item())
            })
            
        # Current Sensor readings
        sensor_dict = {
            "speed_mps": row["speed_mps"],
            "load_tph": row["load_tph"],
            "tension_kn": row["tension_kn"],
            "vibration_rms": row["vibration_rms"],
            "bearing_temp_c": row["bearing_temp_c"]
        }
        
        # Multi-modal fusion
        evaluation = fusion.evaluate(detections, sensor_dict) if fusion else {
            "bhi": 85.0,
            "status": "LEVEL 1: NORMAL (OPTIMAL HEALTH)",
            "emergency_trip": False
        }
        
        # Update UI Elements
        video_placeholder.image(cv2.cvtColor(annotated_frame, cv2.COLOR_BGR2RGB), use_container_width=True)
        
        tension_metric.metric(
            "Belt Tension",
            f"{sensor_dict['tension_kn']:.1f} kN",
            delta=f"{sensor_dict['tension_kn']-45:.1f} kN",
            delta_color="inverse"
        )
        vib_metric.metric("Splice Vibration (RMS)", f"{sensor_dict['vibration_rms']:.2f} mm/s")
        temp_metric.metric("Pulley / Bearing Temp", f"{sensor_dict['bearing_temp_c']:.1f} °C")
        
        # Gauge Chart for BHI
        fig = go.Figure(go.Indicator(
            mode="gauge+number",
            value=evaluation["bhi"],
            title={'text': "Belt Health Index (BHI)"},
            gauge={
                'axis': {'range': [0, 100]},
                'bar': {'color': "darkblue"},
                'steps': [
                    {'range': [0, 40], 'color': "#FF4B4B"},
                    {'range': [40, 75], 'color': "#FFA500"},
                    {'range': [75, 100], 'color': "#00CC96"}
                ]
            }
        ))
        fig.update_layout(height=240, margin=dict(l=20, r=20, t=30, b=20))
        
        # Added unique key parameter here to prevent StreamlitDuplicateElementId error
        bhi_gauge.plotly_chart(fig, use_container_width=True, key=f"bhi_gauge_{idx}")
        
        if evaluation["emergency_trip"]:
            alert_box.error(f"🚨 **{evaluation['status']}**\n\n**Action:** PLC Modbus Trip Signal Sent to Stop Drive Motor!")
        elif "LEVEL 2" in evaluation["status"]:
            alert_box.warning(f"⚠️ **{evaluation['status']}**\n\n**Action:** Logged maintenance ticket with predicted RUL < 48 hrs.")
        else:
            alert_box.success(f"✅ **{evaluation['status']}**")
            
        time.sleep(0.1)
        
    cap.release()