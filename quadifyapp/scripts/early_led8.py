#!/usr/bin/env python3
import os
import sys
import yaml
from smbus2 import SMBus

# MCP23017 registers
IODIRA = 0x00
GPIOA  = 0x12

DEFAULT_ADDR = 0x20  # fallback if not in config

def _int_auto(x):
    """int that accepts '0x20', '32', etc."""
    if isinstance(x, int):
        return x
    return int(str(x), 0)

def load_mcp_addr():
    # 1) ENV overrides (optional)
    if os.getenv("MCP23017_ADDRESS"):
        try:
            return _int_auto(os.getenv("MCP23017_ADDRESS"))
        except Exception:
            pass

    # 2) Try config.yaml (several likely locations/keys)
    candidates = [
        os.getenv("QUADIFY_CONFIG"),
        "/data/plugins/system_hardware/quadify/quadifyapp/config.yaml",
        os.path.join(os.path.dirname(__file__), "..", "config.yaml"),
        os.path.join(os.path.dirname(__file__), "..", "..", "config.yaml"),
    ]
    keys_to_try = [
        ("mcp23017_address",),            # top-level
        ("mcp23017", "address"),          # nested
        ("hardware", "mcp23017_address"),
        ("display", "mcp23017_address"),
    ]

    for path in [p for p in candidates if p]:
        try:
            with open(path, "r") as f:
                cfg = yaml.safe_load(f) or {}
            for key_path in keys_to_try:
                d = cfg
                for k in key_path:
                    if isinstance(d, dict) and k in d:
                        d = d[k]
                    else:
                        d = None
                        break
                if d is not None:
                    return _int_auto(d)
        except Exception:
            pass

    return DEFAULT_ADDR

def main():
    addr = load_mcp_addr()
    try:
        with SMBus(1) as bus:
            # Port A as outputs
            bus.write_byte_data(addr, IODIRA, 0x00)
            # Turn LED8 ON (bit 0 = 1)
            bus.write_byte_data(addr, GPIOA, 0b00000001)
        print(f"early_led8: LED8 ON at MCP23017 0x{addr:02X}")
    except Exception as e:
        print(f"early_led8: failed at 0x{addr:02X}: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()

