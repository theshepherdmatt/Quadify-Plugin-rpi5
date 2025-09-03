import smbus2
import json
import time
import threading
import logging
import subprocess
from enum import IntEnum
import yaml
from pathlib import Path

# MCP23017 Register Definitions
MCP23017_IODIRA = 0x00
MCP23017_IODIRB = 0x01
MCP23017_GPIOA  = 0x12
MCP23017_GPIOB  = 0x13
MCP23017_GPPUA  = 0x0C
MCP23017_GPPUB  = 0x0D

DEFAULT_MCP23017_ADDRESS = 0x20
SWAP_COLUMNS = True  # If your wiring for columns is reversed

class LED(IntEnum):
    """
    Bits in GPIOA representing each LED.
    We assume:
      - Button 1 => Play LED
      - Button 2 => Pause LED
      - Button 3 => Previous LED
      - Button 4 => Next LED
      - Button 5 => Shuffle LED
      - Button 6 => Repeat LED
      - Button 7 => Spare LED
      - Button 8 => Reload LED
    """
    PLAY   = 0b10000000  
    PAUSE  = 0b01000000  
    PREV   = 0b00100000
    NEXT   = 0b00010000
    SHUFF  = 0b00001000
    REPEAT = 0b00000100
    SPARE  = 0b00000010
    RELOAD = 0b00000001

class ButtonsLEDController:
    """
    - Button 1 => Play (Volumio 'play')
    - Button 2 => Pause (Volumio 'pause')
    - The rest is as before: Next, Prev, Shuffle, Repeat, Spare, Reload.

    'Play' LED is lit whenever Volumio is playing,
    'Pause' LED is lit whenever Volumio is paused,
    and ephemeral LED override ensures only one LED is lit at a time.
    """

    def __init__(self, config_path='config.yaml', debounce_delay=0.1):
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.INFO)

        # Optional console logging
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        ch.setFormatter(formatter)
        if not self.logger.handlers:
            self.logger.addHandler(ch)

        self.debounce_delay = debounce_delay
        self.bus = None

        # Attempt to open SMBus #1
        try:
            self.bus = smbus2.SMBus(1)
        except Exception as e:
            self.logger.error(f"Unable to open I2C bus: {e}")

        # 4 rows x 2 columns => 
        # row0 => [button1, button2], row1 => [3,4], row2 => [5,6], row3 => [7,8]
        self.prev_button_state = [[1,1],[1,1],[1,1],[1,1]]
        self.button_map = [
            [1, 2],  # row0 => (Play=1, Pause=2)
            [3, 4],  # row1 => (Prev=3, Next=4)
            [5, 6],  # row2 => (Shuffle=5, Repeat=6)
            [7, 8],  # row3 => (Spare=7, Reload=8)
        ]

        # LED states
        self.status_led_state = 0            # For showing play/pause
        self.current_button_led_state = 0    # Ephemeral override
        self.current_led_state = 0           # Last hardware state

        self.mcp23017_address = self._load_config(config_path)
        self._initialize_mcp23017()

        self.running = False
        self.button_thread = None
        self.volumio_monitor_thread = None

        self.button8_down_time = None
        self.button8_pending = False


    # in ButtonsLEDController._load_config()
    def _load_config(self, cfg_path):
        path = Path(cfg_path)
        if path.is_file():
            try:
                with open(path, "r") as f:
                    data = yaml.safe_load(f) or {}
                    val = data.get("mcp23017_address", DEFAULT_MCP23017_ADDRESS)
                    # NEW: force to int
                    if isinstance(val, str):
                        val = int(val, 16) if val.lower().startswith("0x") else int(val)
                    elif isinstance(val, bool):
                        val = DEFAULT_MCP23017_ADDRESS
                    return val
            except Exception as e:
                self.logger.error(f"Error reading config: {e}")
        return DEFAULT_MCP23017_ADDRESS


    def _initialize_mcp23017(self):
        if not self.bus:
            return
        try:
            # GPIOA => outputs for LEDs
            self.bus.write_byte_data(self.mcp23017_address, MCP23017_IODIRA, 0x00)
            # GPIOB => B0/B1=outputs, B2-B7=inputs
            self.bus.write_byte_data(self.mcp23017_address, MCP23017_IODIRB, 0xFC)

            # Pull-ups on B2..B7
            self.bus.write_byte_data(self.mcp23017_address, MCP23017_GPPUB, 0xFC)

            # Clear all LEDs
            self.bus.write_byte_data(self.mcp23017_address, MCP23017_GPIOA, 0x00)
            # Columns high (inactive)
            self.bus.write_byte_data(self.mcp23017_address, MCP23017_GPIOB, 0x03)
            self.logger.info("MCP23017 init complete.")
        except Exception as e:
            self.logger.error(f"Init error: {e}")

    def start(self):
        self.running = True
        self.button_thread = threading.Thread(target=self._monitor_buttons_loop, name="ButtonMonitor")
        self.button_thread.start()
        self.volumio_monitor_thread = threading.Thread(target=self._monitor_volumio_loop, name="VolumioMonitor")
        self.volumio_monitor_thread.start()

        # NEW: immediate LED state
        try:
            self.update_play_pause_led()
        except Exception:
            self.status_led_state = 0
            self.control_leds()

        self.logger.info("ButtonsLEDController started.")

    def stop(self):
        self.running = False
        if self.button_thread and self.button_thread.is_alive():
            self.button_thread.join()
        if self.volumio_monitor_thread and self.volumio_monitor_thread.is_alive():
            self.volumio_monitor_thread.join()
        # Turn off the LEDs and put columns back to inactive
        self.shutdown_leds()
        self.logger.info("ButtonsLEDController stopped.")

    # -----------------------------------------------------------------
    # Monitoring Buttons
    # -----------------------------------------------------------------
    def _monitor_buttons_loop(self):
        while self.running:
            if not self.bus:
                break
            matrix = self._read_matrix()
            for r in range(4):
                for c in range(2):
                    curr = matrix[r][c]
                    prev = self.prev_button_state[r][c]
                    btn_id = self.button_map[r][c]

                    # --- Special logic for Button 8 (long press support) ---
                    if btn_id == 8:
                        # Button 8 pressed down
                        if curr == 0 and prev == 1:
                            self.button8_down_time = time.time()
                            self.button8_pending = True
                        # Button 8 released
                        elif curr == 1 and prev == 0 and self.button8_pending:
                            held_time = time.time() - self.button8_down_time if self.button8_down_time else 0
                            if held_time >= 3.0:
                                self.logger.info("Button 8 long press (restart CAVA only)")
                                self.restart_cava_only()
                            else:
                                self.logger.info("Button 8 short press (restart Quadify)")
                                self.restart_quadify_only()
                            self.light_button_led_for(LED.RELOAD, 0.5)
                            self.button8_pending = False

                    # --- All other buttons (default: short press only) ---
                    else:
                        if curr == 0 and prev == 1:
                            self.logger.info(f"Button {btn_id} pressed.")
                            self.handle_button_press(btn_id)

                    # Update previous state for this button
                    self.prev_button_state[r][c] = curr

            time.sleep(self.debounce_delay)


    def _read_matrix(self):
        default = [[1,1],[1,1],[1,1],[1,1]]
        if not self.bus:
            return default
        result = [[1,1],[1,1],[1,1],[1,1]]
        try:
            for col in range(2):
                col_out = ~(1 << col) & 0x03
                self.bus.write_byte_data(self.mcp23017_address, MCP23017_GPIOB, col_out | 0xFC)
                time.sleep(0.005)
                val_b = self.bus.read_byte_data(self.mcp23017_address, MCP23017_GPIOB)
                for row in range(4):
                    bit_val = (val_b >> (row+2)) & 0x01
                    if SWAP_COLUMNS:
                        result[row][1-col] = bit_val
                    else:
                        result[row][col] = bit_val
        except Exception as e:
            self.logger.error(f"Matrix read error: {e}")
        return result

    # -----------------------------------------------------------------
    # Volumio Monitor => sets PLAY or PAUSE LED
    # -----------------------------------------------------------------
    def _monitor_volumio_loop(self):
        while self.running:
            try:
                self.update_play_pause_led()
            except Exception as e:
                self.logger.error(f"Volumio monitor error: {e}")
            time.sleep(2)

    def update_play_pause_led(self):
        try:
            res = subprocess.run(["volumio", "status"], capture_output=True, text=True, timeout=2)
            s = (res.stdout or "").strip()
            # Only try to parse if it looks like JSON
            if not s or s[0] not in "[{":
                return
            data = json.loads(s)

            state = (data.get("status") or "").lower()
            prev_led_state = self.status_led_state

            if state == "play":
                self.status_led_state = LED.PLAY.value
            elif state in ("pause", "stop"):
                self.status_led_state = LED.PAUSE.value
            else:
                self.status_led_state = 0

            if self.current_button_led_state and self.status_led_state != prev_led_state:
                self.current_button_led_state = 0

            self.control_leds()
        except Exception as e:
            # keep it low-noise now that we guard for non-JSON
            self.logger.debug(f"update_play_pause_led skipped: {e}")


    # -----------------------------------------------------------------
    # Button Press => ephemeral LED
    # -----------------------------------------------------------------
    def handle_button_press(self, btn_id):
        """
        Button 1 => 'volumio play' or 'toggle'?
        Button 2 => 'volumio pause'
        etc.
        """
        try:
            if btn_id == 1:
                # "Play"
                subprocess.run(["volumio","play"], check=False)
                #self.light_button_led_for(LED.PLAY, 0.5)
            elif btn_id == 2:
                # "Pause"
                subprocess.run(["volumio","pause"], check=False)
                #self.light_button_led_for(LED.PAUSE, 0.5)
            elif btn_id == 3:
                subprocess.run(["volumio","previous"], check=False)
                self.light_button_led_for(LED.PREV, 0.5)
            elif btn_id == 4:
                subprocess.run(["volumio","next"], check=False)
                self.light_button_led_for(LED.NEXT, 0.5)
            elif btn_id == 5:
                subprocess.run(["volumio","random"], check=False)
                self.light_button_led_for(LED.SHUFF, 0.5)
            elif btn_id == 6:
                subprocess.run(["volumio","repeat"], check=False)
                self.light_button_led_for(LED.REPEAT, 0.5)
            elif btn_id == 7:
                self.light_button_led_for(LED.SPARE, 0.5)
            else:
                self.logger.warning(f"No action for button {btn_id}")
        except Exception as e:
            self.logger.error(f"handle_button_press => {e}")

    # -----------------------------------------------------------------
    # Ephemeral LED override
    # -----------------------------------------------------------------
    def light_button_led_for(self, led_enum, duration):
        """
        ephemeral override => show just this LED for 'duration' seconds,
        ignoring the play/pause LED.
        """
        self.current_button_led_state = led_enum.value
        self.control_leds()
        t = threading.Timer(duration, self.reset_button_led)
        t.start()

    def reset_button_led(self):
        self.current_button_led_state = 0
        self.control_leds()

    def control_leds(self):
        """
        If ephemeral LED is active => show only ephemeral
        else => show the status_led_state (play or pause).
        """
        if self.current_button_led_state != 0:
            total_state = self.current_button_led_state
        else:
            total_state = self.status_led_state

        if total_state != self.current_led_state:
            if self.bus:
                try:
                    self.bus.write_byte_data(self.mcp23017_address, MCP23017_GPIOA, total_state)
                    self.current_led_state = total_state
                    self.logger.info(f"LED state => {bin(total_state)}")
                except Exception as e:
                    self.logger.error(f"Error setting LEDs: {e}")
            else:
                self.logger.error("No bus => cannot set LED.")
        else:
            self.logger.debug("No LED change needed.")

    def shutdown_leds(self):
        """
        Turns off all LED outputs on the MCP23017.
        Note: This only resets the outputs; it does not remove power from the board.
        """
        if self.bus:
            try:
                # Clear LED outputs on port A (assuming LEDs are connected to GPIOA)
                self.bus.write_byte_data(self.mcp23017_address, MCP23017_GPIOA, 0x00)
                # Optionally, you could also reset GPIOB if needed (e.g. setting columns to their inactive state)
                self.bus.write_byte_data(self.mcp23017_address, MCP23017_GPIOB, 0x03)
                self.logger.info("MCP23017 shutdown: All LEDs turned off.")
            except Exception as e:
                self.logger.error(f"Error turning off LEDs on MCP23017: {e}")


    def close(self):
        if self.bus:
            self.bus.close()
            self.logger.info("Closed SMBus.")
