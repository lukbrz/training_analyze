import json
import time
import urllib.parse
import urllib.request

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "training_analyze/1.0 (personal Garmin activity analyzer)"
MIN_REQUEST_INTERVAL_S = 1.0  # Nominatim's usage policy caps public use at ~1 req/s

_last_request_time = 0.0


def reverse_geocode(lat: float, lon: float, timeout: float = 5.0) -> str | None:
    """Look up a human-readable place name for a GPS coordinate via OpenStreetMap's
    Nominatim reverse-geocoding API. Returns None on any network/parse failure or if no
    usable address component is found — this is a nice-to-have label, never a reason to
    fail route creation."""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < MIN_REQUEST_INTERVAL_S:
        time.sleep(MIN_REQUEST_INTERVAL_S - elapsed)

    params = urllib.parse.urlencode(
        {"format": "json", "lat": lat, "lon": lon, "zoom": 10, "addressdetails": 1}
    )
    request = urllib.request.Request(f"{NOMINATIM_URL}?{params}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    finally:
        _last_request_time = time.monotonic()

    address = data.get("address", {})
    for key in ("city", "town", "village", "suburb", "municipality", "county"):
        if address.get(key):
            return address[key]
    display_name = data.get("display_name")
    return display_name.split(",")[0] if display_name else None
