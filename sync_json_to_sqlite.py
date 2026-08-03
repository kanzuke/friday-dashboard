#!/usr/bin/env python3
"""
Sync script: imports Travely JSON files into the SQLite dashboard database.

This script is the bridge between Travely (which writes JSON files) and the
dashboard (which reads from SQLite). It should be run:
  1. Once at dashboard startup (called from app.py)
  2. Periodically via a watcher loop or cron job (every 30-60 seconds)

What it syncs:
  - Propositions: ~/.hermes/travely/propositions/*.json → propositions table
  - Archived propositions: ~/.hermes/travely/archive/*.json → propositions table (statut=archivee)
  - Feedback: ~/.hermes/travely/feedback/*.json → feedback table
  - Listings cache: ~/.hermes/dashboard-unified/cache/findy_listings.json → findy_listings table
  - Listings cache: ~/.hermes/dashboard-unified/cache/spoty_listings.json → spoty_listings table
  - Chat history: ~/.hermes/dashboard-unified/chat_history.json → chat_history table (one-time migration)

Only files that have changed (mtime > last synced mtime) are re-imported.

Usage:
  python3 sync_json_to_sqlite.py            # sync once
  python3 sync_json_to_sqlite.py --watch   # sync every 30 seconds
  python3 sync_json_to_sqlite.py --verbose # verbose output
"""

import json
import sys
import time
import os
from pathlib import Path

# Import the db module
sys.path.insert(0, str(Path(__file__).parent))
import db

HERMES_HOME = Path("/root/.hermes")
PROPOSITIONS_DIR = HERMES_HOME / "travely" / "propositions"
ARCHIVE_DIR = HERMES_HOME / "travely" / "archive"
FEEDBACK_DIR = HERMES_HOME / "travely" / "feedback"
LISTINGS_CACHE_DIR = HERMES_HOME / "dashboard-unified" / "cache"
CHAT_HISTORY_FILE = HERMES_HOME / "dashboard-unified" / "chat_history.json"

# Import normalize_proposition from app.py
# We do a minimal import to avoid starting the FastAPI app
import importlib.util

_spec = importlib.util.spec_from_file_location("app_normalize", Path(__file__).parent / "app.py")
# Don't execute app.py — just extract normalize_proposition by reading the function
# Actually, we can import it more cleanly by extracting the function standalone.
# Since app.py has side effects (creates FastAPI app), let's re-implement the
# normalize_proposition call via a subprocess-safe approach.
# Alternative: just store the raw JSON and let the dashboard normalize on read.

# Simple approach: store raw JSON in the DB, normalize at read time (dashboard side).
# The sync script just needs to read the JSON, get the file_id, and call db.upsert_proposition.

VERBOSE = False


def log(msg: str):
    if VERBOSE:
        print(f"[SYNC] {msg}", flush=True)


def get_file_mtime(fpath: Path) -> float:
    try:
        return fpath.stat().st_mtime
    except OSError:
        return 0.0


def load_json(fpath: Path) -> dict | None:
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"[SYNC] WARN: Could not load {fpath}: {e}", flush=True)
        return None


def sync_propositions():
    """Sync all proposition JSON files into SQLite."""
    count = 0
    skipped = 0

    # Active propositions
    for fpath in sorted(PROPOSITIONS_DIR.glob("*.json")):
        mtime = get_file_mtime(fpath)
        source_file = str(fpath)
        if not db.needs_sync(source_file, mtime):
            skipped += 1
            continue

        data = load_json(fpath)
        if data is None:
            continue

        file_id = fpath.stem
        if "id" not in data:
            data["id"] = file_id

        raw_json = json.dumps(data, ensure_ascii=False, indent=2)

        # Build a minimal normalized prop for the DB
        # (the dashboard will do full normalization on read from raw_json)
        prop = {
            "id": data.get("id", file_id),
            "date": _extract_date(data),
            "destination": _extract_destination(data),
            "pays": _extract_pays(data),
            "duree_jours": _extract_duree(data),
            "budget_total": _extract_budget(data),
            "statut": "en_cours",
            "activites": [],  # Will be populated by normalize_proposition at dashboard level
        }

        conn = db.get_conn()
        db.upsert_proposition(conn, prop, raw_json, source_file, mtime)
        conn.commit()
        conn.close()
        db.mark_synced(source_file, mtime)
        count += 1
        log(f"Synced proposition: {file_id}")

    # Archived propositions
    for fpath in sorted(ARCHIVE_DIR.glob("*.json")):
        mtime = get_file_mtime(fpath)
        source_file = str(fpath)
        if not db.needs_sync(source_file, mtime):
            skipped += 1
            continue

        data = load_json(fpath)
        if data is None:
            continue

        file_id = fpath.stem
        if "id" not in data:
            data["id"] = file_id
        data["statut"] = "archivee"

        raw_json = json.dumps(data, ensure_ascii=False, indent=2)
        prop = {
            "id": data.get("id", file_id),
            "date": _extract_date(data),
            "destination": _extract_destination(data),
            "pays": _extract_pays(data),
            "duree_jours": _extract_duree(data),
            "budget_total": _extract_budget(data),
            "statut": "archivee",
            "activites": [],
        }

        conn = db.get_conn()
        db.upsert_proposition(conn, prop, raw_json, source_file, mtime)
        conn.commit()
        conn.close()
        db.mark_synced(source_file, mtime)
        count += 1
        log(f"Synced archived proposition: {file_id}")

    return count, skipped


def sync_feedback():
    """Sync all feedback JSON files into SQLite."""
    count = 0
    skipped = 0

    for fpath in sorted(FEEDBACK_DIR.glob("*.json")):
        mtime = get_file_mtime(fpath)
        source_file = str(fpath)
        if not db.needs_sync(source_file, mtime):
            skipped += 1
            continue

        data = load_json(fpath)
        if data is None:
            continue

        conn = db.get_conn()
        db.upsert_feedback(conn, data, source_file, mtime)
        conn.commit()
        conn.close()
        db.mark_synced(source_file, mtime)
        count += 1
        log(f"Synced feedback: {fpath.stem}")

    return count, skipped


def sync_listings():
    """Sync findy and spoty listing caches from JSON to SQLite."""
    count = 0

    # Findy
    findy_file = LISTINGS_CACHE_DIR / "findy_listings.json"
    if findy_file.exists():
        mtime = get_file_mtime(findy_file)
        source_file = str(findy_file)
        if db.needs_sync(source_file, mtime):
            data = load_json(findy_file)
            if data and isinstance(data, dict):
                annonces = data.get("annonces", [])
                if isinstance(annonces, list):
                    db.upsert_findy_listings(annonces)
                    db.mark_synced(source_file, mtime)
                    count += 1
                    log(f"Synced {len(annonces)} findy listings")

    # Spoty
    spoty_file = LISTINGS_CACHE_DIR / "spoty_listings.json"
    if spoty_file.exists():
        mtime = get_file_mtime(spoty_file)
        source_file = str(spoty_file)
        if db.needs_sync(source_file, mtime):
            data = load_json(spoty_file)
            if data and isinstance(data, dict):
                annonces = data.get("annonces", [])
                if isinstance(annonces, list):
                    db.upsert_spoty_listings(annonces)
                    db.mark_synced(source_file, mtime)
                    count += 1
                    log(f"Synced {len(annonces)} spoty listings")

    return count


def migrate_chat_history():
    """One-time migration of chat history from JSON to SQLite."""
    if not CHAT_HISTORY_FILE.exists():
        return 0

    # Check if already migrated
    conn = db.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt FROM chat_history")
    existing = cur.fetchone()["cnt"]
    conn.close()
    if existing > 0:
        return 0  # Already migrated

    data = load_json(CHAT_HISTORY_FILE)
    if not data or not isinstance(data, list):
        return 0

    count = 0
    for msg in data:
        if isinstance(msg, dict) and msg.get("role") and msg.get("content"):
            ts = msg.get("timestamp")
            if not isinstance(ts, (int, float)):
                ts = time.time()
            db.append_chat_message(msg["role"], msg["content"],
                                   msg.get("type", "text"), ts)
            count += 1

    log(f"Migrated {count} chat history entries")
    return count


def sync_once():
    """Run one full sync pass."""
    prop_count, prop_skipped = sync_propositions()
    fb_count, fb_skipped = sync_feedback()
    listings_count = sync_listings()
    chat_count = migrate_chat_history()

    total = prop_count + fb_count + listings_count + chat_count
    if total > 0 or VERBOSE:
        print(f"[SYNC] Propositions: {prop_count} new, {prop_skipped} skipped | "
              f"Feedback: {fb_count} new, {fb_skipped} skipped | "
              f"Listings: {listings_count} | Chat: {chat_count}", flush=True)
    return total


def watch(interval: int = 30):
    """Run sync in a loop every `interval` seconds."""
    print(f"[SYNC] Watcher started (interval={interval}s)", flush=True)
    while True:
        try:
            sync_once()
        except Exception as e:
            print(f"[SYNC] ERROR: {e}", flush=True)
        time.sleep(interval)


# --- Helpers to extract fields from raw proposition JSON ---


def _extract_date(data: dict) -> str:
    meta = data.get("metadata", {})
    return (meta.get("date_creation", "") or meta.get("date", "") or
            data.get("date", ""))


def _extract_destination(data: dict) -> str:
    # Handle destination as a dict (e.g. Maroc: {"pays": "Maroc", "ville_principale": "Marrakech", ...})
    dest = data.get("destination")
    if isinstance(dest, dict):
        ville = dest.get("ville_principale", "") or dest.get("ville", "")
        pays = dest.get("pays", "")
        if ville and pays:
            return f"{ville}, {pays}"
        return ville or pays or "Destination inconnue"
    if dest and not data.get("metadata"):
        return str(dest)
    meta = data.get("metadata", {})
    if not meta:
        return str(dest) if dest else ""
    ville = (meta.get("ville_principale", "") or
             meta.get("destination_principale", "") or
             meta.get("ville", ""))
    pays = meta.get("pays", "")
    if not pays and ville:
        parts = ville.split(",")
        if len(parts) >= 2:
            pays = parts[-1].strip()
    if ville and pays and not ville.rstrip().endswith(pays):
        return f"{ville}, {pays}"
    return ville or pays or meta.get("titre", "Destination inconnue")


def _extract_pays(data: dict) -> str:
    meta = data.get("metadata", {})
    pays = meta.get("pays", "")
    if not pays:
        ville = meta.get("ville", "")
        if ville and "," in ville:
            pays = ville.split(",")[-1].strip()
    return pays


def _extract_duree(data: dict) -> str:
    meta = data.get("metadata", {})
    duree = meta.get("duree_jours", 0)
    if not duree:
        voyageur = data.get("voyageur", {})
        if isinstance(voyageur, dict):
            duree = voyageur.get("duree_jours", 0)
    if not duree:
        duree = data.get("duree_jours", 0)
    if not duree and meta.get("duree"):
        import re
        m = re.match(r"(\d+)", str(meta["duree"]))
        if m:
            duree = int(m.group(1))
    return str(duree) if duree else "0"


def _extract_budget(data: dict) -> str:
    budget_total = "?"
    budget_data = data.get("budget_estime", {})
    if isinstance(budget_data, dict) and budget_data:
        budget_moyen = budget_data.get("total_moyen_estime_eur")
        budget_min = budget_data.get("total_min_eur")
        budget_max = budget_data.get("total_max_eur")
        if budget_moyen:
            budget_total = f"~{budget_moyen}€"
        elif budget_min and budget_max:
            budget_total = f"{budget_min}-{budget_max}€"
    if budget_total == "?" and "budget" in data:
        bud = data["budget"]
        if isinstance(bud, dict):
            total = (bud.get("BUDGET_TOTAL_ESTIME_EUR") or
                     bud.get("budget_total_estime_eur") or
                     bud.get("total_eur"))
            if total:
                budget_total = f"~{total}€"
    if budget_total == "?" and "budget_total_estime" in data:
        bud = data["budget_total_estime"]
        if isinstance(bud, dict):
            total = bud.get("total", "")
            if total:
                budget_total = str(total)
    if budget_total == "?":
        budget_total = str(data.get("budget_total", "?"))
    return budget_total


if __name__ == "__main__":
    VERBOSE = "--verbose" in sys.argv or "-v" in sys.argv

    if "--watch" in sys.argv:
        watch()
    else:
        sync_once()
