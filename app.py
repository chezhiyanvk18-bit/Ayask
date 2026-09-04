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
import platform
import os
import tempfile

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

# Enhanced camera initialization function
def init_camera(camera_index=0):
    """Initialize camera with cross-platform compatibility"""
    cap = cv2.VideoCapture(camera_index)
    
    # Set camera properties for better compatibility
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_FPS, 30)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # Reduce buffer to avoid lag
    
    # Platform-specific optimizations
    system = platform.system()
    if system == "Windows":
        cap.set(cv2.CAP_PROP_BACKEND, cv2.CAP_DSHOW)  # DirectShow backend
    elif system == "Darwin":  # macOS
        cap.set(cv2.CAP_PROP_BACKEND, cv2.CAP_AVFOUNDATION)
    elif system == "Linux":
        cap.set(cv2.CAP_PROP_BACKEND, cv2.CAP_V4L2)  # V4L2 backend
    
    # Verify camera opened successfully
    if not cap.isOpened():
        return None
    
    # Test if camera can grab a frame
    ret, _ = cap.retrieve()
    if not ret:
        cap.release()
        return None
    
    return cap

# Try multiple camera indices
def find_working_camera():
    """Attempt to find a working camera device"""
    for i in range(5):  # Try camera indices 0-4
        cap = init_camera(i)
        if cap is not None:
            return cap
    
    return None

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
    camera_status = st.empty()

# Input Mode Selection
st.divider()
st.subheader("🎥 Video Input Options")

input_mode = st.radio(
    "Select video source:",
    options=["📁 Upload Video File", "🎬 Use Sample Video", "📷 Use Webcam"],
    horizontal=True
)

video_file = None
video_path = None

if input_mode == "📁 Upload Video File":
    st.info("💡 Upload a conveyor belt video (MP4, AVI, MOV) for analysis")
    uploaded_file = st.file_uploader("📤 Upload a video file", type=["mp4", "avi", "mov", "mkv"])
    
    if uploaded_file is not None:
        # Save uploaded file to temporary location
        temp_dir = tempfile.gettempdir()
        video_path = os.path.join(temp_dir, "uploaded_video.mp4")
        
        with open(video_path, "wb") as f:
            f.write(uploaded_file.getbuffer())
        
        st.success(f"✅ Video uploaded successfully: {uploaded_file.name}")
        video_file = video_path

elif input_mode == "🎬 Use Sample Video":
    st.info("💡 Using the default sample conveyor belt video")
    # Check if sample video exists
    if os.path.exists("data/videos/conveyor_sample.mp4"):
        video_path = "data/videos/conveyor_sample.mp4"
        st.success("✅ Sample video found and ready to use")
        video_file = video_path
    else:
        st.warning("⚠️ Sample video not found at 'data/videos/conveyor_sample.mp4'")
        st.info("Please upload a video file instead")

elif input_mode == "📷 Use Webcam":
    st.info("💡 Using local webcam (only works when running locally, not on Vercel)")
    if os.environ.get("VERCEL"):
        st.error("❌ Webcam access is not available on Vercel (serverless platform). Please use uploaded video or sample video instead.")
    else:
        st.success("✅ Webcam mode enabled")

# Simulation Loop
if st.button("▶️ Start Monitoring Simulation", type="primary"):
    # Load simulated sensor log
    try:
        telemetry_df = pd.read_csv("data/conveyor_telemetry.csv")
    except FileNotFoundError:
        st.error("❌ Telemetry data file not found: data/conveyor_telemetry.csv")
        st.stop()
    
    cap = None
    camera_available = False
    
    # Initialize video source based on input mode
    if input_mode == "📷 Use Webcam":
        if not os.environ.get("VERCEL"):
            cap = find_working_camera()
            camera_available = cap is not None
            if camera_available:
                camera_status.success("✅ Live camera feed active")
            else:
                camera_status.warning("⚠️ Using simulated feed - camera not available")
        else:
            camera_status.error("❌ Webcam not available on Vercel - use uploaded video instead")
    else:
        # Use video file
        if video_file:
            if os.path.exists(video_file):
                cap = cv2.VideoCapture(video_file)
                if cap.isOpened():
                    camera_available = True
                    camera_status.success(f"✅ Video file loaded: {os.path.basename(video_file)}")
                else:
                    camera_status.error("❌ Failed to open video file")
            else:
                camera_status.error("❌ Video file not found")
        else:
            camera_status.warning("⚠️ No video file selected - using simulated feed")
    
    # Check GPU device
    device_id = 0 if torch.cuda.is_available() else "cpu"
    
    frame_count = 0
    
    try:
        for idx, row in telemetry_df.iloc[::20].iterrows():
            frame = None
            
            if camera_available and cap is not None:
                ret = False
                # Try multiple attempts to grab frame
                for attempt in range(3):
                    ret, frame = cap.read()
                    if ret:
                        break
                    time.sleep(0.05)
                
                # If video ends, loop back to start
                if not ret or frame is None:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ret, frame = cap.read()
                    if not ret:
                        frame = None
            
            # Fallback to simulated frame if camera fails
            if frame is None:
                frame = np.zeros((480, 640, 3), dtype=np.uint8)
                cv2.putText(frame, "SIMULATED CONVEYOR FEED", (80, 240),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 255), 2)
                cv2.putText(frame, "(No Video Source Available)", (120, 300),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 165, 255), 1)
            
            # Resize frame for consistency
            frame = cv2.resize(frame, (640, 480))
            
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
                
            frame_count += 1
            time.sleep(0.1)
    
    except Exception as e:
        st.error(f"❌ Error during monitoring: {str(e)}")
        import traceback
        st.error(traceback.format_exc())
    
    finally:
        if cap is not None:
            cap.release()
        st.success(f"✅ Monitoring complete. Processed {frame_count} frames.")

# Sidebar information
with st.sidebar:
    st.subheader("ℹ️ System Information")
    st.write(f"**Platform:** {platform.system()}")
    st.write(f"**Python Version:** {platform.python_version()}")
    st.write(f"**GPU Available:** {'Yes (CUDA)' if torch.cuda.is_available() else 'No (CPU Mode)'}")
    st.write(f"**Deployment:** {'Vercel (Cloud)' if os.environ.get('VERCEL') else 'Local'}")
    
    st.divider()
    st.subheader("📋 Instructions")
    st.markdown("""
    1. **Select Video Source**: Choose between upload, sample, or webcam
    2. **Upload or Select**: Provide your video file
    3. **Start Monitoring**: Click the button to begin analysis
    4. **View Results**: Monitor belt health in real-time
    
    **Note:** Webcam only works locally. Use video files on Vercel.
    """)
