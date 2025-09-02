#!/usr/bin/env python3
# src/main.py

import RPi.GPIO as GPIO
GPIO.setwarnings(False)

import time
import threading
import logging
import yaml
import socket
import subprocess
import lirc
import os
import sys
from PIL import Image, ImageSequence

# UI / Hardware Imports
from display.screens.clock import Clock
from hardware.buttonsleds import ButtonsLEDController
from hardware.shutdown_system import shutdown_system
from display.screens.original_screen import OriginalScreen
from display.screens.modern_screen import ModernScreen
from display.screens.minimal_screen import MinimalScreen
from display.screens.vu_screen import VUScreen
from display.screens.digitalvu_screen import DigitalVUScreen
from display.screensavers.snake_screensaver import SnakeScreensaver
from display.screensavers.geo_screensaver import GeoScreensaver
from display.screensavers.bouncing_text_screensaver import BouncingTextScreensaver
from display.display_manager import DisplayManager
from managers.menu_manager import MenuManager
from managers.mode_manager import ModeManager
from managers.manager_factory import ManagerFactory
from controls.rotary_control import RotaryControl
from network.volumio_listener import VolumioListener
from assets.images.convert2 import main as convert_icons_main


# --------------------------- config / util ---------------------------

def load_config(config_path='/config.yaml'):
    abs_path = os.path.abspath(config_path)
    print(f"Attempting to load config from: {abs_path}")
    print(f"Does the file exist? {os.path.isfile(config_path)}")
    config = {}
    if os.path.isfile(config_path):
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f) or {}
            logging.debug(f"Configuration loaded from {config_path}.")
        except yaml.YAMLError as e:
            logging.error(f"Error loading config file {config_path}: {e}")
    else:
        logging.warning(f"Config file {config_path} not found. Using default configuration.")
    return config


def show_gif_loop(gif_path, stop_condition, display_manager, logger):
    """Displays an animated GIF in a loop until stop_condition() returns True."""
    try:
        image = Image.open(gif_path)
        if not getattr(image, "is_animated", False):
            logger.warning(f"GIF '{gif_path}' is not animated.")
            return
    except Exception as e:
        logger.error(f"Failed to load GIF '{gif_path}': {e}")
        return
    logger.info(f"Displaying GIF: {gif_path}")
    required_size = display_manager.oled.size  # (width, height)
    while not stop_condition():
        for frame in ImageSequence.Iterator(image):
            if stop_condition():
                return
            background = Image.new(display_manager.oled.mode, required_size)
            frame_converted = frame.convert(display_manager.oled.mode)
            background.paste(frame_converted, (0, 0))
            display_manager.oled.display(background)
            frame_duration = frame.info.get('duration', 100) / 1000.0
            time.sleep(frame_duration)


# --------------------------- main ---------------------------

def main():
    # --- Logging ---
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    logger = logging.getLogger("QuadifyMain")

    # --- Config ---
    config_path = "/data/plugins/system_hardware/quadify/quadifyapp/config.yaml"
    config = load_config(config_path)
    display_config = config.get('display', {})


    # --- DisplayManager ---
    display_manager = DisplayManager(display_config)

    # --- LEDs controller ---
    buttons_leds = ButtonsLEDController()
    buttons_leds.start()

    # Convert / ensure menu icons exist
    convert_icons_main()

    # Turn off LED8 (if present on MCP23017)
    try:
        import smbus2
        MCP23017_ADDRESS = 0x20
        MCP23017_GPIOA = 0x12
        bus = smbus2.SMBus(1)
        current = bus.read_byte_data(MCP23017_ADDRESS, MCP23017_GPIOA)
        bus.write_byte_data(MCP23017_ADDRESS, MCP23017_GPIOA, current & 0b11111110)
        bus.close()
    except Exception as e:
        print(f"Error turning off LED8: {e}")

    # --- Startup Logo ---
    logger.info("Displaying startup logo...")
    display_manager.show_logo(duration=6)
    logger.info("Startup logo display complete.")
    display_manager.clear_screen()
    logger.info("Screen cleared after logo display.")

    # --- Ready/loading orchestration ---
    volumio_ready_event = threading.Event()
    min_loading_event = threading.Event()
    ready_stop_event = threading.Event()
    MIN_LOADING_DURATION = 6  # seconds

    # --- VolumioListener (early) ---
    volumio_cfg = config.get('volumio', {})
    volumio_host = volumio_cfg.get('host', 'localhost')
    volumio_port = volumio_cfg.get('port', 3000)

    class DummyModeManager:
        def __init__(self):
            self.last_state = None

        def get_mode(self):
            return None

        def trigger(self, event):
            pass

        def process_state_change(self, sender, state, **kwargs):
            self.last_state = state

    dummy_mode_manager = DummyModeManager()
    volumio_listener = VolumioListener(host=volumio_host, port=volumio_port)
    volumio_listener.mode_manager = dummy_mode_manager

    # --- Rotary (early) to exit ready loop ---
    def on_button_press_inner():
        if not ready_stop_event.is_set():
            ready_stop_event.set()

    rotary_control = RotaryControl(
        rotation_callback=lambda d: None,
        button_callback=on_button_press_inner,
        long_press_callback=lambda: None,
        long_press_threshold=2.5
    )
    threading.Thread(target=rotary_control.start, daemon=True).start()

    def is_streaming_mode(mode: str) -> bool:
        result = mode in ("streaming", "tidal", "qobuz", "spotify", "radioparadise", "motherearthradio")
        logger.debug(f"is_streaming_mode({mode}) -> {result}")
        return result

    def handle_scroll(direction: int, mode_manager: ModeManager):
        current_mode = mode_manager.get_mode()
        logger.debug(f"[IR] handle_scroll(direction={direction}) in mode '{current_mode}'")
        # Playback screens adjust volume by rotary only (IR arrows map to list scrolling)
        if current_mode == 'menu':
            logger.debug("[IR] Scroll -> menu manager")
            mode_manager.menu_manager.scroll_selection(direction)
        elif current_mode == 'configmenu':
            logger.debug("[IR] Scroll -> config menu")
            mode_manager.config_menu.scroll_selection(direction)
        elif current_mode == 'systemupdate':
            logger.debug("[IR] Scroll -> system update menu")
            mode_manager.system_update_menu.scroll_selection(direction)
        elif current_mode == 'clockmenu':
            logger.debug("[IR] Scroll -> clock menu")
            mode_manager.clock_menu.scroll_selection(direction)
        elif current_mode == 'screensavermenu':
            logger.debug("[IR] Scroll -> screensaver menu")
            mode_manager.screensaver_menu.scroll_selection(direction)
        elif current_mode == 'radio':
            logger.debug("[IR] Scroll -> radio manager")
            mode_manager.radio_manager.scroll_selection(direction)
        elif current_mode in (
            'library', 'albums', 'artists', 'genres',
            'last100', 'mediaservers', 'favourites', 'playlists'
        ):
            logger.debug("[IR] Scroll -> library manager")
            mode_manager.library_manager.scroll_selection(direction)
        elif is_streaming_mode(current_mode):
            logger.debug(f"[IR] Scroll -> streaming manager ({current_mode})")
            mode_manager.streaming_manager.scroll_selection(direction)
        elif current_mode == 'screensaver':
            logger.debug("[IR] Scroll -> exit screensaver")
            mode_manager.exit_screensaver()
        else:
            logger.debug("[IR] Scroll -> no action for this mode")

    def handle_select(mode_manager: ModeManager):
        current_mode = mode_manager.get_mode()
        logger.debug(f"[IR] handle_select() in mode '{current_mode}'")

        if current_mode == 'menu':
            logger.debug("[IR] Select -> menu manager")
            mode_manager.menu_manager.select_item()
            return
        if current_mode == 'configmenu':
            logger.debug("[IR] Select -> config menu")
            mode_manager.config_menu.select_item()
            return
        if current_mode == 'systemupdate':
            logger.debug("[IR] Select -> system update menu")
            mode_manager.system_update_menu.select_item()
            return
        if current_mode == 'screensavermenu':
            logger.debug("[IR] Select -> screensaver menu")
            mode_manager.screensaver_menu.select_item()
            return
        if current_mode == 'clockmenu':
            logger.debug("[IR] Select -> clock menu")
            mode_manager.clock_menu.select_item()
            return
        if current_mode in (
            'library', 'albums', 'artists', 'genres',
            'last100', 'mediaservers', 'favourites', 'playlists'
        ):
            logger.debug("[IR] Select -> library manager")
            mode_manager.library_manager.select_item()
            return
        if is_streaming_mode(current_mode):
            logger.debug(f"[IR] Select -> streaming manager ({current_mode})")
            mode_manager.streaming_manager.select_item()
            return
        if current_mode == 'radio':
            logger.debug("[IR] Select -> radio manager")
            mode_manager.radio_manager.select_item()
            return

        playback_screen_mapping = {
            'original': 'original_screen',
            'modern': 'modern_screen',
            'minimal': 'minimal_screen',
            'vuscreen': 'vu_screen',
            'digitalvuscreen': 'digitalvu_screen',
        }
        if current_mode in playback_screen_mapping:
            screen = getattr(mode_manager, playback_screen_mapping[current_mode], None)
            if screen:
                logger.debug(f"[IR] Select -> toggle playback ({current_mode})")
                screen.toggle_play_pause()
            return
        if current_mode == 'clock':
            logger.debug("[IR] Select -> to menu")
            mode_manager.trigger("to_menu")
            return
        if current_mode == 'screensaver':
            logger.debug("[IR] Select -> exit screensaver")
            mode_manager.exit_screensaver()
            return

    # --------------------- IR command socket server ---------------------

    def make_command_server(mode_manager: ModeManager, early: bool = False):
        """
        Start a UNIX socket server at /tmp/quadify.sock.
        In 'early' mode, the first real press (menu/select/ok/toggle) will stop
        the ready loop AND exit this server so the main UI server can rebind.
        """
        sock_path = "/tmp/quadify.sock"

        def server():
            # Clean up any stale socket file
            try:
                os.unlink(sock_path)
            except FileNotFoundError:
                pass
            except OSError:
                pass

            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(sock_path)
            try:
                os.chmod(sock_path, 0o666)  # let other processes (IR listener) connect
            except Exception:
                pass
            srv.listen(5)
            print(f"Quadify command server listening on {sock_path} (early={early})")

            try:
                while True:
                    conn, _ = srv.accept()
                    with conn:
                        data = conn.recv(1024)
                        if not data:
                            continue

                        command = data.decode("utf-8").strip()
                        print(f"Command received: {command}")
                        current_mode = mode_manager.get_mode()

                        # Early server: first real press kills this server so UI one can bind
                        if early and not ready_stop_event.is_set() and command in ("menu", "select", "ok", "toggle"):
                            print("Exiting ready GIF due to remote control command (early server).")
                            ready_stop_event.set()
                            return  # <-- IMPORTANT: exit early server, frees the socket

                        if command == "home":
                            mode_manager.trigger("to_clock")
                        elif command == "shutdown":
                            subprocess.run(["sudo", "/bin/systemctl", "poweroff", "--no-wall"], check=False)

                        elif command == "menu":
                            if current_mode == "clock":
                                mode_manager.trigger("to_menu")
                        elif command == "toggle":
                            mode_manager.toggle_play_pause()
                        elif command == "repeat":
                            pass

                        elif command == "select":
                            handle_select(mode_manager)

                        elif command in ("scroll_up", "scroll_left"):
                            handle_scroll(-1, mode_manager)
                        elif command in ("scroll_down", "scroll_right"):
                            handle_scroll(+1, mode_manager)

                        elif command == "seek_plus":
                            subprocess.run(["volumio", "seek", "plus"], check=False)
                        elif command == "seek_minus":
                            subprocess.run(["volumio", "seek", "minus"], check=False)
                        elif command == "skip_next":
                            subprocess.run(["volumio", "next"], check=False)
                        elif command == "skip_previous":
                            subprocess.run(["volumio", "previous"], check=False)
                        elif command == "volume_plus":
                            volumio_listener.increase_volume()
                        elif command == "volume_minus":
                            volumio_listener.decrease_volume()
                        elif command == "back":
                            mode_manager.trigger("back")
            except Exception as e:
                print(f"Error in command server: {e}")
            finally:
                try:
                    srv.close()
                except Exception:
                    pass
                # Ensure the path is freed for the next bind
                try:
                    os.unlink(sock_path)
                except Exception:
                    pass

        t = threading.Thread(target=server, daemon=True)
        t.start()
        return t


    # Start early command server with dummy manager (for ready exit + basic commands)
    make_command_server(dummy_mode_manager, early=True)
    print("Quadify command server thread (early) started.")

    # --- Loading GIF during boot ---
    def show_loading():
        loading_gif_path = display_config.get('loading_gif_path', 'loading.gif')
        try:
            image = Image.open(loading_gif_path)
            if not getattr(image, "is_animated", False):
                logger.warning(f"Loading GIF '{loading_gif_path}' is not animated.")
                return
        except IOError:
            logger.error(f"Failed to load loading GIF '{loading_gif_path}'.")
            return
        logger.info("Displaying loading GIF during startup.")
        display_manager.clear_screen()
        time.sleep(0.1)
        while not (volumio_ready_event.is_set() and min_loading_event.is_set()):
            for frame in ImageSequence.Iterator(image):
                if volumio_ready_event.is_set() and min_loading_event.is_set():
                    logger.info("Volumio ready & min load done, stopping loading GIF.")
                    return
                display_manager.oled.display(frame.convert(display_manager.oled.mode))
                frame_duration = frame.info.get('duration', 100) / 1000.0
                time.sleep(frame_duration)
        logger.info("Exiting loading GIF display thread.")

    threading.Thread(target=show_loading, daemon=True).start()

    # Minimum loading duration timer
    def set_min_loading_event():
        time.sleep(MIN_LOADING_DURATION)
        min_loading_event.set()
        logger.info("Minimum loading duration has elapsed.")

    threading.Thread(target=set_min_loading_event, daemon=True).start()

    def on_state_changed(sender, state):
        logger.info(f"[on_state_changed] State: {state!r}")
        status = str(state.get('status', '???')).lower()
        logger.info(f"[on_state_changed] Detected status: {status}")
        if hasattr(volumio_listener.mode_manager, "process_state_change"):
            volumio_listener.mode_manager.process_state_change(sender, state)
        if not ready_stop_event.is_set() and status == 'play':
            logger.info("Detected playback start: stopping ready loop.")
            ready_stop_event.set()
        if status in ['play', 'stop', 'pause', 'unknown'] and not volumio_ready_event.is_set():
            volumio_ready_event.set()

    volumio_listener.state_changed.connect(on_state_changed)
    logger.info("Bound on_state_changed to volumio_listener.state_changed")

    # Wait for readiness then show ready loop
    logger.info("Waiting for Volumio readiness & min load time.")
    volumio_ready_event.wait()
    min_loading_event.wait()
    logger.info("Volumio is ready & min loading time passed, proceeding to ready GIF.")

    def show_ready_gif_until_event(stop_event, gif_path):
        try:
            image = Image.open(gif_path)
            if not getattr(image, "is_animated", False):
                display_manager.oled.display(image.convert(display_manager.oled.mode))
                return
            while not stop_event.is_set():
                for frame in ImageSequence.Iterator(image):
                    if stop_event.is_set():
                        return
                    display_manager.oled.display(frame.convert(display_manager.oled.mode))
                    frame_duration = frame.info.get('duration', 100) / 1000.0
                    time.sleep(frame_duration)
        except Exception as e:
            logger.error(f"Failed to loop GIF {gif_path}: {e}")

    ready_loop_path = display_config.get('ready_loop_path', 'ready_loop.gif')
    threading.Thread(
        target=show_ready_gif_until_event,
        args=(ready_stop_event, ready_loop_path),
        daemon=True
    ).start()
    ready_stop_event.wait()
    logger.info("Ready GIF exited, continuing to UI startup.")

    # --- Build full UI stack ---
    clock_config = config.get('clock', {})
    clock = Clock(display_manager, clock_config, volumio_listener)
    clock.logger = logging.getLogger("Clock")
    clock.logger.setLevel(logging.INFO)

    mode_manager = ModeManager(
        display_manager=display_manager,
        clock=clock,
        volumio_listener=volumio_listener,
        preference_file_path="../preference.json",
        config=config
    )

    manager_factory = ManagerFactory(
        display_manager=display_manager,
        volumio_listener=volumio_listener,
        mode_manager=mode_manager,
        config=config
    )
    manager_factory.setup_mode_manager()
    volumio_listener.mode_manager = mode_manager
    volumio_listener.menu_manager = mode_manager.menu_manager

    # Handoff last early state if any
    if getattr(dummy_mode_manager, 'last_state', None):
        logger.info("Handing off last Volumio state from DummyModeManager to real ModeManager")
        mode_manager.process_state_change(volumio_listener, dummy_mode_manager.last_state)
        status = (dummy_mode_manager.last_state.get("status") or "").lower()
        service = (dummy_mode_manager.last_state.get("service") or "").lower()
        display_mode = mode_manager.config.get("display_mode", "original")

        if status == "play":
            if service == "webradio":
                mode_manager.trigger("to_webradio")
            elif display_mode == "vuscreen":
                mode_manager.trigger("to_vuscreen")
            elif display_mode == "digitalvuscreen":
                mode_manager.trigger("to_digitalvuscreen")
            elif display_mode == "modern":
                mode_manager.trigger("to_modern")
            elif display_mode == "minimal":
                mode_manager.trigger("to_minimal")
            else:
                mode_manager.trigger("to_original")
        elif status in ["pause", "stop"]:
            mode_manager.trigger("to_menu")
        else:
            mode_manager.trigger("to_menu")

    # Start the real command server bound to the real mode_manager
    make_command_server(mode_manager, early=False)
    print("Quadify command server thread (UI) started.")

    # --- Rotary handlers (use same unified handlers) ---
    def on_rotate_ui(direction):
        current_mode = mode_manager.get_mode()

        # Playback screens use rotary for volume
        if current_mode == 'original':
            volume_change = 40 if direction == 1 else -40
            mode_manager.original_screen.adjust_volume(volume_change)
            return
        if current_mode == 'modern':
            volume_change = 10 if direction == 1 else -20
            mode_manager.modern_screen.adjust_volume(volume_change)
            return
        if current_mode == 'minimal':
            volume_change = 10 if direction == 1 else -20
            mode_manager.minimal_screen.adjust_volume(volume_change)
            return
        if current_mode == 'vuscreen':
            volume_change = 10 if direction == 1 else -20
            mode_manager.vu_screen.adjust_volume(volume_change)
            return
        if current_mode == 'digitalvuscreen':
            volume_change = 10 if direction == 1 else -20
            mode_manager.digitalvu_screen.adjust_volume(volume_change)
            return
        if current_mode == 'webradio':
            volume_change = 10 if direction == 1 else -20
            mode_manager.webradio_screen.adjust_volume(volume_change)
            return

        # All list-type modes (menu, library, streaming, etc.)
        handle_scroll(direction, mode_manager)

    def on_button_press_ui():
        handle_select(mode_manager)

    def on_long_press_ui():
        current_mode = mode_manager.get_mode()
        if current_mode == "menu":
            mode_manager.trigger("to_clock")
        else:
            mode_manager.trigger("back")

    rotary_control.rotation_callback = on_rotate_ui
    rotary_control.button_callback = on_button_press_ui
    rotary_control.long_press_callback = on_long_press_ui

    # --- Main loop ---
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down Quadify via KeyboardInterrupt.")
    finally:
        if 'buttons_leds' in locals() and buttons_leds:
            try:
                buttons_leds.stop()
            except Exception as e:
                logger.warning(f"Error stopping buttons_leds: {e}")

        if 'rotary_control' in locals() and rotary_control:
            try:
                rotary_control.stop()
            except Exception as e:
                logger.warning(f"Error stopping rotary_control: {e}")

        try:
            volumio_listener.stop_listener()
        except Exception as e:
            logger.warning(f"Error stopping volumio_listener: {e}")

        if 'clock' in locals() and clock:
            try:
                clock.stop()
            except Exception as e:
                logger.warning(f"Error stopping clock: {e}")

        if 'display_manager' in locals() and display_manager:
            try:
                display_manager.clear_screen()
            except Exception as e:
                logger.warning(f"Error clearing display: {e}")

        logger.info("Quadify shut down gracefully.")


if __name__ == "__main__":
    main()

