#!/usr/bin/env python3
import socket
import time
import os
import subprocess

# Debounce (per key)
last_processed_time = {}
DEBOUNCE_TIME = 0.15  # was 0.3 — make it feel faster

def send_command(command, retries=5, delay=0.2):
    sock_path = "/tmp/quadify.sock"
    for attempt in range(retries):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(sock_path)
            s.sendall(command.encode("utf-8"))
            s.close()
            return
        except Exception as e:
            print(f"Attempt {attempt+1}: error sending '{command}': {e}")
            time.sleep(delay)
    print(f"Failed to send '{command}' after {retries} attempts.")

def process_key(key, current_mode):
    now = time.time()
    if key in last_processed_time and (now - last_processed_time[key]) < DEBOUNCE_TIME:
        # print(f"Debounced: {key}")
        return
    last_processed_time[key] = now

    print(f"Key: {key}  Mode: {current_mode}")

    if key == "KEY_HOME":
        send_command("home")

    elif key == "KEY_OK":
        if current_mode in [
            "menu","streaming","tidal","qobuz","spotify","library","radiomanager",
            "playlists","screensaver","configmenu","clockmenu","screensavermenu",
            "systemupdate","radioparadise","motherearthradio"
        ]:
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
            send_command("repeat")

    elif key == "KEY_LEFT":
        if current_mode in ["original", "minimal", "modern", "webradio"]:
            send_command("skip_previous")
        elif current_mode == "menu":
            send_command("scroll_left")

    elif key == "KEY_RIGHT":
        if current_mode in ["original", "minimal", "modern", "webradio"]:
            send_command("skip_next")
        elif current_mode == "menu":
            send_command("scroll_right")

    elif key == "KEY_VOLUMEUP":
        send_command("volume_plus")

    elif key == "KEY_VOLUMEDOWN":
        send_command("volume_minus")

    elif key == "KEY_UP":
        if current_mode in ["original", "modern", "minimal", "webradio"]:
            send_command("seek_plus")
        elif current_mode in [
            "streaming","tidal","qobuz","spotify","library","playlists","radiomanager",
            "displaymenu","clockmenu","configmenu","screensavermenu","systemupdate",
            "radioparadise","motherearthradio"
        ]:
            send_command("scroll_up")

    elif key == "KEY_DOWN":
        if current_mode in ["original", "modern", "minimal", "webradio"]:
            send_command("seek_minus")
        elif current_mode in [
            "streaming","tidal","qobuz","spotify","library","playlists","radiomanager",
            "displaymenu","clockmenu","configmenu","screensavermenu","systemupdate",
            "radioparadise","motherearthradio"
        ]:
            send_command("scroll_down")

    elif key in ["KEY_BACK", "KEY_EXIT", "KEY_RETURN"]:
        send_command("back")

    elif key == "KEY_POWER":
        send_command("shutdown")

def get_current_mode():
    try:
        with open("/tmp/quadify_mode", "r") as f:
            return f.read().strip()
    except Exception:
        return "clock"

def ir_event_listener():
    # Prefer /run, fall back to /var/run
    sock_path = "/run/lirc/lircd"
    if not os.path.exists(sock_path):
        fallback = "/var/run/lirc/lircd"
        if os.path.exists(fallback):
            sock_path = fallback
        else:
            print(f"Error: LIRC socket not found at /run/lirc/lircd or /var/run/lirc/lircd")
            return

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    # Read line by line in blocking mode — lowest latency & simplest
    f = s.makefile("r")
    print(f"IR listener connected to {sock_path}")

    try:
        for line in f:
            # Typical: "0000000000000001 00 KEY_POWER /home/volumio/lircd.conf"
            parts = line.strip().split()
            if len(parts) < 3:
                continue
            repeat_hex = parts[1]
            key = parts[2]

            # Ignore repeat frames (keeps things from double-firing)
            # If you ever want hold-to-scroll, remove this check.
            try:
                repeat = int(repeat_hex, 16)
            except ValueError:
                repeat = 0
            if repeat > 0:
                continue

            current_mode = get_current_mode()
            process_key(key, current_mode)
    finally:
        try:
            f.close()
        except Exception:
            pass
        s.close()

if __name__ == "__main__":
    print("Starting IR listener…")
    ir_event_listener()
