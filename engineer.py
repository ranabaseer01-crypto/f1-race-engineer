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

class LogicEngineer:
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
            'overall_best_lap': 999.0, 'my_lap_dist': 0.0
        }

        self.last_gap_alert = 0
        self.cars_data = {} # Tracking all 22 cars for gap and fastest lap checks

        # Start Telemetry & Sequence in background
        threading.Thread(target=self.run_startup_sequence, daemon=True).start()

    def _tts_worker(self):
        """Processes speech requests by creating fresh instances to avoid buffer hangs."""
        while True:
            text = self.speech_queue.get()
            if text is None: break
            
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
        # Prevent queue flooding by skipping duplicate gap alerts if they come too fast
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
                    f"I'll update you on sectors and pace.")
        
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
        if speed_ms < 10: return

        for i, car in self.cars_data.items():
            if i == my_idx: continue
            
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

    def process_telemetry(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1)
        try:
            sock.bind(("0.0.0.0", 20777))
            print("[SYSTEM]: UDP Stream Connected.")
        except Exception as e:
            print(f"[ERROR]: UDP Bind Error: {e}")
            return

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
                        
                        if self.state['lap'] == 0: self.state['lap'] = my_lap.currentLapNum

                        if my_lap.sector != self.state['sector']:
                            if my_lap.sector == 1: # S1 end
                                s1 = my_lap.sector1TimeInMS / 1000.0
                                if s1 > 0:
                                    diff = s1 - self.state['best_s1']
                                    self.speak(f"Sector 1. {self.format_diff(diff)}")
                                    if s1 < self.state['best_s1']: self.state['best_s1'] = s1
                                    self.state['s1_time'] = s1
                            elif my_lap.sector == 2: # S2 end
                                s2 = my_lap.sector2TimeInMS / 1000.0
                                if s2 > 0:
                                    diff = s2 - self.state['best_s2']
                                    self.speak(f"Sector 2. {self.format_diff(diff)}")
                                    if s2 < self.state['best_s2']: self.state['best_s2'] = s2
                                    self.state['s2_time'] = s2
                            self.state['sector'] = my_lap.sector

                        if my_lap.currentLapNum > self.state['lap'] and self.state['lap'] > 0:
                            last_lap = my_lap.lastLapTime
                            self.state['last_lap'] = last_lap
                            s3 = last_lap - (self.state['s1_time'] + self.state['s2_time'])
                            if s3 > 0 and s3 < self.state['best_s3']: self.state['best_s3'] = s3

                            if self.state['overall_best_lap'] < 999.0:
                                gap = last_lap - self.state['overall_best_lap']
                                self.speak(f"Lap complete. Time {last_lap:.3f}. Gap to fastest is {gap:.3f}")
                            self.state['lap'] = my_lap.currentLapNum

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
            self.speak(f"Update. P {pos}, Lap {lap}. Tyres at {wear:.1f} percent.")

    def on_release(self, key):
        if key == keyboard.Key.space:
            self.space_held = False

if __name__ == "__main__":
    engineer = LogicEngineer()
    print("[READY] Script is running. Press CTRL+C to exit.")
    with keyboard.Listener(on_press=engineer.on_press, on_release=engineer.on_release) as listener:
        listener.join()