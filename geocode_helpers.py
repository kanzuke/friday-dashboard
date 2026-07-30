"""Geocoding helper functions for the Travely map feature.

Provides improved geocoding with query cleaning and fallback strategies
to maximize Nominatim success rate on activity descriptions.
"""
import re
import hashlib
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional


GEOCODE_CACHE_DIR: Path = None  # set by app.py at import time


def init_cache_dir(cache_dir: Path):
    """Set the geocode cache directory. Called from app.py."""
    global GEOCODE_CACHE_DIR
    GEOCODE_CACHE_DIR = cache_dir


def _nominatim_geocode(query: str) -> Optional[dict]:
    """Geocode an address using Nominatim (OpenStreetMap, free, no API key).

    Returns {"lat": float, "lon": float, "display_name": str} or None.
    Respects Nominatim usage policy: max 1 request per second, custom User-Agent.
    """
    if not query or not query.strip():
        return None
    encoded = urllib.parse.urlencode({
        "q": query.strip(),
        "format": "json",
        "limit": 1,
        "addressdetails": 0,
    })
    url = f"https://nominatim.openstreetmap.org/search?{encoded}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "HermesDashboard/1.0 (dashboard@wired-lan.fr)",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            results = json.loads(resp.read().decode("utf-8"))
            if results and isinstance(results, list) and len(results) > 0:
                r = results[0]
                return {
                    "lat": float(r["lat"]),
                    "lon": float(r["lon"]),
                    "display_name": r.get("display_name", query),
                }
    except Exception as e:
        print(f"[GEOCODE] Nominatim failed for '{query}': {e}")
    return None


def _geocode_cached(query: str) -> Optional[dict]:
    """Geocode with disk cache to avoid repeated Nominatim calls.

    Cache files are stored as JSON in travely/cache/geocode/.
    Each file is named by the MD5 hash of the query.
    """
    if not query or not query.strip():
        return None
    if GEOCODE_CACHE_DIR is None:
        return None
    GEOCODE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.md5(query.encode("utf-8")).hexdigest()
    cache_file = GEOCODE_CACHE_DIR / f"{cache_key}.json"

    # Check cache first
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass

    # Not in cache, call Nominatim (rate-limited: 1 req/sec)
    time.sleep(1.0)  # Nominatim usage policy
    result = _nominatim_geocode(query)
    if result:
        try:
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except IOError:
            pass
    return result


def clean_geocode_query(query: str) -> str:
    """Clean up an activity text for better geocoding results.

    Removes parenthetical content, generic prefixes, and normalizes whitespace.
    Also extracts place names from patterns like "Arrivée à Tromsø" or
    "Balade dans le bourg de Crozon".
    """
    text = query.strip()
    # Remove parenthetical content: "(cratère lac)" -> ""
    text = re.sub(r'\([^)]*\)', '', text)
    # Remove content after "+" or "—" or " - " (compound activities)
    # e.g. "Chasse aux aurores boréales (terrestre) + Centre Polaire" -> "Chasse aux aurores boréales"
    text = re.split(r'\s*[+—-]\s*', text)[0]
    # Try to extract place name after "à ", "au ", "de ", "du ", "de la ", "des " patterns
    # e.g. "Arrivée à Tromsø" -> "Tromsø"
    # e.g. "Balade dans le bourg de Crozon" -> "Crozon"
    place_match = re.search(
        r'\b(?:à|au|de|du|de la|des|dans le|dans la|sur le|sur la)\s+(.+)',
        text
    )
    if place_match:
        extracted = place_match.group(1).strip()
        # Only use the extraction if it's reasonably short (a place name, not a sentence)
        if len(extracted) < 60 and not any(w in extracted.lower() for w in ('et ', ', ', 'puis', 'avec', 'pour', 'vers')):
            text = extracted
    # Remove common French activity prefixes (longest first)
    prefixes = [
        'Randonnée ', 'Rando ', 'Marche ', 'Visite du ', 'Visite de la ',
        'Visite des ', 'Visite de ', 'Observation des ', 'Observation du ',
        'Baignade ', 'Promenade ', 'Tour de ', 'Découverte de ', 'Découverte du ',
        'Balade ', 'Arrivée ', 'Départ ', 'Retour ', 'Installation ',
        'Visite ', 'Découverte ', 'Observation ', 'Excursion ', 'Soirée ',
    ]
    for p in prefixes:
        if text.startswith(p):
            text = text[len(p):]
            break
    # Clean whitespace
    text = ' '.join(text.split())
    return text.strip() or query.strip()


def geocode_with_fallbacks(query: str, skip_destination_fallback: bool = False) -> Optional[dict]:
    """Try geocoding with progressively simpler queries.

    1. Full query (activity + destination)
    2. Cleaned activity + destination
    3. Just the cleaned activity name

    The 'just destination' fallback is skipped by default for activity points
    since it returns the destination coordinates (already shown as first marker).
    """
    if not query or not query.strip():
        return None

    # Try full query first
    result = _geocode_cached(query)
    if result:
        return result

    # If query contains a comma, try progressively simpler queries
    parts = [p.strip() for p in query.split(',') if p.strip()]
    if len(parts) >= 2:
        destination = parts[-1]  # last part is usually the destination
        activity = parts[0]      # first part is the activity
        cleaned_activity = clean_geocode_query(activity)
        if cleaned_activity and cleaned_activity != activity:
            # Try cleaned activity + destination
            attempt = f"{cleaned_activity}, {destination}"
            result = _geocode_cached(attempt)
            if result:
                return result
        # Try just the cleaned activity name
        if cleaned_activity:
            result = _geocode_cached(cleaned_activity)
            if result:
                return result
        # Try a few more specific combinations with the cleaned activity
        if cleaned_activity and len(parts) >= 3:
            mid = parts[1]  # region or country
            attempt = f"{cleaned_activity}, {mid}"
            result = _geocode_cached(attempt)
            if result:
                return result
        # As very last resort, try just the destination (but only if explicitly allowed)
        if not skip_destination_fallback:
            result = _geocode_cached(destination)
            if result:
                return result

    return None