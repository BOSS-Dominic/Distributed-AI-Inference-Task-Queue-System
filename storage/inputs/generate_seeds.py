"""
Generate synthetic seed images --- for end-to-end MVP validation.
Produces test images with varying sizes, colours and patterns so that
ResNet-18 returns meaningful classification labels.

Alternative: manually download seed images from COCO 2017 validation set.
"""

import os
from PIL import Image, ImageDraw


SEEDS_DIR = os.path.join(os.path.dirname(__file__), "seeds")
os.makedirs(SEEDS_DIR, exist_ok=True)


def create_test_image(filename, size, color, pattern="solid"):
    """Create a synthetic test image."""
    img = Image.new("RGB", size, color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    w, h = size

    if pattern == "solid":
        # Monochrome fill
        draw.rectangle([(0, 0), (w, h)], fill=color)

    elif pattern == "gradient":
        # Horizontal gradient
        for x in range(w):
            r = int(color[0] * (x / w))
            g = int(color[1] * (x / w))
            b = int(color[2] * (x / w))
            draw.line([(x, 0), (x, h)], fill=(r, g, b))

    elif pattern == "checkerboard":
        # Checkerboard pattern
        sq = 64
        for i in range(0, w, sq):
            for j in range(0, h, sq):
                c = color if (i // sq + j // sq) % 2 == 0 else (255 - color[0], 255 - color[1], 255 - color[2])
                draw.rectangle([(i, j), (i + sq - 1, j + sq - 1)], fill=c)

    elif pattern == "stripes":
        # Vertical stripes
        sw = 32
        for x in range(0, w, sw):
            c = color if (x // sw) % 2 == 0 else (255, 255, 255)
            draw.rectangle([(x, 0), (x + sw - 1, h)], fill=c)

    elif pattern == "circles":
        # Concentric circles
        cx, cy = w // 2, h // 2
        for r in range(10, max(w, h) // 2, 30):
            c = (
                (color[0] + r * 2) % 256,
                (color[1] + r * 3) % 256,
                (color[2] + r * 5) % 256,
            )
            draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], outline=c, width=3)

    elif pattern == "crosshair":
        # Crosshair pattern
        cx, cy = w // 2, h // 2
        draw.line([(0, cy), (w, cy)], fill=color, width=5)
        draw.line([(cx, 0), (cx, h)], fill=color, width=5)
        for r in range(30, min(w, h) // 2, 40):
            draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], outline=color, width=2)

    elif pattern == "dots":
        # Dotted pattern
        for x in range(20, w, 40):
            for y in range(20, h, 40):
                c = (
                    (color[0] * x) % 256,
                    (color[1] * y) % 256,
                    (color[2] * (x + y)) % 256,
                )
                draw.ellipse([(x - 10, y - 10), (x + 10, y + 10)], fill=c)

    elif pattern == "landscape":
        # Fake landscape: sky + ground + sun
        draw.rectangle([(0, 0), (w, h // 2)], fill=(135, 206, 235))  # Sky
        draw.rectangle([(0, h // 2), (w, h)], fill=(34, 139, 34))     # Grass
        sun_r = min(w, h) // 8
        draw.ellipse([(w * 3 // 4 - sun_r, h // 6 - sun_r),
                       (w * 3 // 4 + sun_r, h // 6 + sun_r)], fill=(255, 223, 0))

    elif pattern == "texture":
        # Random-ish noise texture
        import random
        random.seed(hash(filename))
        for x in range(0, w, 8):
            for y in range(0, h, 8):
                r = (color[0] + random.randint(-30, 30)) % 256
                g = (color[1] + random.randint(-30, 30)) % 256
                b = (color[2] + random.randint(-30, 30)) % 256
                draw.rectangle([(x, y), (x + 7, y + 7)], fill=(r, g, b))

    img.save(os.path.join(SEEDS_DIR, filename))
    print(f"  Created: {filename} ({size[0]}x{size[1]}) - {pattern}")


# ----------------------------------------------------------------
# Batch generation
# ----------------------------------------------------------------

print("Generating synthetic seed images...\n")

SEEDS = [
    # (filename, size, colour, pattern)
    ("cat_like_01.jpg",       (224, 224),   (180, 120, 80),  "solid"),
    ("dog_scene_02.jpg",      (640, 480),   (200, 130, 50),  "landscape"),
    ("vehicle_01.jpg",        (800, 600),   (60, 60, 180),   "stripes"),
    ("bird_shape_01.jpg",     (224, 224),   (120, 180, 200), "circles"),
    ("food_plate_01.jpg",     (320, 320),   (200, 80, 80),   "checkerboard"),
    ("indoor_scene_01.jpg",   (640, 480),   (180, 180, 140), "gradient"),
    ("person_face_01.jpg",    (224, 224),   (220, 170, 130), "dots"),
    ("outdoor_park_01.jpg",   (1920, 1080), (80, 150, 80),   "landscape"),
    ("object_test_01.jpg",    (640, 480),   (200, 200, 200), "crosshair"),
    ("animal_form_01.jpg",    (500, 500),   (150, 100, 50),  "texture"),
    ("building_view_01.jpg",  (1024, 768),  (100, 100, 180), "solid"),
    ("beach_sunset_01.jpg",   (640, 480),   (255, 140, 60),  "gradient"),
    ("mountain_peak_01.jpg",  (800, 600),   (100, 120, 140), "stripes"),
    ("city_street_01.jpg",    (640, 480),   (80, 80, 80),    "checkerboard"),
    ("fruit_basket_01.jpg",   (320, 320),   (255, 100, 50),  "circles"),
]

for filename, size, color, pattern in SEEDS:
    create_test_image(filename, size, color, pattern)

print(f"\nGenerated {len(SEEDS)} seed images in: {SEEDS_DIR}")
print("   Replace with real COCO/ImageNet images for production experiments.")
