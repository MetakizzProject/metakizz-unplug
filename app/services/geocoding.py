"""Geocoding service for the Buddy Finder.

Resolves "city, country" → (latitude, longitude) using OpenStreetMap's
Nominatim (free, no API key). Used to drop pins on the public map.

Rate limit: Nominatim's terms of use require ≤1 req/sec and a custom
User-Agent. We pass a contact email in the User-Agent and rely on
process-local caching to avoid hammering the service. For our scale
(2000+ ambassadors, geocode at publish time once per post), this is
plenty.
"""

import logging
import time
import threading
from typing import Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "MetaKizz Buddy Finder (info@metakizzproject.com)"

# Process-local cache to avoid duplicate calls within the same process.
# Key: (city_lower, country_lower). Value: (lat, lng) or (None, None).
_cache: Dict[Tuple[str, str], Tuple[Optional[float], Optional[float]]] = {}
_cache_lock = threading.Lock()
_last_request_at = 0.0


def geocode_city(city, country_code=None, timeout=5):
    """Resolve a city + country to (lat, lng).

    Returns (lat, lng) on success or (None, None) on failure (network
    error, no results, malformed response). Best-effort — never raises.
    """
    global _last_request_at

    if not city or not city.strip():
        return (None, None)

    city_clean = city.strip().lower()
    country_clean = (country_code or "").strip().lower()
    cache_key = (city_clean, country_clean)

    with _cache_lock:
        if cache_key in _cache:
            return _cache[cache_key]

    # Honor Nominatim's 1 rps limit.
    now = time.time()
    elapsed = now - _last_request_at
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)

    params = {
        "q": city,
        "format": "json",
        "limit": 1,
    }
    if country_code:
        params["countrycodes"] = country_code.lower()

    try:
        resp = requests.get(
            NOMINATIM_URL,
            params=params,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            timeout=timeout,
        )
        _last_request_at = time.time()
        if resp.status_code != 200:
            logger.warning("nominatim non-200: %s for %s", resp.status_code, city)
            result = (None, None)
        else:
            data = resp.json() or []
            if not data:
                logger.info("nominatim no results for city=%r country=%s", city, country_code)
                result = (None, None)
            else:
                first = data[0]
                try:
                    lat = float(first.get("lat"))
                    lng = float(first.get("lon"))
                    result = (lat, lng)
                except (TypeError, ValueError):
                    result = (None, None)
    except Exception:
        logger.exception("nominatim request failed for city=%r", city)
        result = (None, None)

    with _cache_lock:
        _cache[cache_key] = result
    return result
