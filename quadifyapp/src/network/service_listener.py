# src/network/service_listener.py

import os
import time
import logging
from typing import Optional, Dict, Any, List, Iterable

import requests

logger = logging.getLogger(__name__)

# Overrideable via env
VOLUMIO_HOST = os.environ.get("VOLUMIO_HOST", "http://localhost:3000")


def _to_icon_url(albumart: Optional[str]) -> Optional[str]:
    """
    Build a usable absolute URL for an albumart/icon path returned by Volumio.
    Volumio often returns paths like '/albumart?sourceicon=...'.
    """
    if not albumart:
        return None
    return f"{VOLUMIO_HOST}{albumart}" if albumart.startswith("/") else albumart


def _safe_label(name: Optional[str]) -> str:
    """Stable, filesystem-safe label for filenames/keys."""
    n = name or "UNKNOWN"
    return n.upper().replace(" ", "_").replace("/", "_")


def _iter_services(navigation: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    """
    Yield 'service rows' from Volumio navigation. Handles both:
    1) navigation.lists[] where each list has items[]
    2) navigation.lists[] where each list IS the item (no 'items' key)
    """
    lists = (navigation or {}).get("lists") or []
    for group in lists:
        items = group.get("items", None)
        if items is not None:
            for it in items or []:
                yield it
        else:
            # Treat the group itself as a service row
            yield group


def _normalise_entry(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalise a Volumio navigation entry (either a group or an item)
    into the fields we care about.
    """
    name = item.get("name") or item.get("title") or "UNKNOWN"
    # plugin/service key may appear as plugin_name or service depending on plugin/screen
    plugin = item.get("plugin_name") or item.get("service") or "unknown"
    uri = item.get("uri") or ""
    albumart = item.get("albumart")
    icon_url = _to_icon_url(albumart)
    label = _safe_label(name)

    return {
        "name": name,
        "plugin": plugin,
        "uri": uri,
        "albumart": albumart,
        "icon_url": icon_url,
        "label": label,
    }


def _get_json(url: str, retries: int = 3, delay: float = 0.8) -> Dict[str, Any]:
    """
    GET url with a small warm-up retry loop. Raises on final failure.
    """
    last_err = None
    for i in range(max(1, retries)):
        try:
            r = requests.get(url, timeout=6)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if i < retries - 1:
                time.sleep(delay)
            else:
                raise last_err
    return {}  # not reached


def get_available_services() -> List[Dict[str, Any]]:
    """
    Query Volumio for available services (top-level + Music Library subfolders).

    Returns a list of dicts with:
      - name
      - plugin
      - uri
      - albumart
      - icon_url (absolute or None)
      - label  (safe stable key)

    NOTE: This function does NOT download icons. Use your icon fetcher for that.
    """
    root_url = f"{VOLUMIO_HOST}/api/v1/browse?uri="
    musiclib_url = f"{VOLUMIO_HOST}/api/v1/browse?uri=music-library"

    # Keep order of first appearance, but avoid dupes (keyed by label)
    dedup: Dict[str, Dict[str, Any]] = {}
    ordered_labels: List[str] = []

    # ---- Top-level services
    logger.info("Requesting Volumio root music services from: %s", root_url)
    try:
        data = _get_json(root_url, retries=3, delay=0.8)
        for item in _iter_services(data.get("navigation", {})):
            entry = _normalise_entry(item)
            lbl = entry["label"]
            if lbl not in dedup:
                dedup[lbl] = entry
                ordered_labels.append(lbl)
    except Exception as e:
        logger.error("Failed to query Volumio API (root): %s", e)
        # Return whatever we have (likely empty) rather than raising
        return [dedup[lbl] for lbl in ordered_labels]

    # ---- Music Library subfolders/services
    has_musiclib = any(
        (dedup[lbl].get("uri") or "").lower() == "music-library" for lbl in ordered_labels
    )
    if has_musiclib:
        logger.info("Querying inside Music Library for subfolders/services.")
        try:
            ml_data = _get_json(musiclib_url, retries=3, delay=0.8)
            for item in _iter_services(ml_data.get("navigation", {})):
                entry = _normalise_entry(item)
                lbl = entry["label"]
                if lbl not in dedup:
                    dedup[lbl] = entry
                    ordered_labels.append(lbl)
        except Exception as e:
            logger.error("Failed to query Music Library: %s", e)

    logger.info("Found %d music services (including subfolders).", len(ordered_labels))
    return [dedup[lbl] for lbl in ordered_labels]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    services = get_available_services()
    print("Available services:")
    for svc in services:
        print(
            " - {:20} | Plugin: {:12} | URI: {:28} | Icon: {}".format(
                svc["name"], svc["plugin"], svc["uri"], svc.get("icon_url") or "None"
            )
        )
