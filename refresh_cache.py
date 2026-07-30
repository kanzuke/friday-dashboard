#!/usr/bin/env python3
"""
Cache refresh script for Hermes Dashboard.
Fetches data from Nextcloud Tables (ID 4 = immo, ID 5 = vehicules)
and saves to local JSON files for the dashboard to read.

Run once after each cron cycle (Findy at 05h UTC, Spoty at 06h UTC).
Can be called manually or via systemd timer / cron.
"""

import json
import os
import base64
import urllib.request
import sys
from pathlib import Path
from datetime import datetime, timezone

# --- Config ---
# Force HERMES_HOME to the real /root/.hermes, regardless of profile context.
# When running inside a profile session, HERMES_HOME may point to
# /root/.hermes/profiles/<name> — we need to go up to the real root.
_HERMES_CANDIDATES = [
    os.environ.get("HERMES_HOME"),
    "/root/.hermes",
    os.path.expanduser("~/.hermes"),
]
HERMES_HOME = None
for c in _HERMES_CANDIDATES:
    if not c:
        continue
    p = Path(c)
    # If this is a profile dir (e.g., /root/.hermes/profiles/dev), go up 2 levels
    if p.name in ("dev", "findy", "spoty", "travely", "default") and p.parent.name == "profiles":
        p = p.parent.parent
    if p.exists():
        HERMES_HOME = p
        break
if HERMES_HOME is None:
    HERMES_HOME = Path("/root/.hermes")
CACHE_DIR = HERMES_HOME / "dashboard-unified" / "cache"

# Nextcloud credentials from Findy .env (shared with Spoty)
ENV_FILE = HERMES_HOME / "profiles" / "findy" / ".env"

# Column mappings for Nextcloud Tables
IMMO_COLUMNS = {19: "titre", 20: "prix", 21: "surface", 22: "url", 23: "statut"}
IMMO_STATUT_LABELS = {0: "Intéressant", 1: "Pas intéressant", 2: "Trop tard"}

VEHICULE_COLUMNS = {25: "titre", 26: "prix", 27: "annee", 28: "kilometrage",
                    29: "carburant", 30: "portes", 31: "localisation", 32: "url"}


def load_nc_credentials():
    """Load Nextcloud credentials from .env file."""
    if not ENV_FILE.exists():
        print(f"[ERROR] .env file not found: {ENV_FILE}")
        sys.exit(1)
    creds = {}
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("NEXTCLOUD_") and "=" in line:
                key, value = line.split("=", 1)
                creds[key] = value.strip('"').strip("'")
    return creds


def parse_nc_rows(rows, col_map):
    """Parse Nextcloud Tables rows into flat dicts using column mapping."""
    result = []
    for row in rows:
        item = {"row_id": row.get("id"), "created_at": row.get("createdAt", "")}
        for cell in row.get("data", []):
            col_id = cell.get("columnId")
            val = cell.get("value")
            if col_id in col_map:
                field = col_map[col_id]
                if field == "url" and val and isinstance(val, str):
                    try:
                        url_data = json.loads(val)
                        val = url_data.get("value", url_data.get("title", val))
                    except (json.JSONDecodeError, TypeError):
                        pass
                item[field] = val
        result.append(item)
    return result


def fetch_nc_tables(table_id, creds):
    """Fetch rows from a Nextcloud Tables API and parse them."""
    base_url = creds.get("NEXTCLOUD_BASE_URL", "https://cloud.wired-lan.fr")
    user = creds.get("NEXTCLOUD_LOGIN", "")
    password = creds.get("NEXTCLOUD_PASSWORD", "")
    if not user or not password:
        print(f"[ERROR] Missing NC credentials for table {table_id}")
        return []

    try:
        url = f"{base_url}/index.php/apps/tables/api/1/tables/{table_id}/rows"
        req = urllib.request.Request(url, headers={
            "Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode(),
            "Accept": "application/json",
            "OCS-APIRequest": "true",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            raw_rows = data if isinstance(data, list) else []
            if table_id == 4:
                parsed = parse_nc_rows(raw_rows, IMMO_COLUMNS)
                for item in parsed:
                    statut_val = item.get("statut")
                    item["statut_label"] = IMMO_STATUT_LABELS.get(statut_val, "Nouveau")
                return parsed
            elif table_id == 5:
                return parse_nc_rows(raw_rows, VEHICULE_COLUMNS)
            return raw_rows
    except Exception as e:
        print(f"[WARN] NC Tables fetch failed for table {table_id}: {e}")
        return []


def refresh_cache():
    """Fetch all tables and save to local JSON cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    creds = load_nc_credentials()
    now = datetime.now(timezone.utc).isoformat()

    # Table 4: Immobilier (Findy)
    print("[CACHE] Fetching immo listings (table 4)...")
    immo_rows = fetch_nc_tables(4, creds)
    immo_data = {
        "fetched_at": now,
        "table_id": 4,
        "source": "nextcloud_tables",
        "count": len(immo_rows),
        "annonces": immo_rows,
    }
    immo_path = CACHE_DIR / "findy_listings.json"
    with open(immo_path, "w", encoding="utf-8") as f:
        json.dump(immo_data, f, ensure_ascii=False, indent=2)
    print(f"[CACHE] Saved {len(immo_rows)} immo listings to {immo_path}")

    # Table 5: Véhicules (Spoty)
    print("[CACHE] Fetching vehicule listings (table 5)...")
    veh_rows = fetch_nc_tables(5, creds)
    veh_data = {
        "fetched_at": now,
        "table_id": 5,
        "source": "nextcloud_tables",
        "count": len(veh_rows),
        "annonces": veh_rows,
    }
    veh_path = CACHE_DIR / "spoty_listings.json"
    with open(veh_path, "w", encoding="utf-8") as f:
        json.dump(veh_data, f, ensure_ascii=False, indent=2)
    print(f"[CACHE] Saved {len(veh_rows)} vehicule listings to {veh_path}")

    print(f"[CACHE] Done at {now}")


if __name__ == "__main__":
    refresh_cache()
