#!/usr/bin/env python3
"""
Enhanced Race Engineer with Predictive Analytics
- Stores all lap times in CSV
- Analyzes tire degradation patterns
- Predicts future lap times based on wear
- Calculates optimal pit strategy
- Real-time fuel/tire management
"""

import csv
import os
import socket
import threading
import queue
import time
import pyttsx3
from pynput import keyboard
from f1_2020_telemetry.packets import unpack_udp_packet

# --- MAPS ---
COMPOUNDS = {16: "Soft", 17: "Medium", 18: "Hard", 7: "Intermediate", 8: "Wet", 
             9: "Wet", 10: "Wet", 11: "Super Soft", 12: "Ultra Soft", 13: "Hyper Soft"}
WEATHER = {0: "Clear", 1: "Light Cloud", 2: "Overcast", 3: "Light Rain", 4: "Heavy Rain", 5: "Storm"}


class DataManager:
    """Manages CSV storage and lap time analysis with tire degradation tracking."""
    def __init__(self, filename="race_history.csv"):
        self.filename = filename
        self.headers = ["Timestamp", "Lap", "S1", "S2", "S3", "Total", "Wear_Avg", 
                       "Fuel_Laps", "Wear_FL", "Wear_FR", "Wear_RL", "Wear_RR", "Compound"]
        
        # If the file doesn't exist, create it with headers
        if not os.path.exists(self.filename):
            with open(self.filename, 'w', newline='') as f:
                csv.writer(f).writerow(self.headers)

    def save_lap(self, data_dict):
        """Appends a single lap to the local CSV file."""
        with open(self.filename, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=self.headers)
            writer.writerow(data_dict)

    def get_lap_history(self):
        """Reads all saved laps from CSV and returns as list of dicts."""
        if not os.path.exists(self.filename):
            return []
        
        laps = []
        try:
            with open(self.filename, 'r', newline='') as f:
                reader = csv.DictReader(f)
                laps = list(reader)
        except Exception as e:
            print(f"[DATA ERROR] Reading CSV: {e}")
        
        return laps


class LapPredictor:
    """Analyzes lap time trends and predicts future performance based on tire degradation."""
    
    def __init__(self, data_manager):
        self.data_manager = data_manager
        self.baseline_lap = None  # First clean lap as reference
        self.degradation_rate = 0.0  # ms per lap per tire wear %
        self.lap_history = []

    def analyze_degradation(self):
        """
        Analyze stored lap times to calculate tire degradation impact.
        Returns: (baseline_lap_time, degradation_rate_per_wear_pct)
        """
        laps = self.data_manager.get_lap_history()
        
        if len(laps) < 3:
            return None, 0.0
        
        # Convert to numeric values
        processed = []
        for lap in laps:
            try:
                total_time = float(lap['Total'])
                wear_avg = float(lap['Wear_Avg'])
                if total_time > 0:
                    processed.append({'time': total_time, 'wear': wear_avg, 'lap': int(lap['Lap'])})
            except ValueError:
                continue
        
        if len(processed) < 3:
            return None, 0.0
        
        # Use first lap with low wear as baseline (typically fresh tires at start)
        baseline = min([p for p in processed if p['wear'] < 10], 
                      key=lambda x: x['time'], default=processed[0])
        self.baseline_lap = baseline['time']
        
        # Calculate degradation rate: how much each wear % adds to lap time
        # Simple linear regression approach
        if len(processed) > 2:
            time_deltas = []
            wear_deltas = []
            
            for i in range(1, len(processed)):
                time_delta = processed[i]['time'] - baseline['time']
                wear_delta = processed[i]['wear'] - baseline['wear']
                
                if wear_delta > 0:
                    time_deltas.append(time_delta)
                    wear_deltas.append(wear_delta)
            
            if wear_deltas and sum(wear_deltas) > 0:
                avg_degradation = sum(time_deltas) / sum(wear_deltas) if sum(wear_deltas) > 0 else 0
                self.degradation_rate = avg_degradation
        
        return self.baseline_lap, self.degradation_rate

    def predict_lap_time(self, current_wear_avg, fuel_laps_remaining=None):
        """
        Predict lap time based on tire wear AND fuel level.
        Heavier fuel = slower pace (heavy car).
        Returns: predicted_lap_time (in seconds)
        """
        if self.baseline_lap is None:
            self.analyze_degradation()
        
        if self.baseline_lap is None:
            return None
        
        # Tire degradation impact
        wear_impact = current_wear_avg * self.degradation_rate
        
        # Fuel impact: heavier fuel load = slower lap
        # ~1kg of fuel = ~0.03s per lap penalty
        fuel_impact = 0.0
        if fuel_laps_remaining and fuel_laps_remaining > 0:
            fuel_kg_estimate = fuel_laps_remaining * 1.8  # rough estimate: 1.8kg per lap of fuel
            fuel_impact = fuel_kg_estimate * 0.03
        
        predicted_time = self.baseline_lap + wear_impact + fuel_impact
        return predicted_time

    def estimate_pit_stop_time(self):
        """
        Estimates pit stop duration.
        Typical: 20-25 seconds (depends on tire change + fuel)
        """
        return 22.0  # conservative estimate


class PitStrategy:
    """Calculates optimal pit stop strategy based on fuel, tires, and lap predictions."""
    
    def __init__(self, lap_predictor):
        self.lap_predictor = lap_predictor
        self.pit_stop_time = 22.0  # pit stop duration in seconds

    def calculate_optimal_pit_lap(self, current_lap, total_laps, fuel_laps, tire_wear_avg, 
                                  car_ahead_gap, car_behind_gap, current_position):
        """
        Determines optimal pit strategy based on:
        - Fuel remaining (if low, advise lean mix - no pit)
        - Tire wear (critical if > 50% - must pit)
        - Undercut strategy (pit before car ahead to gain time)
        - Overcut strategy (stay out longer to gain tires advantage)
        
        Returns: (recommended_lap, strategy_reason, expected_gain_loss)
        """
        laps_remaining = total_laps - current_lap
        pit_stop_duration = self.pit_stop_time
        
        # FUEL MANAGEMENT: If fuel is critical, advise lean mix (no pit)
        if fuel_laps < laps_remaining:
            # Not enough fuel to finish - must lean out
            fuel_shortage = laps_remaining - fuel_laps
            return None, f"FUEL CRITICAL - Switch to LEAN fuel mix NOW. {fuel_shortage:.1f} laps short.", -0.5
        
        # TIRE CRITICAL: Must pit for tire change
        if tire_wear_avg > 50:
            return current_lap + 1, "TIRE CRITICAL - Must pit now", 0.0
        
        # If car ahead is close, consider UNDERCUT (pit now to gain advantage)
        if 0 < car_ahead_gap < 2.5 and current_position > 1:
            # Undercut works if we can build gap after pit
            # Time lost in pit vs time gained by fresh tires on next lap
            current_pace = self.lap_predictor.predict_lap_time(tire_wear_avg, fuel_laps)
            fresh_pace = self.lap_predictor.predict_lap_time(10, fuel_laps - 1)  # estimate fresh tire time
            
            pace_gain_per_lap = (current_pace - fresh_pace) if current_pace and fresh_pace else 0
            
            if pace_gain_per_lap > pit_stop_duration / 2:
                return current_lap + 1, f"UNDERCUT vs gap {car_ahead_gap:.2f}s", pace_gain_per_lap
        
        # Check OVERCUT strategy: if car behind is far, stay out and get fresh tires advantage
        if car_behind_gap > 5.0 and tire_wear_avg < 40:
            return current_lap + 4, "OVERCUT - build gap before stopping", 0.0
        
        # Default: pit when tires degrade past sweet zone
        if tire_wear_avg > 40:
            return current_lap + 2, "Pit window opening - tires degrading", 0.0
        
        # No fuel pit stop needed (can't refuel anyway) - focus only on tire management
        return None, "No pit needed yet - tires still in good condition", 0.0

    def get_pit_exit_position(self, pit_lap, current_position, current_gap_ahead, current_gap_behind):
        """
        Estimates position after pit stop.
        Rough calculation: losing time in pit = losing position
        
        Returns: estimated_position_after_pit
        """
        pit_time_loss = 22.0  # seconds
        
        # Assume cars are doing 100 kph average = ~27.8 m/s
        # In 22 seconds they cover ~611 meters
        avg_car_speed_ms = 27.8
        distance_lost = pit_time_loss * avg_car_speed_ms
        
        # If gap ahead is < distance lost, you'll lose position
        position_after_pit = current_position
        if current_gap_ahead * 1000 < distance_lost:  # gap in seconds -> rough meters
            position_after_pit += 1
        
        return position_after_pit


class RaceEngineer:
    """Main race engineer logic integrating all analysis features."""
    
    def __init__(self):
        # Audio Queue
        self.speech_queue = queue.Queue()
        self.tts_thread = threading.Thread(target=self._tts_worker, daemon=True)
        self.tts_thread.start()

        # Telemetry State Tracking
        self.state_lock = threading.Lock()
        self.space_held = False
        
        self.state = {
            'lap': 0, 'sector': 0, 'pos': 0,
            'fuel_laps': 0.0, 'wear': [0,0,0,0], 'tyre_compound': 'Unknown',
            'speed': 0, 'track_temp': 0, 'weather': 'Unknown',
            'drs_allowed': 0, 'is_raining': False,
            's1_time': 0.0, 's2_time': 0.0, 'last_lap': 0.0,
            'best_s1': 999.0, 'best_s2': 999.0, 'best_s3': 999.0,
            'overall_best_lap': 999.0, 'my_lap_dist': 0.0,
            'pit_window_open': False,
        }

        self.last_gap_alert = 0
        self.last_pit_alert = 0
        self.cars_data = {} # Tracking all 22 cars for gap and fastest lap checks

        # Initialize data management and predictive analytics
        self.data_manager = DataManager()
        self.lap_predictor = LapPredictor(self.data_manager)
        self.pit_strategy = PitStrategy(self.lap_predictor)
        
        # Start Telemetry & Sequence in background
        threading.Thread(target=self.run_startup_sequence, daemon=True).start()

    def _tts_worker(self):
        """Processes speech requests by creating fresh instances to avoid buffer hangs."""
        while True:
            text = self.speech_queue.get()
            if text is None: 
                break
            
            try:
                # Initialize fresh for every phrase to prevent 'silent' engine hangs
                engine = pyttsx3.init()
                engine.setProperty('rate', 190)
                
                print(f"[ENGINEER]: {text}")
                engine.say(text)
                engine.runAndWait()
                
                # Force cleanup of the instance
                del engine
                time.sleep(0.05)
            except Exception as e:
                print(f"[AUDIO ERROR]: {e}")
            finally:
                self.speech_queue.task_done()

    def speak(self, text):
        """Queue speech for TTS."""
        self.speech_queue.put(text)

    def format_diff(self, diff):
        """Formats the sector difference into the +/- 0.123 format."""
        if diff >= 900.0 or diff <= -900.0:
            return "Setting personal best."
        if diff > 0.001:
            return f"You are slower by {diff:.3f} seconds."
        elif diff < -0.001:
            return f"You are faster by {abs(diff):.3f} seconds."
        return "Matching your best time exactly."

    def run_startup_sequence(self):
        print("[SYSTEM]: Waiting for F1 2020 telemetry to initialize...")
        threading.Thread(target=self.process_telemetry, daemon=True).start()
        
        # Wait until critical data packets arrive
        connected = False
        while not connected:
            with self.state_lock:
                if self.state['lap'] > 0 and self.state['weather'] != 'Unknown' and self.state['tyre_compound'] != 'Unknown':
                    connected = True
            time.sleep(0.5)

        # Buffer to let other packets settle
        time.sleep(2)

        with self.state_lock:
            w = self.state['weather']
            tt = self.state['track_temp']
            tc = self.state['tyre_compound']
            pos = self.state['pos']
            fuel = self.state['fuel_laps']

        greeting = (f"Radio check. We are receiving telemetry. "
                    f"Weather is {w}. Track temperature is {tt} degrees. "
                    f"You are on the {tc} tyres. Current position P {pos}. "
                    f"You have {fuel:.1f} laps of fuel. "
                    f"I'll update you on sectors, pace, and pit strategy.")
        
        self.speak(greeting)

    def check_gaps(self, my_idx):
        """Analyzes distances to find cars within 1 second ahead or behind."""
        # Reduced gap alert frequency to 20 seconds to prevent audio queue clogging
        if time.time() - self.last_gap_alert < 20:
            return

        my_pos = self.state['pos']
        my_lap = self.state['lap']
        my_dist = self.state['my_lap_dist']
        
        # Convert km/h to m/s
        speed_ms = self.state['speed'] / 3.6
        if speed_ms < 10: 
            return

        for i, car in self.cars_data.items():
            if i == my_idx: 
                continue
            
            if car['lap'] == my_lap:
                if car['pos'] == my_pos - 1: # Car ahead
                    gap_m = car['dist'] - my_dist
                    if 0 < gap_m < (speed_ms * 1.0): 
                        self.speak("Car ahead is under a second away.")
                        self.last_gap_alert = time.time()
                
                elif car['pos'] == my_pos + 1: # Car behind
                    gap_m = my_dist - car['dist']
                    if 0 < gap_m < (speed_ms * 1.0):
                        self.speak("Car behind is under a second away.")
                        self.last_gap_alert = time.time()

    def analyze_pit_strategy(self, my_idx, total_laps):
        """
        Analyzes current situation and recommends pit strategy.
        Called periodically to give tactical advice.
        """
        if time.time() - self.last_pit_alert < 45:  # Alert every 45 seconds max
            return
        
        with self.state_lock:
            current_lap = self.state['lap']
            fuel_laps = self.state['fuel_laps']
            tire_wear = sum(self.state['wear']) / 4
            pos = self.state['pos']
        
        # Get gaps to cars ahead and behind
        my_car = self.cars_data.get(my_idx)
        if not my_car:
            return
        
        car_ahead_gap = 0.0
        car_behind_gap = 0.0
        
        for i, car in self.cars_data.items():
            if i == my_idx:
                continue
            if car['pos'] == pos - 1:
                car_ahead_gap = abs(my_car['dist'] - car['dist']) / 27.8  # rough seconds
            elif car['pos'] == pos + 1:
                car_behind_gap = abs(my_car['dist'] - car['dist']) / 27.8
        
        # Get pit strategy recommendation
        pit_lap, reason, gain = self.pit_strategy.calculate_optimal_pit_lap(
            current_lap, total_laps, fuel_laps, tire_wear,
            car_ahead_gap, car_behind_gap, pos
        )
        
        if pit_lap is not None:
            # Predict lap times
            current_predicted = self.lap_predictor.predict_lap_time(tire_wear, fuel_laps)
            fresh_predicted = self.lap_predictor.predict_lap_time(10, fuel_laps - 1)
            
            if current_predicted and fresh_predicted:
                time_gain = current_predicted - fresh_predicted
                pit_exit_pos = self.pit_strategy.get_pit_exit_position(pit_lap, pos, car_ahead_gap, car_behind_gap)
                
                message = (f"Pit strategy: {reason}. "
                          f"Box at lap {pit_lap}. Tire wear {tire_wear:.0f}%. "
                          f"Fuel {fuel_laps:.1f} laps. "
                          f"Expected exit position P {pit_exit_pos}. "
                          f"Pace gain after pit: {time_gain:.2f}s per lap.")
                self.speak(message)
                self.last_pit_alert = time.time()

    def process_telemetry(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1)
        try:
            sock.bind(("0.0.0.0", 20777))
            print("[SYSTEM]: UDP Stream Connected.")
        except Exception as e:
            print(f"[ERROR]: UDP Bind Error: {e}")
            return

        idx = None
        total_laps = 0

        while True:
            try:
                data, _ = sock.recvfrom(4096)
                packet = unpack_udp_packet(data)
                p_id = packet.header.packetId
                idx = packet.header.playerCarIndex

                with self.state_lock:
                    if p_id == 1: # Session
                        weather_val = packet.weather
                        self.state['weather'] = WEATHER.get(weather_val, "Unknown")
                        self.state['track_temp'] = packet.trackTemperature
                        total_laps = packet.totalLaps
                        
                        if weather_val >= 3 and not self.state['is_raining']:
                            self.speak("It is raining. DRS disabled.")
                            self.state['is_raining'] = True
                        elif weather_val < 3 and self.state['is_raining']:
                            self.speak("Rain is clearing.")
                            self.state['is_raining'] = False

                    elif p_id == 2: # Lap Data
                        for i in range(len(packet.lapData)):
                            ld = packet.lapData[i]
                            self.cars_data[i] = {
                                'pos': ld.carPosition, 'lap': ld.currentLapNum,
                                'dist': ld.lapDistance, 'best_lap': ld.bestLapTime
                            }
                            if ld.bestLapTime > 0 and ld.bestLapTime < self.state['overall_best_lap']:
                                self.state['overall_best_lap'] = ld.bestLapTime
                                if i != idx and self.state['lap'] > 1:
                                    self.speak("Someone set the fastest lap.")

                        my_lap = packet.lapData[idx]
                        self.state['my_lap_dist'] = my_lap.lapDistance
                        self.state['pos'] = my_lap.carPosition
                        
                        if self.state['lap'] == 0: 
                            self.state['lap'] = my_lap.currentLapNum

                        if my_lap.sector != self.state['sector']:
                            if my_lap.sector == 1: # S1 end
                                s1 = my_lap.sector1TimeInMS / 1000.0
                                if s1 > 0:
                                    diff = s1 - self.state['best_s1']
                                    self.speak(f"Sector 1. {self.format_diff(diff)}")
                                    if s1 < self.state['best_s1']: 
                                        self.state['best_s1'] = s1
                                    self.state['s1_time'] = s1
                            elif my_lap.sector == 2: # S2 end
                                s2 = my_lap.sector2TimeInMS / 1000.0
                                if s2 > 0:
                                    diff = s2 - self.state['best_s2']
                                    self.speak(f"Sector 2. {self.format_diff(diff)}")
                                    if s2 < self.state['best_s2']: 
                                        self.state['best_s2'] = s2
                                    self.state['s2_time'] = s2
                            self.state['sector'] = my_lap.sector

                            if my_lap.currentLapNum > self.state['lap'] and self.state['lap'] > 0:
                                last_lap = my_lap.lastLapTime
                                self.state['last_lap'] = last_lap
                                
                                # Calculate Sector 3
                                s3 = last_lap - (self.state['s1_time'] + self.state['s2_time'])
                                
                                wear_avg = sum(self.state['wear']) / 4
                                
                                # Prepare the data packet for the CSV
                                lap_payload = {
                                    "Timestamp": time.strftime("%H:%M:%S"),
                                    "Lap": self.state['lap'],
                                    "S1": f"{self.state['s1_time']:.3f}",
                                    "S2": f"{self.state['s2_time']:.3f}",
                                    "S3": f"{s3:.3f}",
                                    "Total": f"{last_lap:.3f}",
                                    "Wear_Avg": f"{wear_avg:.1f}",
                                    "Fuel_Laps": f"{self.state['fuel_laps']:.2f}",
                                    "Wear_FL": self.state['wear'][0],
                                    "Wear_FR": self.state['wear'][1],
                                    "Wear_RL": self.state['wear'][2],
                                    "Wear_RR": self.state['wear'][3],
                                    "Compound": self.state['tyre_compound']
                                }
                                
                                # SAVE IT TO DISK
                                self.data_manager.save_lap(lap_payload)
                                
                                # Analyze degradation after every 3rd lap
                                if self.state['lap'] % 3 == 0:
                                    baseline, degrade_rate = self.lap_predictor.analyze_degradation()
                                    if baseline:
                                        predicted = self.lap_predictor.predict_lap_time(wear_avg, self.state['fuel_laps'])
                                        if predicted:
                                            pace_loss = predicted - baseline
                                            self.speak(f"Lap recorded. Pace loss due to wear: {pace_loss:.3f} seconds.")
                                
                                # Notify the driver
                                if self.state['overall_best_lap'] < 999.0:
                                    gap = last_lap - self.state['overall_best_lap']
                                    self.speak(f"Lap complete. Time {last_lap:.3f}. Gap to fastest is {gap:.3f}")
                                
                                self.state['lap'] = my_lap.currentLapNum
                                
                                # Check pit strategy periodically
                                threading.Thread(target=self.analyze_pit_strategy, args=(idx, total_laps), daemon=True).start()

                    elif p_id == 7: # Status
                        p = packet.carStatusData[idx]
                        self.state['fuel_laps'] = p.fuelRemainingLaps
                        self.state['wear'] = list(p.tyresWear)
                        self.state['tyre_compound'] = COMPOUNDS.get(p.actualTyreCompound, "Unknown")
                        if p.drsAllowed == 1 and self.state['drs_allowed'] == 0:
                            self.speak("DRS available.")
                        self.state['drs_allowed'] = p.drsAllowed

                    elif p_id == 6: # Telemetry
                        self.state['speed'] = packet.carTelemetryData[idx].speed

                    self.check_gaps(idx)
            except Exception:
                continue

    def on_press(self, key):
        if key == keyboard.Key.space and not self.space_held:
            self.space_held = True
            with self.state_lock:
                pos = self.state['pos']
                lap = self.state['lap']
                wear = sum(self.state['wear']) / 4
                fuel = self.state['fuel_laps']
            
            # Get predictions
            predicted_lap = self.lap_predictor.predict_lap_time(wear, fuel)
            
            message = f"Update. P {pos}, Lap {lap}. Tyres at {wear:.1f} percent. Fuel {fuel:.1f} laps. "
            if predicted_lap:
                message += f"Predicted lap time: {predicted_lap:.3f} seconds."
            
            self.speak(message)

    def on_release(self, key):
        if key == keyboard.Key.space:
            self.space_held = False


if __name__ == "__main__":
    engineer = RaceEngineer()
    print("[READY] Script is running. Press SPACE for updates. Press CTRL+C to exit.")
    with keyboard.Listener(on_press=engineer.on_press, on_release=engineer.on_release) as listener:
        listener.join()