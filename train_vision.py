# src/train_vision.py
import torch
from ultralytics import YOLO

def train():
    print(f"CUDA Available: {torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"Using GPU: {torch.cuda.get_device_name(0)}")
        
    # Load lightweight YOLOv8 Nano model
    model = YOLO("yolov8n.pt")
    
    # Train on your local GPU (device=0)
    model.train(
        data="data/dataset.yaml",
        epochs=40,
        imgsz=640,
        batch=16,
        device=0 if torch.cuda.is_available() else "cpu",
        project="models",
        name="belt_defect_yolov8"
    )
    print("Training complete. Best weights saved in models/belt_defect_yolov8/weights/best.pt")

if __name__ == "__main__":
    train()