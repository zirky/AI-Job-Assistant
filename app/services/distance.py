import json
import math
import os
import re
import unicodedata
from pathlib import Path
from typing import Dict, Optional, Tuple
import httpx

USER_AGENT = "SelfHostedJobAssistant/1.0 (contact: local-selfhosted@example.org)"

_CACHE_PATH = Path(
    "/app/data/geo_cache.json" if Path("/app/data").exists() else "/tmp/job_assistant_geo_cache.json"
)
_GEO_CACHE: Dict[str, Tuple[float, float]] = {}
_GEO_CACHE_LOADED = False
_ROUTE_CACHE: Dict[str, Tuple[int, float]] = {}


def _norm_key(place: str) -> str:
    cleaned = re.sub(
        r",?\s*(czechia|czech republic|česká republika|ceska republika)\s*$",
        "",
        (place or "").strip(),
        flags=re.IGNORECASE,
    )
    norm = unicodedata.normalize("NFKD", cleaned)
    ascii_str = norm.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"\s+", " ", ascii_str).strip()


def _load_geo_cache() -> None:
    global _GEO_CACHE_LOADED, _GEO_CACHE
    if _GEO_CACHE_LOADED:
        return
    _GEO_CACHE_LOADED = True
    try:
        if _CACHE_PATH.exists():
            raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, (list, tuple)) and len(v) == 2:
                        _GEO_CACHE[str(k)] = (float(v[0]), float(v[1]))
    except Exception:
        pass


def _save_geo_cache() -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_GEO_CACHE, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, _CACHE_PATH)
    except Exception:
        pass


async def _query_nominatim(client: httpx.AsyncClient, query: str) -> Optional[Tuple[float, float]]:
    resp = await client.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": f"{query}, Czechia", "format": "json", "limit": 1, "countrycodes": "cz"},
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    data = resp.json()
    if data:
        return float(data[0]["lat"]), float(data[0]["lon"])
    return None


async def _query_photon(client: httpx.AsyncClient, query: str) -> Optional[Tuple[float, float]]:
    resp = await client.get(
        "https://photon.komoot.io/api/",
        params={"q": f"{query}, Czechia", "limit": 1},
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    data = resp.json()
    features = data.get("features") or []
    if features:
        coords = (features[0].get("geometry") or {}).get("coordinates") or []
        if len(coords) >= 2:
            # GeoJSON má [lon, lat]
            return float(coords[1]), float(coords[0])
    return None


async def geocode_location(place: str) -> Optional[Tuple[float, float]]:
    if not place or not place.strip():
        return None

    _load_geo_cache()
    key = _norm_key(place)
    if not key:
        return None
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]

    query = re.sub(
        r",?\s*(czechia|czech republic|česká republika|ceska republika)\s*$",
        "",
        place.strip(),
        flags=re.IGNORECASE,
    ).strip()

    async with httpx.AsyncClient(timeout=8.0) as client:
        for provider in (_query_nominatim, _query_photon):
            try:
                coords = await provider(client, query)
                if coords is not None:
                    _GEO_CACHE[key] = coords
                    _save_geo_cache()
                    return coords
            except Exception:
                continue

    return None


async def estimate_commute(
    start_lat: float, start_lon: float, end_lat: float, end_lon: float
) -> Tuple[Optional[int], Optional[float]]:
    if round(start_lat, 4) == round(end_lat, 4) and round(start_lon, 4) == round(end_lon, 4):
        return 5, 1.5

    cache_key = f"{round(start_lat,4)},{round(start_lon,4)}->{round(end_lat,4)},{round(end_lon,4)}"
    if cache_key in _ROUTE_CACHE:
        return _ROUTE_CACHE[cache_key]

    url = f"https://router.project-osrm.org/route/v1/driving/{start_lon},{start_lat};{end_lon},{end_lat}"
    params = {"overview": "false"}
    headers = {"User-Agent": USER_AGENT}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            routes = data.get("routes", [])
            if routes:
                seconds = float(routes[0]["duration"])
                meters = float(routes[0]["distance"])
                mins = max(1, round(seconds / 60))
                km = round(meters / 1000.0, 1)
                _ROUTE_CACHE[cache_key] = (mins, km)
                return mins, km
    except Exception:
        pass

    r = 6371.0
    dlat = math.radians(end_lat - start_lat)
    dlon = math.radians(end_lon - start_lon)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(start_lat))
        * math.cos(math.radians(end_lat))
        * math.sin(dlon / 2) ** 2
    )
    km = round(2 * r * math.asin(math.sqrt(a)) * 1.25, 1)
    mins = max(1, round((km / 50.0) * 60))
    _ROUTE_CACHE[cache_key] = (mins, km)
    return mins, km


async def estimate_commute_minutes(
    start_lat: float, start_lon: float, end_lat: float, end_lon: float
) -> Optional[int]:
    mins, _ = await estimate_commute(start_lat, start_lon, end_lat, end_lon)
    return mins