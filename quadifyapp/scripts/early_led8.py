# /home/volumio/Quadify/scripts/early_led8.py

import smbus2

MCP23017_ADDRESS = 0x20  # Change if your chip uses a different address
MCP23017_IODIRA  = 0x00
MCP23017_GPIOA   = 0x12

bus = smbus2.SMBus(1)
bus.write_byte_data(MCP23017_ADDRESS, MCP23017_IODIRA, 0x00)      # All A pins as output
bus.write_byte_data(MCP23017_ADDRESS, MCP23017_GPIOA, 0b00000001) # Set bit 0 = LED 8 ON
bus.close()
