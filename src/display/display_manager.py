import logging
import os
import time
import threading
from PIL import Image, ImageDraw, ImageFont, ImageSequence
from luma.core.interface.serial import spi
from luma.oled.device import ssd1322


class DisplayManager:
    def __init__(self, config):
        # SPI connection for SSD1322 (256x64), rotate=2 for your panel orientation
        self.serial = spi(device=0, port=0)
        self.oled = ssd1322(self.serial, width=256, height=64, rotate=2)
        self.icons = {}
        self.config = config or {}
        self.lock = threading.Lock()

        # Logger
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            ch = logging.StreamHandler()
            ch.setLevel(logging.DEBUG)
            ch.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
            self.logger.addHandler(ch)

        self.logger.info("DisplayManager initialized.")

        # Fonts only (icons are owned by MenuManager)
        self.fonts = {}
        self._load_fonts()

        # Mode change callbacks
        self.on_mode_change_callbacks = []

    # ---------- Public helpers ----------

    @property
    def size(self):
        return self.oled.size  # (width, height)

    def add_on_mode_change_callback(self, callback):
        if callable(callback):
            self.on_mode_change_callbacks.append(callback)
            self.logger.debug(f"Added mode change callback: {callback}")

    def notify_mode_change(self, current_mode):
        self.logger.debug(f"Notifying mode change to: {current_mode}")
        for cb in self.on_mode_change_callbacks:
            try:
                cb(current_mode)
            except Exception as e:
                self.logger.error(f"Error in callback {cb}: {e}")

    # ---------- Font loading ----------

    def _load_fonts(self):
        fonts_config = (self.config or {}).get('fonts', {})
        default_font = ImageFont.load_default()
        for key, font_info in fonts_config.items():
            path = font_info.get('path')
            size = font_info.get('size', 12)
            if path and os.path.isfile(path):
                try:
                    self.fonts[key] = ImageFont.truetype(path, size=size)
                    self.logger.info(f"Loaded font '{key}' from '{path}' size={size}.")
                except IOError as e:
                    self.logger.error(f"Error loading font '{key}' from '{path}': {e}")
                    self.fonts[key] = default_font
            else:
                self.logger.debug(f"Font '{key}' missing at '{path}', using default.")
                self.fonts[key] = default_font
        self.logger.info(f"Fonts loaded: {list(self.fonts.keys())}")

    # ---------- Drawing primitives ----------

    def clear_screen(self):
        with self.lock:
            img = Image.new("RGB", self.oled.size, "black").convert(self.oled.mode)
            self.oled.display(img)

    def display_text(self, text, position, font_key='default', fill="white"):
        with self.lock:
            img = Image.new("RGB", self.oled.size, "black")
            draw = ImageDraw.Draw(img)
            font = self.fonts.get(font_key, ImageFont.load_default())
            draw.text(position, text, font=font, fill=fill)
            self.oled.display(img.convert(self.oled.mode))

    def draw_custom(self, draw_function):
        """draw_function(draw) -> draw on a fresh black image."""
        with self.lock:
            img = Image.new("RGB", self.oled.size, "black")
            draw = ImageDraw.Draw(img)
            draw_function(draw)
            self.oled.display(img.convert(self.oled.mode))

    def display_image(self, image_path, resize=True, timeout=None):
        """Convenience for static files (PNG/JPG/GIF single frame)."""
        with self.lock:
            try:
                img = Image.open(image_path)
                if img.mode == "RGBA":
                    bg = Image.new("RGB", img.size, (0, 0, 0))
                    bg.paste(img, mask=img.split()[3])
                    img = bg
                if resize:
                    img = img.resize(self.oled.size, Image.LANCZOS)
                self.oled.display(img.convert(self.oled.mode))
                if timeout:
                    threading.Timer(timeout, self.clear_screen).start()
            except Exception as e:
                self.logger.error(f"Failed to load image '{image_path}': {e}")

    def display_pil(self, image, resize=False):
        """Primary hook for MenuManager: hand me a PIL.Image and I’ll show it."""
        if image is None:
            return
        with self.lock:
            img = image
            if img.mode == "RGBA":
                bg = Image.new("RGB", img.size, (0, 0, 0))
                bg.paste(img, mask=img.split()[3])
                img = bg
            if resize:
                img = img.resize(self.oled.size, Image.LANCZOS)
            self.oled.display(img.convert(self.oled.mode))

    # ---------- Transitions / animations ----------

    def slide_clock_to_menu(self, clock, menu, duration=0.4, fps=60):
        """Simple slide animation: clock out left, menu in right."""
        width, _ = self.oled.size
        frames = max(1, int(duration * fps))
        for step in range(frames + 1):
            progress = int((width * step) / frames)
            base = Image.new("RGB", self.oled.size, "black")

            clock_img = clock.render_to_image(offset_x=-progress)
            base.paste(clock_img, (0, 0), clock_img if clock_img.mode == "RGBA" else None)

            menu_img = menu.render_to_image(offset_x=width - progress)
            base.paste(menu_img, (0, 0), menu_img if menu_img.mode == "RGBA" else None)

            t0 = time.time()
            self.oled.display(base)
            # Try to keep frame pacing roughly stable
            remaining = (duration / frames) - (time.time() - t0)
            if remaining > 0:
                time.sleep(remaining)
        # Let the menu take over
        if hasattr(menu, "display_menu"):
            menu.display_menu()

    # ---------- Splash / looped gfx ----------

    def show_logo(self, duration=5):
        logo_path = self.config.get('logo_path')
        if not logo_path:
            self.logger.debug("No logo path configured.")
            return
        try:
            img = Image.open(logo_path)
        except Exception as e:
            self.logger.error(f"Could not load logo from '{logo_path}': {e}")
            return

        start = time.time()
        if getattr(img, "is_animated", False):
            while time.time() - start < duration:
                for frame in ImageSequence.Iterator(img):
                    if time.time() - start >= duration:
                        break
                    fr = frame.convert("RGB").resize(self.oled.size, Image.LANCZOS).convert(self.oled.mode)
                    self.oled.display(fr)
                    time.sleep(frame.info.get('duration', 100) / 1000.0)
        else:
            fr = img.convert(self.oled.mode).resize(self.oled.size, Image.LANCZOS)
            self.oled.display(fr)
            time.sleep(duration)

    def show_ready_gif_until_event(self, stop_event):
        path = self.config.get('ready_loop_path')  # was ready_gif_path
        if not path:
            self.logger.error("ready_loop_path not set in display config.")
            return
        try:
            gif = Image.open(path)
        except Exception as e:
            self.logger.error(f"Could not load ready loop GIF: {e}")
            return

        self.logger.info("Displaying ready.gif in a loop until event set.")
        while not stop_event.is_set():
            for frame in ImageSequence.Iterator(gif):
                if stop_event.is_set():
                    return
                fr = frame.convert("RGB").resize(self.oled.size, Image.LANCZOS).convert(self.oled.mode)
                self.oled.display(fr)
                time.sleep(frame.info.get('duration', 100) / 1000.0)

    # ---------- Lifecycle ----------

    def stop_mode(self):
        """Clear screen when a mode using DisplayManager ends."""
        self.clear_screen()
        self.logger.info("DisplayManager: cleared display.")
