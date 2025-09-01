#!/usr/bin/env python3
# src/assets/images/fetch_icons.py

import os
import sys
import json
import logging
from io import BytesIO
from typing import Dict, Any, Optional

import requests
from PIL import Image
import cairosvg

# ---- Config ----
VOLUMIO_HOST = os.environ.get("VOLUMIO_HOST", "http://localhost:3000")
ASSETS_DIR = os.environ.get("QUADIFY_ICON_DIR", "/home/volumio/Quadify/src/assets/pngs")
MANIFEST_PATH = os.environ.get("QUADIFY_ICON_MANIFEST", "/home/volumio/Quadify/src/assets/icons_manifest.json")
ICON_SIZE = int(os.environ.get("QUADIFY_ICON_SIZE", "50"))
MARGIN_RATIO = float(os.environ.get("QUADIFY_ICON_MARGIN", "1.2"))  # >1 leaves margin, 1 = tight

# Allow "from network.service_listener import get_available_services"
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))
from network.service_listener import get_available_services  # noqa


# ---- Logging ----
logger = logging.getLogger("icon_fetcher")
if not logger.handlers:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(h)
logger.setLevel(logging.INFO)


# ---- Helpers ----
def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def sanitise_label(name: str) -> str:
    label = (name or "UNKNOWN").strip().upper()
    for ch in (" ", "/", "\\", ":", ";", "|", "(", ")", "[", "]", "{", "}", ","):
        label = label.replace(ch, "_")
    while "__" in label:
        label = label.replace("__", "_")
    return label or "UNKNOWN"


def is_svg_from_headers_or_url(url: str, headers: Dict[str, str], body_head: bytes) -> bool:
    ct = (headers.get("Content-Type") or "").lower()
    if "image/svg" in ct or "svg+xml" in ct:
        return True
    # Volumio often returns SVG via albumart?sourceicon=...*.svg
    if ".svg" in url.lower():
        return True
    # Content sniff (cheap): look for <svg in first bytes
    head = body_head.lower()
    return b"<svg" in head


def trim_icon(img: Image.Image, margin_ratio: float = 1.2) -> Image.Image:
    """
    Crop transparent padding based on alpha bbox, then expand bbox by margin_ratio.
    >1 keeps some margin around the icon; 1 is tight; <1 is extra tight (usually not desired).
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    alpha = img.split()[3]
    bbox = alpha.getbbox()
    if not bbox:
        return img

    left, top, right, bottom = bbox
    w = right - left
    h = bottom - top

    cx = left + w / 2.0
    cy = top + h / 2.0

    # New width/height with margin
    new_w = min(img.width, max(1, int(w * margin_ratio)))
    new_h = min(img.height, max(1, int(h * margin_ratio)))

    new_left = max(0, int(cx - new_w / 2))
    new_top = max(0, int(cy - new_h / 2))
    new_right = min(img.width, new_left + new_w)
    new_bottom = min(img.height, new_top + new_h)

    return img.crop((new_left, new_top, new_right, new_bottom))


def fit_square(img: Image.Image, size: int) -> Image.Image:
    img = img.copy()
    img.thumbnail((size, size), Image.Resampling.LANCZOS)
    bg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    x = (size - img.width) // 2
    y = (size - img.height) // 2
    bg.paste(img, (x, y))
    return bg


def fetch_bytes(url: str, timeout: float = 10.0) -> (bytes, Dict[str, str]):
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    # grab first 2KB for sniffing
    body = resp.content
    head = body[:2048] if body else b""
    return body, dict(resp.headers), head


def load_image_any(url: str) -> Optional[Image.Image]:
    """
    Fetch and decode an image from any format. Converts SVG via cairosvg, all others via PIL.
    Returns an RGBA image or None on failure.
    """
    try:
        body, headers, head = fetch_bytes(url)
    except Exception as e:
        logger.warning(f"Download failed for {url}: {e}")
        return None

    try:
        if is_svg_from_headers_or_url(url, headers, head):
            # Render SVG to PNG bytes at a generous size, then we’ll trim/fit.
            # Using ICON_SIZE*2 to keep some resolution before shrink.
            png = cairosvg.svg2png(bytestring=body, output_width=ICON_SIZE * 2, output_height=ICON_SIZE * 2)
            img = Image.open(BytesIO(png)).convert("RGBA")
        else:
            img = Image.open(BytesIO(body))
            # Handle palettes/transparency nicely
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            else:
                img = img.copy().convert("RGBA")
        return img
    except Exception as e:
        logger.warning(f"Failed to decode {url}: {e}")
        return None


def normalise_icon(url: str, size: int = ICON_SIZE, margin_ratio: float = MARGIN_RATIO) -> Optional[Image.Image]:
    img = load_image_any(url)
    if img is None:
        return None
    try:
        img = trim_icon(img, margin_ratio=margin_ratio)
        img = fit_square(img, size)
        return img
    except Exception as e:
        logger.warning(f"Failed to process image from {url}: {e}")
        return None


def write_manifest(manifest: Dict[str, Any], path: str = MANIFEST_PATH) -> None:
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


# ---- Main flow ----
def main() -> Dict[str, Any]:
    """
    Discover services via Volumio, fetch & normalise icons into ASSETS_DIR,
    write a manifest JSON, and return it (label -> file path).
    """
    ensure_dir(ASSETS_DIR)

    try:
        services = get_available_services()
    except Exception as e:
        logger.error(f"get_available_services() failed: {e}")
        services = []

    manifest: Dict[str, Any] = {"icons": {}, "size": ICON_SIZE}
    seen_labels = set()

    for svc in services:
        name = svc.get("name") or svc.get("plugin") or "UNKNOWN"
        label = sanitise_label(name)
        if label in seen_labels:
            continue
        seen_labels.add(label)

        icon_url = svc.get("icon_url") or svc.get("albumart")
        if not icon_url:
            logger.info(f"No icon URL for {label}, skipping.")
            continue

        if icon_url.startswith("/"):
            icon_url = VOLUMIO_HOST.rstrip("/") + icon_url

        save_path = os.path.join(ASSETS_DIR, f"{label}.png")

        if os.path.exists(save_path):
            # Already cached—still add to manifest
            manifest["icons"][label] = {"path": save_path, "source": icon_url}
            logger.info(f"Icon already cached for {label}: {save_path}")
            continue

        img = normalise_icon(icon_url, size=ICON_SIZE, margin_ratio=MARGIN_RATIO)
        if img is None:
            logger.warning(f"Icon fetch/convert failed for {label} ({icon_url})")
            continue

        try:
            img.save(save_path, format="PNG")
            manifest["icons"][label] = {"path": save_path, "source": icon_url}
            logger.info(f"Saved icon for {label}: {save_path}")
        except Exception as e:
            logger.warning(f"Failed to save {label} -> {save_path}: {e}")

    write_manifest(manifest, MANIFEST_PATH)
    logger.info(f"Wrote icon manifest: {MANIFEST_PATH} ({len(manifest['icons'])} entries)")
    return manifest


if __name__ == "__main__":
    main()
