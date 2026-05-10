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

# Hardcoded country centers (ISO 3166-1 alpha-2 → lat, lng). Used to drop
# aggregate "X dancers in this country" pins on the map without hitting
# Nominatim 200 times. Top countries first; everything else falls through
# to a Nominatim lookup and gets cached.
COUNTRY_CENTERS = {
    "ES": (40.4168, -3.7038),    # Spain (Madrid)
    "FR": (46.2276, 2.2137),     # France
    "IT": (41.8719, 12.5674),    # Italy (Rome)
    "DE": (51.1657, 10.4515),    # Germany
    "PT": (39.3999, -8.2245),    # Portugal
    "NL": (52.1326, 5.2913),     # Netherlands
    "BE": (50.5039, 4.4699),     # Belgium
    "CH": (46.8182, 8.2275),     # Switzerland
    "AT": (47.5162, 14.5501),    # Austria
    "GB": (54.7024, -3.2766),    # UK
    "IE": (53.4129, -8.2439),    # Ireland
    "PL": (51.9194, 19.1451),    # Poland
    "CZ": (49.8175, 15.473),     # Czech Republic
    "DK": (56.2639, 9.5018),     # Denmark
    "SE": (60.1282, 18.6435),    # Sweden
    "NO": (60.4720, 8.4689),     # Norway
    "FI": (61.9241, 25.7482),    # Finland
    "GR": (39.0742, 21.8243),    # Greece
    "RO": (45.9432, 24.9668),    # Romania
    "HU": (47.1625, 19.5033),    # Hungary
    "TR": (38.9637, 35.2433),    # Turkey
    "RU": (61.5240, 105.3188),   # Russia
    "UA": (48.3794, 31.1656),    # Ukraine
    "US": (37.0902, -95.7129),   # United States
    "CA": (56.1304, -106.3468),  # Canada
    "MX": (23.6345, -102.5528),  # Mexico
    "BR": (-14.2350, -51.9253),  # Brazil
    "AR": (-38.4161, -63.6167),  # Argentina
    "CL": (-35.6751, -71.5430),  # Chile
    "CO": (4.5709, -74.2973),    # Colombia
    "PE": (-9.1900, -75.0152),   # Peru
    "AU": (-25.2744, 133.7751),  # Australia
    "NZ": (-40.9006, 174.8860),  # New Zealand
    "JP": (36.2048, 138.2529),   # Japan
    "ZA": (-30.5595, 22.9375),   # South Africa
    "IL": (31.0461, 34.8516),    # Israel
    "AE": (23.4241, 53.8478),    # UAE
    "AO": (-11.2027, 17.8739),   # Angola
    "CV": (16.5388, -23.0418),   # Cape Verde
    "MZ": (-18.6657, 35.5296),   # Mozambique
}


def country_center(country_code):
    """Return (lat, lng) for the centroid of a country, or None."""
    if not country_code:
        return None
    cc = country_code.strip().upper()
    return COUNTRY_CENTERS.get(cc)

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
