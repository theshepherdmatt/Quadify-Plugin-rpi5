#!/usr/bin/env python3
import socket
import time
import os
import subprocess

# Global dictionary for debouncing
last_processed_time = {}
DEBOUNCE_TIME = 0.3  # seconds, adjust as needed

def send_command(command, retries=5, delay=0.5):
    sock_path = "/tmp/quadify.sock"
    for attempt in range(retries):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(sock_path)
            s.sendall(command.encode("utf-8"))
            s.close()
            return  # Success
        except Exception as e:
            print(f"Attempt {attempt+1}: Error sending command '{command}': {e}")
            time.sleep(delay)
    print(f"Failed to send command '{command}' after {retries} attempts.")


def process_key(key, current_mode):
    """Decide what command to run based on the key and current mode, with debouncing."""
    now = time.time()
    if key in last_processed_time and (now - last_processed_time[key]) < DEBOUNCE_TIME:
        print(f"Ignoring duplicate key: {key}")
        return
    last_processed_time[key] = now

    print(f"Processing key: {key} in mode: {current_mode}")
    
    if key == "KEY_HOME":
        send_command("home")

    elif key == "KEY_OK":
        # In menu or tidal mode, KEY_OK selects the item.
        if current_mode in ["menu", "streaming", "tidal", "qobuz", "spotify", "library", "radiomanager", "playlists", "screensaver", "configmenu", "clockmenu", "screensavermenu", "systemupdate", "radioparadise", "motherearthradio"]:
            send_command("select")
        elif current_mode in ["clock", "screensaver"]:
            send_command("toggle")
        else:
            send_command("toggle")

    elif key == "KEY_MENU":
        if current_mode == "screensaver":
            send_command("exit_screensaver")
        elif current_mode == "clock":
            send_command("menu")
        else:
            pass

    elif key == "KEY_LEFT":
        if current_mode in ["original", "minimal", "modern", "webradio"]:
            send_command("skip_previous")
        elif current_mode in ["menu"]:
            send_command("scroll_left")

    elif key == "KEY_RIGHT":
        if current_mode in ["original", "minimal", "modern", "webradio"]:
            send_command("skip_next")
        elif current_mode in ["menu"]:
            send_command("scroll_right")


    elif key == "KEY_VOLUMEUP":
        send_command("volume_plus")

    elif key == "KEY_VOLUMEDOWN":
        send_command("volume_minus")


    elif key == "KEY_UP":
        if current_mode in ["original", "modern", "minimal", "webradio"]:
            send_command("seek_plus")
        elif current_mode in ["streaming", "tidal", "qobuz", "spotify", "library", "playlists", "radiomanager", 
                            "displaymenu", "clockmenu", "configmenu", "screensavermenu", "systemupdate", "radioparadise", "motherearthradio"]:
            send_command("scroll_up")
        else:
            print("No mapping for KEY_UP in current mode.")

    elif key == "KEY_DOWN":
        if current_mode in ["original", "modern", "minimal", "webradio"]:
            send_command("seek_minus")
        elif current_mode in ["streaming", "tidal", "qobuz", "spotify", "library", "playlists", "radiomanager", 
                            "displaymenu", "clockmenu", "configmenu", "screensavermenu", "systemupdate", "radioparadise", "motherearthradio"]:
            send_command("scroll_down")
        else:
            print("No mapping for KEY_DOWN in current mode.")

    
    elif key in ["KEY_BACK", "KEY_EXIT", "KEY_RETURN"]:
        send_command("back")

    elif key == "KEY_POWER":
        send_command("shutdown")
    else:
        print(f"No mapping for key: {key}")


def get_current_mode():
    """
    Reads the current Quadify mode from a file.
    Ensure your Quadify application writes the current mode to /tmp/quadify_mode.
    """
    try:
        with open("/tmp/quadify_mode", "r") as f:
            return f.read().strip()
    except Exception:
        return "clock"  # Default mode if file not found

def ir_event_listener():
    """
    Listens for IR events from the LIRC socket and processes them.
    """
    # Path to LIRC's Unix socket (default is usually /var/run/lirc/lircd)
    sock_path = "/var/run/lirc/lircd"
    if not os.path.exists(sock_path):
        print(f"Error: LIRC socket {sock_path} not found!")
        return

    # Create a Unix socket and connect to LIRC daemon
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    s.setblocking(False)
    print("IR listener connected to LIRC socket.")

    try:
        while True:
            try:
                data = s.recv(1024)
                if data:
                    # LIRC events are typically sent as lines of text.
                    lines = data.decode("utf-8").splitlines()
                    for line in lines:
                        # Expected line format:
                        # "0000000000000001 00 KEY_POWER /home/volumio/lircd.conf"
                        parts = line.split()
                        if len(parts) >= 3:
                            key = parts[2]
                            current_mode = get_current_mode()
                            print(f"IR event: {key} (mode: {current_mode})")
                            process_key(key, current_mode)
                else:
                    time.sleep(0.1)
            except BlockingIOError:
                time.sleep(0.1)
    finally:
        s.close()

if __name__ == "__main__":
    print("Starting IR listener...")
    ir_event_listener()
