import time
import threading
from PIL import Image, ImageDraw

class Clock:
    def __init__(self, display_manager, config, volumio_listener):
        self.display_manager = display_manager
        self.volumio_listener = volumio_listener
        self.config = config
        self.running = False
        self.thread = None

        self.font_y_offsets = {
            "clock_sans":    -15,
            "clock_dots":    -10,
            "clock_digital":   0,
            "clock_bold":     -5
        }
        self.font_line_spacing = {
            "clock_sans":    15,
            "clock_dots":    10,
            "clock_digital":  8,
            "clock_bold":    12
        }
        self.date_font_map = {
            "clock_sans":    "clockdate_sans",
            "clock_dots":    "clockdate_dots",
            "clock_digital": "clockdate_digital",
            "clock_bold":    "clockdate_bold"
        }

    def render_clock_image(self, offset_x=0):
        """Create a PIL image of the current clock (with optional horizontal offset)."""
        time_font_key = self.config.get("clock_font_key", "clock_digital")
        if time_font_key not in self.display_manager.fonts:
            time_font_key = "clock_digital"
        date_font_key = self.date_font_map.get(time_font_key, "clockdate_digital")
        show_seconds = self.config.get("show_seconds", False)
        time_str = time.strftime("%H:%M:%S") if show_seconds else time.strftime("%H:%M")
        show_date = self.config.get("show_date", False)
        date_str = time.strftime("%d %b %Y") if show_date else None

        y_offset = self.font_y_offsets.get(time_font_key, 0)
        line_gap = self.font_line_spacing.get(time_font_key, 10)
        w = self.display_manager.oled.width
        h = self.display_manager.oled.height

        img = Image.new("RGB", (w, h), "black")
        draw = ImageDraw.Draw(img)
        time_font = self.display_manager.fonts[time_font_key]
        date_font = self.display_manager.fonts.get(date_font_key, time_font)

        lines = []
        if time_str:
            lines.append((time_str, time_font))
        if date_str:
            lines.append((date_str, date_font))

        # Compute layout
        total_height = 0
        line_dims = []
        for (text, font) in lines:
            box = draw.textbbox((0, 0), text, font=font)
            lw = box[2] - box[0]
            lh = box[3] - box[1]
            line_dims.append((lw, lh, font))
            total_height += lh
        if len(lines) == 2:
            total_height += line_gap

        start_y = (h - total_height) // 2 + y_offset
        y_cursor = start_y
        for i, (text, font) in enumerate(lines):
            lw, lh, the_font = line_dims[i]
            x_pos = (w - lw) // 2 + offset_x
            draw.text((x_pos, y_cursor), text, font=the_font, fill="white")
            y_cursor += lh
            if i < len(lines) - 1:
                y_cursor += line_gap

        return img

    def draw_clock(self, offset_x=0):
        """Draw the clock at a specified horizontal offset (for animation)."""
        img = self.render_clock_image(offset_x)
        final_img = img.convert(self.display_manager.oled.mode)
        self.display_manager.oled.display(final_img)

    def start(self):
        """Start continuous clock updates."""
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self.update_clock, daemon=True)
            self.thread.start()
            print("Clock: Started.")

    def stop(self):
        """Stop continuous clock updates and clear the display."""
        if self.running:
            self.running = False
            self.thread.join()
            self.display_manager.clear_screen()
            print("Clock: Stopped.")

    def update_clock(self):
        """Threaded loop: updates the clock display every second."""
        while self.running:
            self.draw_clock()
            time.sleep(1)

    def toggle_play_pause(self):
        """Send toggle command to Volumio (if connected)."""
        if not self.volumio_listener or not self.volumio_listener.is_connected():
            return
        try:
            self.volumio_listener.socketIO.emit("toggle", {})
        except Exception as e:
            print(f"ClockScreen: toggle_play_pause failed => {e}")

    def slide_out_left(self, duration=0.5, fps=30):
        """Animate the clock sliding out left (for transitions)."""
        w = self.display_manager.oled.width
        frames = int(duration * fps)
        for step in range(frames + 1):
            offset = -int((w * step) / frames)
            self.draw_clock(offset_x=offset)
            time.sleep(duration / frames)

    def render_to_image(self, offset_x=0):
        """Render the clock to an image (for transition blending)."""
        return self.render_clock_image(offset_x)
