#!/usr/bin/env python3
import sys, yaml, os
from smbus2 import SMBus

BUSNUM = 1
IODIRA, IODIRB = 0x00, 0x01
GPIOA,  GPIOB  = 0x12, 0x13
OLATA,  OLATB  = 0x14, 0x15

PLUGIN_CFGS = [
    "/data/plugins/system_hardware/quadify/quadifyapp/config.yaml",  # preferred
    "/data/plugins/system_hardware/quadify/config.yaml",             # alt
    "/home/volumio/Quadify/config.yaml"                              # legacy
]

def _coerce_addr(v):
    if v is None:
        return None
    if isinstance(v, int):
        return v
    try:
        s = str(v).strip().lower()
        return int(s, 16) if s.startswith("0x") else int(s)
    except Exception:
        return None

def addr_from_cfg():
    # Try env override first (UI or service can pass an override)
    env = os.environ.get("MCP23017_ADDR")
    a = _coerce_addr(env)
    if a is not None:
        return a

    # Try YAML config(s)
    for p in PLUGIN_CFGS:
        try:
            with open(p, "r") as f:
                data = yaml.safe_load(f) or {}
            # allow both root key and nested under buttons_leds
            v = (data.get("mcp23017_address")
                 or (data.get("buttons_leds") or {}).get("mcp23017_address"))
            a = _coerce_addr(v)
            if a is not None:
                return a
        except Exception:
            continue
    return None

def probe(bus, addr):
    try:
        bus.write_quick(addr)
        return True
    except Exception:
        return False

def off(bus, addr):
    try:
        # All pins output-low briefly, then float to inputs (LEDs off even if active-low)
        bus.write_byte_data(addr, IODIRA, 0x00)
        bus.write_byte_data(addr, IODIRB, 0x00)
        for reg in (OLATA, OLATB, GPIOA, GPIOB):
            bus.write_byte_data(addr, reg, 0x00)
        bus.write_byte_data(addr, IODIRA, 0xFF)
        bus.write_byte_data(addr, IODIRB, 0xFF)
        print(f"LEDs off via MCP23017 at 0x{addr:02X}")
        return True
    except Exception:
        return False

def main():
    # priority: CLI arg -> config -> scan 0x20–0x27
    addr = None
    if len(sys.argv) > 1:
        addr = _coerce_addr(sys.argv[1])
    if addr is None:
        addr = addr_from_cfg()

    with SMBus(BUSNUM) as bus:
        if addr is not None and probe(bus, addr) and off(bus, addr):
            return
        for a in range(0x20, 0x28):
            if probe(bus, a) and off(bus, a):
                return
    print("No responding MCP23017 on i2c-1 (0x20–0x27).", file=sys.stderr)
    sys.exit(1)

if __name__ == "__main__":
    main()
