#!/usr/bin/env python3
import os, sys, time, signal, logging

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_ROOT = os.path.abspath(os.path.join(HERE, ".."))
if SRC_ROOT not in sys.path:
    sys.path.insert(0, SRC_ROOT)

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
        try: ctl.shutdown_leds()
        except Exception: pass
        try: ctl.stop()
        except Exception: pass
        LOG.info("Buttons/LEDs daemon exiting.")

if __name__ == "__main__":
    main()
