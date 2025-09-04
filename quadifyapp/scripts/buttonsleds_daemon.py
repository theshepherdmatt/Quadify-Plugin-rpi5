#!/usr/bin/env python3
import os, sys, time, signal, logging
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parent.parent                     # …/quadifyapp
SRC  = ROOT / "src"                           # …/quadifyapp/src

# Ensure the 'src' directory (which contains 'hardware/') is importable
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hardware.buttonsleds import ButtonsLEDController

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
LOG = logging.getLogger("buttonsleds")

def main():
    LOG.info("Buttons/LEDs daemon starting")
    ctl = ButtonsLEDController()  # <- no kwargs, no YAML here
    stopping = False
    def _stop(*_): 
        nonlocal stopping; stopping = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    ctl.start()
    try:
        while not stopping:
            time.sleep(0.5)
    finally:
        # Do NOT kill LEDs on service stop/restart; the shutdown unit handles that.
        try: ctl.stop()
        except Exception:
            pass
        LOG.info("Buttons/LEDs daemon exiting.")

if __name__ == "__main__":
    main()
