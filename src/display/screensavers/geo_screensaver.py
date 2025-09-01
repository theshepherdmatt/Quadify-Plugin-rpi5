import random
import time
import threading
from PIL import Image, ImageDraw


class GeoScreensaver:
    """
    A Python-based screensaver that displays random geometric shapes.

    Basic idea:
      - A set of shapes is generated (circles, rectangles, triangles, etc.).
      - Each shape has a position, velocity, colour, and possibly other attributes.
      - Each update, the shapes move (bounce or wrap).
      - When resetting, all shapes are regenerated randomly.
    """

    def __init__(self, display_manager, update_interval=0.04, num_shapes=15):
        """
        :param display_manager: An instance of your DisplayManager
                                (must have .oled.width, .oled.height, and .oled.display()).
        :param update_interval: Seconds between frames (0.04 ~ 40ms).
        :param num_shapes: How many shapes to generate.
        """
        self.display_manager = display_manager
        self.update_interval = update_interval

        # Dimensions from the display
        self.width = display_manager.oled.width
        self.height = display_manager.oled.height

        self.num_shapes = num_shapes
        self.shapes = []

        self.is_running = False
        self.thread = None

    def reset_animation(self):
        """Clears and regenerates random shapes."""
        self.shapes = []

        for _ in range(self.num_shapes):
            shape_type = random.choice(["circle", "rectangle", "triangle"])
            x = random.randint(0, self.width - 1)
            y = random.randint(0, self.height - 1)
            dx = random.choice([-1, 1]) * random.uniform(0.5, 2)
            dy = random.choice([-1, 1]) * random.uniform(0.5, 2)

            # Random size (radius or side length)
            size = random.randint(5, 15)

            # Random colour (for PIL, (R,G,B))
            colour = (
                random.randint(50, 255),
                random.randint(50, 255),
                random.randint(50, 255),
            )

            self.shapes.append({
                "type": shape_type,
                "x": x,
                "y": y,
                "dx": dx,
                "dy": dy,
                "size": size,
                "colour": colour
            })

    def start_screensaver(self):
        """Begin the main loop in a background thread."""
        if self.is_running:
            return
        self.is_running = True

        # Reset to fresh state
        self.reset_animation()

        # Start a daemon thread
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def stop_screensaver(self):
        """Stop the loop and wait for thread to finish."""
        self.is_running = False
        if self.thread:
            self.thread.join()
            self.thread = None

    def run(self):
        """
        The main loop: calls `refresh_action()`
        every self.update_interval until stopped.
        """
        while self.is_running:
            self.refresh_action()
            time.sleep(self.update_interval)

    def refresh_action(self):
        """
        Draws each shape to an image, updates their positions, and
        displays the image on the screen.
        """
        # 1) Prepare an empty image + draw
        img = Image.new("RGB", (self.width, self.height), "black")
        draw = ImageDraw.Draw(img)

        # 2) Update and draw shapes
        for shape in self.shapes:
            # Move the shape
            shape["x"] += shape["dx"]
            shape["y"] += shape["dy"]

            # Bounce shapes off edges
            if shape["x"] < 0 or shape["x"] > self.width:
                shape["dx"] *= -1
            if shape["y"] < 0 or shape["y"] > self.height:
                shape["dy"] *= -1

            # Re-clamp after bounce
            shape["x"] = max(0, min(self.width, shape["x"]))
            shape["y"] = max(0, min(self.height, shape["y"]))

            # Draw shape based on its type
            size = shape["size"]
            x = shape["x"]
            y = shape["y"]
            col = shape["colour"]

            if shape["type"] == "circle":
                # Draw a circle (ellipse)
                # Coords: top-left, bottom-right
                draw.ellipse(
                    [x - size, y - size, x + size, y + size],
                    fill=col
                )
            elif shape["type"] == "rectangle":
                draw.rectangle(
                    [x - size, y - size, x + size, y + size],
                    fill=col
                )
            elif shape["type"] == "triangle":
                # Equilateral-ish triangle
                half_side = size
                points = [
                    (x, y - size),
                    (x - half_side, y + size),
                    (x + half_side, y + size)
                ]
                draw.polygon(points, fill=col)

        # 3) Convert and display
        final_img = img.convert(self.display_manager.oled.mode)
        with self.display_manager.lock:
            self.display_manager.oled.display(final_img)
