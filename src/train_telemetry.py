# src/train_telemetry.py
import pandas as pd
import pickle
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report

def train_sensor_model():
    df = pd.read_csv("data/conveyor_telemetry.csv")
    
    features = ["speed_mps", "load_tph", "tension_kn", "vibration_rms", "bearing_temp_c"]
    X = df[features]
    y = df["health_state"]
    
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    
    clf = RandomForestClassifier(n_estimators=100, random_state=42)
    clf.fit(X_train, y_train)
    
    y_pred = clf.predict(X_test)
    print("Telemetry Model Classification Report:")
    print(classification_report(y_test, y_pred))
    
    with open("models/telemetry_model.pkl", "wb") as f:
        pickle.dump(clf, f)
    print("Model saved to models/telemetry_model.pkl")

if __name__ == "__main__":
    train_sensor_model()