from PIL import Image
import os

ICON_DIR = '/home/volumio/Quadify/src/assets/pngs'
SIZE = (50, 50)
FILES = ['BACK.png', 'CONFIG.png']

for fname in FILES:
    fpath = os.path.join(ICON_DIR, fname)
    if not os.path.exists(fpath):
        print(f"Not found: {fpath}")
        continue
    img = Image.open(fpath).convert('RGBA')

    # Resize the icon (the symbol) to something like 32x32 for a margin (adjust as needed)
    icon_size = 32
    img = img.resize((icon_size, icon_size), Image.LANCZOS)

    # Create a transparent background
    new_img = Image.new('RGBA', SIZE, (0, 0, 0, 0))
    # Paste the icon in the centre
    pos = ((SIZE[0] - icon_size)//2, (SIZE[1] - icon_size)//2)
    new_img.paste(img, pos, img)
    new_img.save(fpath)
    print(f"Centred with padding and saved: {fpath}")
