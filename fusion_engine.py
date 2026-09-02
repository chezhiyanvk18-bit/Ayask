# src/fusion_engine.py

class FusionEngine:
    def __init__(self, telemetry_model):
        self.telemetry_model = telemetry_model

    def evaluate(self, vision_detections, sensor_data_dict):
        """
        Combines visual defect severity with IoT sensor telemetry.
        Returns: Belt Health Index (BHI 0-100), Status Level, Emergency Trip Boolean
        """
        # 1. Vision Penalty
        vision_penalty = 0.0
        critical_visual_defect = False
        
        for det in vision_detections:
            class_name = det.get("class", "")
            confidence = det.get("confidence", 0.0)
            
            if class_name in ["longitudinal_tear", "splice_damage"] and confidence > 0.65:
                vision_penalty += 50.0 * confidence
                critical_visual_defect = True
            elif class_name in ["edge_wear", "surface_crack"]:
                vision_penalty += 20.0 * confidence

        # 2. Sensor Telemetry Score
        features = [[
            sensor_data_dict["speed_mps"],
            sensor_data_dict["load_tph"],
            sensor_data_dict["tension_kn"],
            sensor_data_dict["vibration_rms"],
            sensor_data_dict["bearing_temp_c"]
        ]]
        sensor_pred_state = self.telemetry_model.predict(features)[0]
        
        sensor_penalty = 0.0
        if sensor_pred_state == 1:
            sensor_penalty = 25.0
        elif sensor_pred_state == 2:
            sensor_penalty = 55.0

        # 3. Calculate Composite Belt Health Index (BHI)
        bhi = max(0.0, min(100.0, 100.0 - (vision_penalty + sensor_penalty)))

        # 4. Determine Action Level & PLC Interlock
        emergency_trip = False
        if bhi < 40.0 or critical_visual_defect or sensor_pred_state == 2:
            status = "LEVEL 3: CRITICAL (EMERGENCY TRIP ACTIVATED)"
            emergency_trip = True
        elif bhi < 75.0 or sensor_pred_state == 1:
            status = "LEVEL 2: WARNING (SCHEDULE MAINTENANCE)"
        else:
            status = "LEVEL 1: NORMAL (OPTIMAL HEALTH)"

        return {
            "bhi": round(bhi, 1),
            "status": status,
            "emergency_trip": emergency_trip,
            "sensor_state": sensor_pred_state
        }