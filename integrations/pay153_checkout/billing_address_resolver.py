from __future__ import annotations

import json
import os
import random
import threading
import time
from pathlib import Path
from typing import Any

from curl_cffi import requests

_CACHE_PATH = Path(os.getenv("PAY153_BILLING_ADDRESS_CACHE", "/opt/pay153/data/billing_addresses.json"))
_CACHE_LOCK = threading.RLock()
_RESOLVE_LOCK = threading.Lock()
_NOMINATIM_LOCK = threading.Lock()
_LAST_NOMINATIM_AT = 0.0
_LAST_PICK: dict[str, str] = {}
_RECENT_PICKS: dict[str, list[str]] = {}
_PICK_LOCK = threading.Lock()
_MEMORY_CACHE: dict[str, list[dict[str, str]]] | None = None
_MEMORY_CACHE_MTIME_NS = -1

_COUNTRY_NAMES = {
    "BA": "Bosnia and Herzegovina", "US": "United States", "BR": "Brazil",
    "IN": "India", "DE": "Germany", "NL": "Netherlands", "GB": "United Kingdom",
    "FR": "France", "AU": "Australia", "JP": "Japan", "PH": "Philippines",
}

_BUILTIN_PUBLIC_ADDRESSES: dict[str, list[dict[str, str]]] = {
    "BA": [
        {"name": "Movenpick Sarajevo", "line1": "Fra Filipa Lastrica 3", "city": "Sarajevo", "state": "", "postal_code": "71000", "country": "BA", "source": "builtin_public"},
        {"name": "Hotel Old Sarajevo", "line1": "Bravadziluk 38", "city": "Sarajevo", "state": "", "postal_code": "71000", "country": "BA", "source": "builtin_public"},
        {"name": "Courtyard Sarajevo", "line1": "Skenderija 1", "city": "Sarajevo", "state": "", "postal_code": "71000", "country": "BA", "source": "builtin_public"},
        {"name": "Hotel Europe", "line1": "Vladislava Skarica 5", "city": "Sarajevo", "state": "", "postal_code": "71000", "country": "BA", "source": "builtin_public"},
        {"name": "Hotel Bosna", "line1": "Kralja Petra I Karadjordjevica 97", "city": "Banja Luka", "state": "", "postal_code": "78000", "country": "BA", "source": "builtin_public"},
        {"name": "Hotel Mellain", "line1": "Aleja Alije Izetbegovica 3", "city": "Tuzla", "state": "", "postal_code": "75000", "country": "BA", "source": "builtin_public"},
        {"name": "Residence Inn Sarajevo", "line1": "Skenderija 43", "city": "Sarajevo", "state": "", "postal_code": "71000", "country": "BA", "source": "builtin_public"},
    ],
    "US": [
        {"name": "The Plaza Hotel", "line1": "768 5th Ave", "city": "New York", "state": "NY", "postal_code": "10019", "country": "US", "source": "builtin_public"},
        {"name": "The New Yorker Hotel", "line1": "481 8th Ave", "city": "New York", "state": "NY", "postal_code": "10001", "country": "US", "source": "builtin_public"},
        {"name": "The Beverly Hills Hotel", "line1": "9641 Sunset Blvd", "city": "Beverly Hills", "state": "CA", "postal_code": "90210", "country": "US", "source": "builtin_public"},
        {"name": "Millennium Biltmore Los Angeles", "line1": "506 S Grand Ave", "city": "Los Angeles", "state": "CA", "postal_code": "90071", "country": "US", "source": "builtin_public"},
        {"name": "Palmer House", "line1": "17 E Monroe St", "city": "Chicago", "state": "IL", "postal_code": "60603", "country": "US", "source": "builtin_public"},
        {"name": "InterContinental Miami", "line1": "100 Chopin Plaza", "city": "Miami", "state": "FL", "postal_code": "33131", "country": "US", "source": "builtin_public"},
        {"name": "Omni Dallas Hotel", "line1": "555 S Lamar St", "city": "Dallas", "state": "TX", "postal_code": "75202", "country": "US", "source": "builtin_public"},
        {"name": "Palace Hotel", "line1": "2 New Montgomery St", "city": "San Francisco", "state": "CA", "postal_code": "94105", "country": "US", "source": "builtin_public"},
        {"name": "Fairmont Olympic Hotel", "line1": "411 University St", "city": "Seattle", "state": "WA", "postal_code": "98101", "country": "US", "source": "builtin_public"},
        {"name": "Willard InterContinental", "line1": "1401 Pennsylvania Ave NW", "city": "Washington", "state": "DC", "postal_code": "20004", "country": "US", "source": "builtin_public"},
    ],
    "GB": [
        {"name": "The Savoy", "line1": "Strand", "city": "London", "state": "", "postal_code": "WC2R 0EZ", "country": "GB", "source": "builtin_public"},
        {"name": "The Ritz London", "line1": "150 Piccadilly", "city": "London", "state": "", "postal_code": "W1J 9BR", "country": "GB", "source": "builtin_public"},
        {"name": "The Midland Hotel", "line1": "16 Peter St", "city": "Manchester", "state": "", "postal_code": "M60 2DS", "country": "GB", "source": "builtin_public"},
        {"name": "The Edwardian Manchester", "line1": "Free Trade Hall, Peter St", "city": "Manchester", "state": "", "postal_code": "M2 5GP", "country": "GB", "source": "builtin_public"},
        {"name": "The Grand Hotel Birmingham", "line1": "1 Church St", "city": "Birmingham", "state": "", "postal_code": "B3 2FE", "country": "GB", "source": "builtin_public"},
        {"name": "The Balmoral", "line1": "1 Princes St", "city": "Edinburgh", "state": "", "postal_code": "EH2 2EQ", "country": "GB", "source": "builtin_public"},
        {"name": "Kimpton Blythswood Square", "line1": "11 Blythswood Square", "city": "Glasgow", "state": "", "postal_code": "G2 4AD", "country": "GB", "source": "builtin_public"},
        {"name": "Titanic Hotel Liverpool", "line1": "Stanley Dock, Regent Rd", "city": "Liverpool", "state": "", "postal_code": "L3 0AN", "country": "GB", "source": "builtin_public"},
    ],
    "PH": [
        {"name": "The Manila Hotel", "line1": "One Rizal Park, Ermita", "city": "Manila", "state": "Metro Manila", "postal_code": "0913", "country": "PH", "source": "builtin_public"},
        {"name": "New World Makati Hotel", "line1": "Esperanza Street corner Makati Avenue", "city": "Makati", "state": "Metro Manila", "postal_code": "1228", "country": "PH", "source": "builtin_public"},
        {"name": "Shangri-La The Fort", "line1": "30th Street corner 5th Avenue, Bonifacio Global City", "city": "Taguig", "state": "Metro Manila", "postal_code": "1634", "country": "PH", "source": "builtin_public"},
        {"name": "Conrad Manila", "line1": "Seaside Boulevard, Mall of Asia Complex", "city": "Pasay", "state": "Metro Manila", "postal_code": "1300", "country": "PH", "source": "builtin_public"},
        {"name": "Okada Manila", "line1": "New Seaside Drive, Entertainment City", "city": "Paranaque", "state": "Metro Manila", "postal_code": "1701", "country": "PH", "source": "builtin_public"},
        {"name": "Seda BGC", "line1": "30th Street corner 11th Avenue, Bonifacio Global City", "city": "Taguig", "state": "Metro Manila", "postal_code": "1634", "country": "PH", "source": "builtin_public"},
    ]
}


def _load_cache() -> dict[str, list[dict[str, str]]]:
    global _MEMORY_CACHE, _MEMORY_CACHE_MTIME_NS
    try:
        mtime_ns = _CACHE_PATH.stat().st_mtime_ns
        if _MEMORY_CACHE is not None and _MEMORY_CACHE_MTIME_NS == mtime_ns:
            return _MEMORY_CACHE
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        _MEMORY_CACHE = data if isinstance(data, dict) else {}
        _MEMORY_CACHE_MTIME_NS = mtime_ns
        return _MEMORY_CACHE
    except Exception:
        return {}


def _save_cache(cache: dict[str, list[dict[str, str]]]) -> None:
    global _MEMORY_CACHE, _MEMORY_CACHE_MTIME_NS
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_CACHE_PATH)
        _MEMORY_CACHE = cache
        _MEMORY_CACHE_MTIME_NS = _CACHE_PATH.stat().st_mtime_ns
    except Exception:
        pass


def _component(components: list[dict[str, Any]], kind: str, *, short: bool = False) -> str:
    for item in components or []:
        if kind in (item.get("types") or []):
            return str(item.get("shortText" if short else "longText") or item.get("longText") or "").strip()
    return ""


def _google_places(country: str, city: str, region: str) -> list[dict[str, str]]:
    key = str(os.getenv("GOOGLE_MAPS_API_KEY") or "").strip()
    if not key:
        return []
    country_name = _COUNTRY_NAMES.get(country, country)
    query_city = city or region or country_name
    payload = {
        "textQuery": f"hotels and public businesses in {query_city}, {country_name}",
        "languageCode": "en",
        "maxResultCount": 20,
    }
    resp = requests.post(
        "https://places.googleapis.com/v1/places:searchText",
        json=payload,
        headers={
            "X-Goog-Api-Key": key,
            "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.addressComponents",
            "Content-Type": "application/json",
        },
        timeout=20,
    )
    if resp.status_code != 200:
        return []
    out: list[dict[str, str]] = []
    for place in (resp.json() or {}).get("places") or []:
        components = place.get("addressComponents") or []
        item_country = _component(components, "country", short=True).upper()
        street_number = _component(components, "street_number")
        route = _component(components, "route")
        line1 = " ".join(part for part in (route, street_number) if part).strip()
        item_city = (
            _component(components, "locality")
            or _component(components, "postal_town")
            or _component(components, "administrative_area_level_2")
        )
        state = _component(components, "administrative_area_level_1", short=True)
        postal = _component(components, "postal_code")
        if item_country == country and line1 and item_city and postal:
            out.append({
                "name": str((place.get("displayName") or {}).get("text") or "").strip(),
                "line1": line1, "city": item_city, "state": state,
                "postal_code": postal, "country": country, "source": "google_places",
            })
    return out


def _nominatim(country: str, city: str, region: str) -> list[dict[str, str]]:
    global _LAST_NOMINATIM_AT
    country_name = _COUNTRY_NAMES.get(country, country)
    query_city = city or region or country_name
    queries = [
        f"hotel {query_city} {country_name}",
        f"restaurant {query_city} {country_name}",
        f"shopping centre {query_city} {country_name}",
    ]
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for query in queries:
        with _NOMINATIM_LOCK:
            delay = 1.05 - (time.monotonic() - _LAST_NOMINATIM_AT)
            if delay > 0:
                time.sleep(delay)
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": query, "format": "jsonv2", "addressdetails": 1,
                    "limit": 12, "countrycodes": country.lower(),
                },
                headers={"User-Agent": "pay153-public-address-resolver/1.0 (main.153.ink)"},
                timeout=20,
            )
            _LAST_NOMINATIM_AT = time.monotonic()
        if resp.status_code != 200:
            continue
        for row in resp.json() or []:
            address = row.get("address") or {}
            item_country = str(address.get("country_code") or "").upper()
            road = str(address.get("road") or address.get("pedestrian") or "").strip()
            house = str(address.get("house_number") or "").strip()
            place_name = str(
                address.get("tourism") or address.get("amenity") or address.get("shop")
                or row.get("name") or ""
            ).strip()
            line1 = " ".join(part for part in (road, house) if part).strip()
            if not house and place_name and road:
                line1 = f"{place_name}, {road}"
            item_city = str(
                address.get("city") or address.get("town") or address.get("village")
                or address.get("municipality") or address.get("county") or ""
            ).strip()
            state = str(address.get("state") or address.get("state_district") or "").strip()
            iso_state = str(address.get("ISO3166-2-lvl4") or address.get("ISO3166-2-lvl6") or "").upper()
            if country == "US" and iso_state.startswith("US-"):
                state = iso_state.split("-", 1)[1]
            postal = str(address.get("postcode") or "").strip()
            signature = (line1.lower(), item_city.lower(), postal.lower())
            if item_country == country and line1 and item_city and postal and signature not in seen:
                seen.add(signature)
                out.append({
                    "name": place_name, "line1": line1, "city": item_city,
                    "state": state, "postal_code": postal, "country": country,
                    "source": "openstreetmap_nominatim",
                })
        if len(out) >= 8:
            break
    return out


def _pick(key: str, rows: list[dict[str, str]], preferred_city: str = "") -> dict[str, str] | None:
    valid = [row for row in rows if row.get("line1") and row.get("city") and row.get("postal_code")]
    wanted = preferred_city.strip().casefold()
    if wanted:
        local = [row for row in valid if wanted in str(row.get("city") or "").casefold() or str(row.get("city") or "").casefold() in wanted]
        if local:
            valid = local
    if not valid:
        return None
    with _PICK_LOCK:
        recent = _RECENT_PICKS.setdefault(key, [])
        recent_set = set(recent)
        choices = [
            row for row in valid
            if f"{row.get('line1')}|{row.get('postal_code')}" not in recent_set
        ] or valid
        selected = dict(random.SystemRandom().choice(choices))
        signature = f"{selected.get('line1')}|{selected.get('postal_code')}"
        _LAST_PICK[key] = signature
        recent.append(signature)
        del recent[:-64]
    return selected


def resolve_cached_country_address(country: str, preferred_city: str = "") -> dict[str, str] | None:
    """Pick an already validated address from the whole country cache.

    This path never performs a network lookup, so high-concurrency checkout
    jobs can rotate addresses without serializing on Nominatim.
    """
    country = str(country or "").upper()
    with _CACHE_LOCK:
        cache = _load_cache()
        rows: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for key, values in cache.items():
            if not str(key).upper().startswith(country + "|") or not isinstance(values, list):
                continue
            for row in values:
                if not isinstance(row, dict):
                    continue
                signature = (
                    str(row.get("line1") or "").strip().casefold(),
                    str(row.get("city") or "").strip().casefold(),
                    str(row.get("postal_code") or "").strip().casefold(),
                )
                if not all(signature) or signature in seen:
                    continue
                seen.add(signature)
                rows.append(dict(row))
    return _pick(f"{country}|__country_pool__", rows, preferred_city)


def resolve_public_address(country: str, city: str = "", region: str = "", postal_hint: str = "") -> dict[str, str] | None:
    country = str(country or "").upper()
    city = str(city or "").strip()
    region = str(region or "").strip()
    cache_key = f"{country}|{city.lower()}|{region.lower()}"
    with _CACHE_LOCK:
        cache = _load_cache()
        cached = list(cache.get(cache_key) or [])
    # Refresh small pools; otherwise randomly reuse the validated cache.
    if len(cached) < 5:
        with _RESOLVE_LOCK:
            with _CACHE_LOCK:
                cache = _load_cache()
                cached = list(cache.get(cache_key) or [])
            if len(cached) < 5:
                fetched = _google_places(country, city, region)
                if not fetched:
                    try:
                        fetched = _nominatim(country, city, region)
                    except Exception:
                        fetched = []
                merged: list[dict[str, str]] = []
                signatures: set[tuple[str, str, str]] = set()
                for row in fetched + cached + list(_BUILTIN_PUBLIC_ADDRESSES.get(country) or []):
                    signature = (str(row.get("line1") or "").lower(), str(row.get("city") or "").lower(), str(row.get("postal_code") or "").lower())
                    if all(signature) and signature not in signatures:
                        signatures.add(signature)
                        merged.append(row)
                cached = merged[:40]
                with _CACHE_LOCK:
                    cache[cache_key] = cached
                    _save_cache(cache)
    if not cached:
        cached = list(_BUILTIN_PUBLIC_ADDRESSES.get(country) or [])
    return _pick(cache_key, cached, city)
