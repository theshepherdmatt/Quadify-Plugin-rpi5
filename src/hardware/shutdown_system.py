import os
import time
import RPi.GPIO as GPIO
from PIL import Image, ImageDraw, ImageFont

def reset_oled():
    """
    Resets the OLED by pulling its reset (RST) pin low.
    This ensures the display turns off.
    """
    OLED_GPIO_PIN = 25  # The GPIO pin connected to the OLED reset
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(OLED_GPIO_PIN, GPIO.OUT)
        GPIO.output(OLED_GPIO_PIN, GPIO.LOW)
        time.sleep(1)
    except Exception as e:
        print(f"Error while resetting OLED GPIO: {e}")
    finally:
        GPIO.cleanup()


def display_shutdown_text(display_manager):
    """
    Renders and displays a "Shutting Down..." message using a custom font.
    """
    with display_manager.lock:
        width, height = display_manager.oled.width, display_manager.oled.height
        image = Image.new("RGB", (width, height), "black")
        draw = ImageDraw.Draw(image)
        
        try:
            # Adjust the font path and size as needed.
            font = ImageFont.truetype("/home/volumio/Quadify/src/assets/fonts/OpenSans-Regular.ttf", 22)
        except Exception as e:
            display_manager.logger.error(f"Error loading custom font: {e}")
            font = ImageFont.load_default()
        
        text = "Shutting Down..."
        text_width, text_height = draw.textsize(text, font=font)
        x = (width - text_width) // 2
        y = (height - text_height) // 2
        draw.text((x, y), text, font=font, fill="white")
        
        display_manager.oled.display(image)
        display_manager.logger.info("Shutdown text displayed on OLED.")


def shutdown_system(display_manager, buttons_leds, mode_manager=None):
    """
    Stops all active screens, displays the shutdown message,
    turns off the MCP23017 LED outputs, resets the OLED,
    and then shuts down the Raspberry Pi.
    """
    # Stop background display updates (clock, screensaver, etc.)
    if mode_manager is not None:
        mode_manager.stop_all_screens()
    
    # Reinforce the shutdown message over the entire delay period
    shutdown_duration = 10  # seconds
    start_time = time.time()
    while time.time() - start_time < shutdown_duration:
        display_shutdown_text(display_manager)
        time.sleep(1)  # Re-display every second to override any lingering updates

    # Turn off LEDs on the MCP23017 board and close the I2C bus
    buttons_leds.shutdown_leds()
    buttons_leds.close()

    # Reset the OLED so that it turns off
    reset_oled()
    
    # Finally, power off the Raspberry Pi (ensure passwordless shutdown in sudoers)
    os.system("sudo systemctl poweroff --no-wall")
