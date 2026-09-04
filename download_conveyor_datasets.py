# download_conveyor_datasets.py
# ==============================================================================
# TEAM AYASK · CONVEYOR BELT DEFECT DATASET MANAGER & TRAINER
# SIH 26008 · MULTI-DATASET DOWNLOADER, VALIDATOR & YOLOV8 TRAINER
# ==============================================================================

import os
import sys
import yaml
import shutil
import argparse
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "src" / "data" / "Conveyor"
MODELS_DIR = BASE_DIR / "models" / "belt_defect_yolov8" / "weights"

def check_dataset():
    """Inspect and report the status of the local conveyor belt dataset."""
    print("=" * 72)
    print("  TEAM AYASK · CONVEYOR BELT DATASET STATUS")
    print("=" * 72)

    data_yaml_path = DATA_DIR / "data.yaml"
    if not data_yaml_path.exists():
        print(f"[!] data.yaml not found at: {data_yaml_path}")
        return False

    with open(data_yaml_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    train_dir = DATA_DIR / "train" / "images"
    val_dir = DATA_DIR / "valid" / "images"
    test_dir = DATA_DIR / "test" / "images"

    n_train = len(list(train_dir.glob("*.jpg")) + list(train_dir.glob("*.png"))) if train_dir.exists() else 0
    n_val = len(list(val_dir.glob("*.jpg")) + list(val_dir.glob("*.png"))) if val_dir.exists() else 0
    n_test = len(list(test_dir.glob("*.jpg")) + list(test_dir.glob("*.png"))) if test_dir.exists() else 0

    classes = config.get("names", [])

    print(f"\nDataset Location:  {DATA_DIR}")
    print(f"Training Images:   {n_train} frames")
    print(f"Validation Images: {n_val} frames")
    print(f"Test Images:       {n_test} frames")
    print(f"Total Dataset:     {n_train + n_val + n_test} annotated conveyor images")
    print(f"\nDefect Classes ({len(classes)} classes):")
    for i, name in enumerate(classes):
        print(f"  [{i}] {name}")

    best_pt = MODELS_DIR / "best.pt"
    if best_pt.exists():
        print(f"\nTrained Model Weights: {best_pt} ({best_pt.stat().st_size / (1024*1024):.2f} MB)")
    else:
        print("\nTrained Model Weights: NOT YET COMPILED (Run with --train to build)")

    print("=" * 72)
    return True

def download_roboflow(api_key, workspace="fyp-lnegm", project="conveyor-belt-x0o7y", version=15):
    """Download additional conveyor belt defect datasets from Roboflow Universe."""
    try:
        from roboflow import Roboflow
    except ImportError:
        print("[!] Installing roboflow package...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "roboflow"])
        from roboflow import Roboflow

    print(f"\n[*] Connecting to Roboflow Universe: {workspace}/{project}/v{version}...")
    rf = Roboflow(api_key=api_key)
    proj = rf.workspace(workspace).project(project)
    dataset = proj.version(version).download("yolov8", location=str(DATA_DIR))
    print(f"[OK] Dataset successfully downloaded to: {DATA_DIR}")

def train_model(epochs=10, imgsz=320, batch=16):
    """Train or fine-tune YOLOv8 on the conveyor belt defect dataset."""
    from ultralytics import YOLO

    data_yaml_path = DATA_DIR / "data.yaml"
    if not data_yaml_path.exists():
        print(f"[!] Cannot train: data.yaml not found at {data_yaml_path}")
        return

    # Fix paths in data.yaml to absolute
    with open(data_yaml_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["path"] = str(DATA_DIR).replace("\\", "/")
    cfg["train"] = "train/images"
    cfg["val"] = "valid/images"
    cfg["test"] = "test/images"
    with open(data_yaml_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f)

    print("\n" + "=" * 72)
    print(f"  STARTING YOLOV8 CONVEYOR DEFECT MODEL TRAINING ({epochs} EPOCHS)")
    print("=" * 72)

    base_weights = MODELS_DIR / "best.pt"
    init_model = str(base_weights) if base_weights.exists() else "yolov8n.pt"

    model = YOLO(init_model)
    results = model.train(
        data=str(data_yaml_path),
        epochs=epochs,
        imgsz=imgsz,
        batch=batch,
        workers=2,
        project=str(BASE_DIR / "models"),
        name="belt_defect_yolov8",
        exist_ok=True
    )

    # Copy best.pt to primary location
    out_best = BASE_DIR / "runs" / "detect" / "models" / "belt_defect_yolov8" / "weights" / "best.pt"
    if out_best.exists():
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out_best, MODELS_DIR / "best.pt")
        print(f"\n[OK] Model successfully trained and saved to: {MODELS_DIR / 'best.pt'}")

def main():
    parser = argparse.ArgumentParser(description="Team AYASK Conveyor Dataset Manager")
    parser.add_argument("--status", action="store_true", default=True, help="Check current dataset status")
    parser.add_argument("--train", action="store_true", help="Train YOLOv8 on conveyor dataset")
    parser.add_argument("--epochs", type=int, default=10, help="Number of training epochs")
    parser.add_argument("--download-roboflow", type=str, metavar="API_KEY", help="Download dataset using Roboflow API key")
    parser.add_argument("--workspace", type=str, default="fyp-lnegm", help="Roboflow workspace")
    parser.add_argument("--project", type=str, default="conveyor-belt-x0o7y", help="Roboflow project")
    parser.add_argument("--version", type=int, default=15, help="Roboflow dataset version")

    args = parser.parse_args()

    if args.download_roboflow:
        download_roboflow(args.download_roboflow, args.workspace, args.project, args.version)

    if args.train:
        train_model(epochs=args.epochs)
    else:
        check_dataset()

if __name__ == "__main__":
    main()
