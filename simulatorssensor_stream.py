# simulators/sensor_stream.py
import numpy as np
import pandas as pd
import time

def generate_synthetic_telemetry(num_samples=5000):
    """Generates synthetic conveyor telemetry covering normal, degradation, and rupture stages."""
    np.random.seed(42)
    timestamps = pd.date_range(start="2026-09-01", periods=num_samples, freq="1s")
    
    # Baseline normal operations
    belt_speed = np.random.normal(3.5, 0.1, num_samples)          # m/s
    load_tonnage = np.random.normal(1200, 50, num_samples)       # Tons/hr
    tension_kn = np.random.normal(45.0, 2.0, num_samples)        # kN
    vibration_rms = np.random.normal(2.5, 0.3, num_samples)      # mm/s
    bearing_temp = np.random.normal(55.0, 3.0, num_samples)      # Celsius
    
    # Introduce progressive degradation in the last 20% of samples
    degrade_start = int(num_samples * 0.8)
    degrade_len = num_samples - degrade_start
    
    tension_kn[degrade_start:] += np.linspace(0, 35, degrade_len) + np.random.normal(0, 2, degrade_len)
    vibration_rms[degrade_start:] += np.linspace(0, 8.0, degrade_len)
    bearing_temp[degrade_start:] += np.linspace(0, 40.0, degrade_len)
    
    # Labels: 0 = Normal, 1 = Warning, 2 = Critical Rupture Risk
    labels = np.zeros(num_samples, dtype=int)
    labels[degrade_start : degrade_start + int(degrade_len * 0.6)] = 1
    labels[degrade_start + int(degrade_len * 0.6) :] = 2
    
    df = pd.DataFrame({
        "timestamp": timestamps,
        "speed_mps": belt_speed,
        "load_tph": load_tonnage,
        "tension_kn": tension_kn,
        "vibration_rms": vibration_rms,
        "bearing_temp_c": bearing_temp,
        "health_state": labels
    })
    return df

if __name__ == "__main__":
    df = generate_synthetic_telemetry()
    df.to_csv("data/conveyor_telemetry.csv", index=False)
    print("Synthetic telemetry dataset saved to data/conveyor_telemetry.csv")