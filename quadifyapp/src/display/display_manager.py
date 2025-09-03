import logging
import os
import time
import threading
import yaml
from PIL import Image, ImageDraw, ImageFont, ImageSequence
from luma.core.interface.serial import spi
from luma.oled.device import ssd1322


class DisplayManager:
    def __init__(self, config=None, yaml_path=None, watch_yaml=False, watch_interval=2.0):
        """
        yaml_path: path to Quadify config.yaml. Defaults to
                   /data/plugins/system_hardware/quadify/quadifyapp/config.yaml
                   or env QUADIFY_CONFIG_YAML if set.
        watch_yaml: if True, monitor the YAML file and hot-reload rotation.
        """
        # --- Logger ---
        self.logger = logging.getLogger(self.__class__.__name__)
        self.logger.setLevel(logging.INFO)
        if not self.logger.handlers:
            ch = logging.StreamHandler()
            ch.setLevel(logging.DEBUG)
            ch.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
            self.logger.addHandler(ch)

        # --- Load YAML ---
        self.yaml_path = yaml_path or os.environ.get(
            "QUADIFY_CONFIG_YAML",
            "/data/plugins/system_hardware/quadify/quadifyapp/config.yaml"
        )
        self._yaml_mtime = None
        self.yaml_cfg = self._read_yaml(self.yaml_path)

        # Keep runtime overrides separate so live YAML edits can apply
        self._runtime_overrides = dict(config or {})

        # Merge YAML + overrides (overrides win)
        self.config = self._merge_config(self.yaml_cfg, self._runtime_overrides)

        # --- Rotation (prefer top-level display_rotate, else display.rotation) ---
        self.rotate = self._resolve_rotation(self.config)
        self.logger.info(f"Using display_rotate={self.rotate} (quadrant) from {self.yaml_path}")

        # SPI + device (SSD1322 @ 256x64)
        self.serial = spi(device=0, port=0)
        self.oled = ssd1322(self.serial, width=256, height=64, rotate=self.rotate)

        self.icons = {}
        self.lock = threading.Lock()

        # Fonts (icons are owned by MenuManager)
        self.fonts = {}
        self._load_fonts()

        # Mode change callbacks
        self.on_mode_change_callbacks = []

        # Optional YAML watcher
        self._watch_stop = None
        if watch_yaml:
            self._watch_stop = threading.Event()
            threading.Thread(target=self._watch_yaml_loop, args=(watch_interval,), daemon=True).start()

        self.logger.info("DisplayManager initialized.")

    # ---------- Config helpers ----------

    def _merge_config(self, base, overlay):
        """Shallow merge for top-level; nested 'display' merged one level deep."""
        base = dict(base or {})
        overlay = dict(overlay or {})

        out = dict(base)
        out.update({k: v for k, v in overlay.items() if k != "display"})

        # Merge display.*
        d_base = dict((base.get("display") or {}))
        d_over = dict((overlay.get("display") or {}))
        d_merged = dict(d_base)
        d_merged.update(d_over)
        if d_merged:
            out["display"] = d_merged

        return out

    def _dget(self, key, default=None):
        """
        Resolve a key either at top-level or under display.* (display.* wins).
        Useful for paths like logo_path, ready_loop_path, fonts, etc.
        """
        d = (self.config.get("display") or {}) if isinstance(self.config, dict) else {}
        if key in d:
            return d.get(key, default)
        return (self.config.get(key, default) if isinstance(self.config, dict) else default)

    # ---------- Rotation ----------

    def _resolve_rotation(self, cfg):
        """
        Prefer 'display_rotate' (0..3 or 0/90/180/270), fallback to 'display.rotation'.
        Return an int in {0,1,2,3} for luma.
        """
        raw = None
        if isinstance(cfg, dict):
            raw = cfg.get("display_rotate")
            if raw is None:
                raw = (cfg.get("display", {}) or {}).get("rotation")

        def to_quadrant(v):
            if v is None:
                return 0
            try:
                n = int(str(v).strip())
                # Accept quadrant directly
                if n in (0, 1, 2, 3):
                    return n
                # Accept degrees too
                if n in (0, 90, 180, 270):
                    return (n // 90) % 4
                return (n // 90) % 4
            except Exception:
                return 0

        q = to_quadrant(raw)
        self.logger.info(f"Display rotation config: raw={raw!r} -> quadrant={q}")
        return q

    # ---------- YAML helpers ----------

    def _read_yaml(self, path):
        try:
            data = {}
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                self._yaml_mtime = os.path.getmtime(path)
                self.logger.debug(f"Loaded YAML from {path}: {data}")
            else:
                self.logger.warning(f"YAML not found at {path}; using defaults.")
            return data
        except Exception as e:
            self.logger.error(f"Failed reading YAML '{path}': {e}")
            return {}

    def _watch_yaml_loop(self, interval):
        self.logger.info(f"Watching {self.yaml_path} for changes every {interval}s")
        while not self._watch_stop.is_set():
            try:
                if os.path.isfile(self.yaml_path):
                    mtime = os.path.getmtime(self.yaml_path)
                    if self._yaml_mtime is None:
                        self._yaml_mtime = mtime
                    elif mtime != self._yaml_mtime:
                        self._yaml_mtime = mtime
                        self._on_yaml_changed()
            except Exception as e:
                self.logger.warning(f"YAML watch error: {e}")
            time.sleep(interval)

    def _on_yaml_changed(self):
        self.logger.info(f"{self.yaml_path} changed; reloading…")
        new_yaml = self._read_yaml(self.yaml_path)
        # Replace YAML, re-merge overrides on top
        self.yaml_cfg = new_yaml
        self.config = self._merge_config(self.yaml_cfg, self._runtime_overrides)

        # Recompute rotation and reinit device if changed
        new_rotate = self._resolve_rotation(self.config)
        if new_rotate != self.rotate:
            self.logger.info(f"Applying new rotation: {self.rotate} → {new_rotate}")
            self.rotate = new_rotate
            with self.lock:
                self.oled = ssd1322(self.serial, width=256, height=64, rotate=self.rotate)

        # Reload fonts if display.fonts changed (optional but handy)
        self._load_fonts()

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
        fonts_config = (self._dget('fonts') or {})
        default_font = ImageFont.load_default()
        loaded = []
        for key, font_info in fonts_config.items():
            path = (font_info or {}).get('path')
            size = (font_info or {}).get('size', 12)
            if path and os.path.isfile(path):
                try:
                    self.fonts[key] = ImageFont.truetype(path, size=size)
                    loaded.append(key)
                except IOError as e:
                    self.logger.error(f"Error loading font '{key}' from '{path}': {e}")
                    self.fonts[key] = default_font
            else:
                self.fonts[key] = default_font
        if loaded:
            self.logger.info(f"Loaded fonts: {loaded}")
        else:
            self.logger.info("Loaded default fonts (no custom fonts found).")

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
            remaining = (duration / frames) - (time.time() - t0)
            if remaining > 0:
                time.sleep(remaining)
        if hasattr(menu, "display_menu"):
            menu.display_menu()

    # ---------- Splash / looped gfx ----------

    def show_logo(self, duration=5):
        logo_path = self._dget('logo_path')
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
        path = self._dget('ready_loop_path')
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
