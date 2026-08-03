"""
SQLite database layer for the Hermes Dashboard.

Replaces the previous JSON-file-based storage with a single SQLite database
at ~/.hermes/dashboard-unified/dashboard.db.

Tables:
  - propositions: Travel propositions from Travely
  - programme_items: Individual activities/program items with cached geocoordinates
  - feedback: Comments and actions on propositions
  - chat_history: Chat messages with Friday
  - adventurelog_locations: Locations added to AdventureLog (with cached coords)
  - findy_listings: Cached real-estate listings (from Nextcloud Tables)
  - spoty_listings: Cached vehicle listings (from Nextcloud Tables)
  - sync_state: Tracks which JSON files have been imported (for the sync script)

Design principles:
  - All dashboard reads come from SQLite (no JSON file reads at runtime)
  - Travely continues writing JSON files — a separate sync script imports them
  - Geocoded coordinates are stored permanently in programme_items; the map
    endpoint reads them from the DB instead of calling Nominatim each time
  - New coordinates are geocoded once when a proposition is synced (if not already
    cached), or on-demand when an AdventureLog location is added
"""

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# --- Configuration ---

HERMES_HOME = Path("/root/.hermes")
DB_DIR = HERMES_HOME / "dashboard-unified"
DB_PATH = DB_DIR / "dashboard.db"


def get_db_path() -> Path:
    """Return the path to the dashboard SQLite database."""
    return DB_PATH


def get_conn() -> sqlite3.Connection:
    """Get a SQLite connection with row factory enabled."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create all tables if they don't exist."""
    conn = get_conn()
    cur = conn.cursor()

    # --- propositions ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS propositions (
            id TEXT PRIMARY KEY,
            titre TEXT,
            destination TEXT,
            pays TEXT,
            duree TEXT,
            budget TEXT,
            statut TEXT DEFAULT 'en_cours',
            date_creation TEXT,
            metadata_json TEXT,
            raw_json TEXT,
            source_file TEXT,
            file_mtime REAL DEFAULT 0,
            imported_at REAL DEFAULT 0
        )
    """)

    # --- programme_items ---
    # Stores individual activities extracted from propositions.
    # latitude/longitude are cached once (geocoded via Nominatim at sync time
    # or on first map load) and never re-geocoded.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS programme_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposition_id TEXT NOT NULL,
            activity_index INTEGER DEFAULT 0,
            heure TEXT DEFAULT '',
            activite TEXT DEFAULT '',
            type TEXT DEFAULT 'default',
            description TEXT DEFAULT '',
            cout TEXT DEFAULT '',
            position TEXT DEFAULT '',
            latitude REAL,
            longitude REAL,
            display_name TEXT DEFAULT '',
            geocoded_at REAL,
            FOREIGN KEY (proposition_id) REFERENCES propositions(id) ON DELETE CASCADE
        )
    """)

    # --- feedback ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY,
            proposition_id TEXT NOT NULL,
            type TEXT NOT NULL,
            action TEXT,
            text TEXT,
            author TEXT DEFAULT 'mathieu',
            reorientation TEXT,
            refus_raison TEXT,
            date TEXT,
            source_file TEXT,
            file_mtime REAL DEFAULT 0,
            imported_at REAL DEFAULT 0
        )
    """)

    # --- chat_history ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT,
            type TEXT DEFAULT 'text',
            timestamp REAL DEFAULT 0
        )
    """)

    # --- adventurelog_locations ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS adventurelog_locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposition_id TEXT,
            programme_item_id INTEGER,
            adventurelog_uuid TEXT,
            name TEXT,
            latitude REAL,
            longitude REAL,
            date_added REAL DEFAULT 0,
            FOREIGN KEY (programme_item_id) REFERENCES programme_items(id) ON DELETE SET NULL
        )
    """)

    # --- findy_listings ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS findy_listings (
            row_id INTEGER PRIMARY KEY,
            titre TEXT,
            prix TEXT,
            surface TEXT,
            url TEXT,
            statut INTEGER,
            statut_label TEXT,
            created_at TEXT,
            cached_at REAL DEFAULT 0
        )
    """)

    # --- spoty_listings ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS spoty_listings (
            row_id INTEGER PRIMARY KEY,
            titre TEXT,
            prix TEXT,
            annee TEXT,
            kilometrage TEXT,
            carburant TEXT,
            portes TEXT,
            localisation TEXT,
            url TEXT,
            created_at TEXT,
            cached_at REAL DEFAULT 0
        )
    """)

    # --- sync_state ---
    # Tracks which JSON files have been imported into SQLite.
    # Used by sync_json_to_sqlite.py to avoid re-importing unchanged files.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sync_state (
            source_file TEXT PRIMARY KEY,
            file_mtime REAL DEFAULT 0,
            imported_at REAL DEFAULT 0
        )
    """)

    # Indexes for performance
    cur.execute("CREATE INDEX IF NOT EXISTS idx_prog_prop ON programme_items(proposition_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_prop ON feedback(proposition_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_chat_ts ON chat_history(timestamp)")

    conn.commit()
    conn.close()


# --- Propositions CRUD ---


def upsert_proposition(conn: sqlite3.Connection, prop: dict, raw_json: str,
                       source_file: str, file_mtime: float):
    """Insert or update a proposition in the database."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO propositions (id, titre, destination, pays, duree, budget, statut,
                                  date_creation, metadata_json, raw_json, source_file,
                                  file_mtime, imported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            titre=excluded.titre,
            destination=excluded.destination,
            pays=excluded.pays,
            duree=excluded.duree,
            budget=excluded.budget,
            statut=excluded.statut,
            date_creation=excluded.date_creation,
            metadata_json=excluded.metadata_json,
            raw_json=excluded.raw_json,
            source_file=excluded.source_file,
            file_mtime=excluded.file_mtime,
            imported_at=excluded.imported_at
    """, (
        prop.get("id", source_file),
        prop.get("destination", ""),  # titre is often the destination
        prop.get("destination", ""),
        prop.get("pays", ""),
        str(prop.get("duree_jours", "")),
        str(prop.get("budget_total", "")),
        prop.get("statut", "en_cours"),
        prop.get("date", ""),
        json.dumps({k: v for k, v in prop.items()
                    if k not in ("id", "date", "destination", "pays", "duree_jours",
                                 "budget_total", "statut", "activites")},
                   ensure_ascii=False, default=str),
        raw_json,
        source_file,
        file_mtime,
        time.time(),
    ))

    # Replace programme_items for this proposition
    cur.execute("DELETE FROM programme_items WHERE proposition_id = ?", (prop.get("id", source_file),))

    # Insert programme items from the normalized activites list
    activites = prop.get("activites", [])
    if isinstance(activites, list):
        for idx, act in enumerate(activites):
            if isinstance(act, str):
                cur.execute("""
                    INSERT INTO programme_items (proposition_id, activity_index, activite, type)
                    VALUES (?, ?, ?, ?)
                """, (prop.get("id", source_file), idx, act, "default"))
            elif isinstance(act, dict):
                cur.execute("""
                    INSERT INTO programme_items (proposition_id, activity_index, activite, type,
                                                  heure, description, cout, position)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    prop.get("id", source_file), idx,
                    act.get("activite", act.get("nom", "")),
                    act.get("type", "default"),
                    act.get("heure", ""),
                    act.get("description", ""),
                    str(act.get("cout", "")),
                    act.get("position", ""),
                ))


def get_all_propositions() -> list[dict]:
    """Load all propositions from SQLite, sorted by date descending."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, titre, destination, pays, duree, budget, statut,
               date_creation, metadata_json
        FROM propositions
        ORDER BY date_creation DESC
    """)
    props = []
    for row in cur.fetchall():
        prop = {
            "id": row["id"],
            "titre": row["titre"],
            "destination": row["destination"],
            "pays": row["pays"],
            "duree_jours": row["duree"],
            "budget_total": row["budget"],
            "statut": row["statut"],
            "date": row["date_creation"],
        }
        # Merge metadata
        if row["metadata_json"]:
            try:
                meta = json.loads(row["metadata_json"])
                for k, v in meta.items():
                    if k not in prop:
                        prop[k] = v
            except (json.JSONDecodeError, TypeError):
                pass
        props.append(prop)
    conn.close()
    return props


def get_proposition_by_id(prop_id: str, raw: bool = False) -> Optional[dict]:
    """Load a single proposition by ID.

    If raw=True, return the raw JSON (preserves 'programme', 'itineraires', etc.
    needed for map point extraction and AdventureLog place extraction).
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM propositions WHERE id = ?", (prop_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    if raw:
        try:
            return json.loads(row["raw_json"])
        except (json.JSONDecodeError, TypeError):
            return None
    # Return normalized form
    prop = {
        "id": row["id"],
        "destination": row["destination"],
        "pays": row["pays"],
        "duree_jours": row["duree"],
        "budget_total": row["budget"],
        "statut": row["statut"],
        "date": row["date_creation"],
    }
    if row["metadata_json"]:
        try:
            meta = json.loads(row["metadata_json"])
            for k, v in meta.items():
                if k not in prop:
                    prop[k] = v
        except (json.JSONDecodeError, TypeError):
            pass
    return prop


def get_programme_items(prop_id: str) -> list[dict]:
    """Get all programme items for a proposition, including cached coordinates."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, proposition_id, activity_index, heure, activite, type,
               description, cout, position, latitude, longitude, display_name,
               geocoded_at
        FROM programme_items
        WHERE proposition_id = ?
        ORDER BY activity_index
    """, (prop_id,))
    items = [dict(row) for row in cur.fetchall()]
    conn.close()
    return items


def update_programme_item_coords(item_id: int, lat: float, lon: float,
                                  display_name: str = ""):
    """Update cached coordinates for a programme item (geocoded once, never re-geocoded)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE programme_items
        SET latitude = ?, longitude = ?, display_name = ?, geocoded_at = ?
        WHERE id = ?
    """, (lat, lon, display_name, time.time(), item_id))
    conn.commit()
    conn.close()


# --- Feedback CRUD ---


def upsert_feedback(conn: sqlite3.Connection, entry: dict, source_file: str,
                    file_mtime: float):
    """Insert or update a feedback entry."""
    cur = conn.cursor()
    fid = entry.get("feedback_id", source_file)
    cur.execute("""
        INSERT INTO feedback (id, proposition_id, type, action, text, author,
                              reorientation, refus_raison, date, source_file, file_mtime,
                              imported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            proposition_id=excluded.proposition_id,
            type=excluded.type,
            action=excluded.action,
            text=excluded.text,
            author=excluded.author,
            reorientation=excluded.reorientation,
            refus_raison=excluded.refus_raison,
            date=excluded.date,
            source_file=excluded.source_file,
            file_mtime=excluded.file_mtime,
            imported_at=excluded.imported_at
    """, (
        fid,
        entry.get("proposition_id", ""),
        entry.get("type", "comment"),
        entry.get("action"),
        entry.get("text", ""),
        entry.get("author", "mathieu"),
        entry.get("reorientation"),
        entry.get("refus_raison"),
        entry.get("date", ""),
        source_file,
        file_mtime,
        time.time(),
    ))


def get_all_feedback() -> dict:
    """Load all feedback grouped by proposition_id.

    Returns:
        {prop_id: {"comments": [...], "action": str|None, "action_detail": str|None,
                    "action_date": str|None}}
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, proposition_id, type, action, text, author, reorientation,
               refus_raison, date
        FROM feedback
        ORDER BY date ASC
    """)
    feedback = {}
    for row in cur.fetchall():
        prop_id = row["proposition_id"]
        fb = feedback.setdefault(prop_id, {
            "comments": [],
            "action": None,
            "action_detail": None,
            "action_date": None,
        })
        if row["type"] == "comment":
            fb["comments"].append(dict(row))
        elif row["type"] == "action":
            fb["action"] = row["action"]
            fb["action_detail"] = row["reorientation"] or row["refus_raison"]
            fb["action_date"] = row["date"]
    conn.close()
    return feedback


def insert_feedback_direct(prop_id: str, fb_type: str, action: str = None,
                            text: str = None, author: str = "mathieu",
                            reorientation: str = None, refus_raison: str = None,
                            feedback_id: str = None) -> str:
    """Insert a feedback entry directly (from dashboard UI, not from JSON sync).

    Also writes a JSON file to the feedback directory so Travely can read it.

    Returns the feedback_id.
    """
    import uuid as _uuid

    if not feedback_id:
        feedback_id = _uuid.uuid4().hex[:8]

    now_iso = datetime.now(timezone.utc).isoformat()
    entry = {
        "type": fb_type,
        "proposition_id": prop_id,
        "action": action,
        "text": text,
        "author": author,
        "reorientation": reorientation,
        "refus_raison": refus_raison,
        "feedback_id": feedback_id,
        "date": now_iso,
    }

    # Write JSON file for Travely compatibility
    feedback_dir = HERMES_HOME / "travely" / "feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    json_path = feedback_dir / f"{feedback_id}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=2)

    # Insert into SQLite
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO feedback (id, proposition_id, type, action, text, author,
                              reorientation, refus_raison, date, source_file, file_mtime,
                              imported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            proposition_id=excluded.proposition_id,
            type=excluded.type,
            action=excluded.action,
            text=excluded.text,
            author=excluded.author,
            reorientation=excluded.reorientation,
            refus_raison=excluded.refus_raison,
            date=excluded.date
    """, (
        feedback_id, prop_id, fb_type, action, text, author,
        reorientation, refus_raison, now_iso,
        str(json_path), 0.0, time.time()
    ))
    conn.commit()
    conn.close()

    return feedback_id


def delete_feedback(feedback_id: str):
    """Delete a feedback entry from both SQLite and the JSON file."""
    # Delete JSON file
    feedback_dir = HERMES_HOME / "travely" / "feedback"
    json_path = feedback_dir / f"{feedback_id}.json"
    if json_path.exists():
        json_path.unlink()

    # Delete from SQLite
    conn = get_conn()
    conn.execute("DELETE FROM feedback WHERE id = ?", (feedback_id,))
    conn.commit()
    conn.close()


def delete_action_for_proposition(prop_id: str):
    """Delete all action-type feedback for a proposition (when clearing action)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id FROM feedback WHERE proposition_id = ? AND type = 'action'
    """, (prop_id,))
    rows = cur.fetchall()
    conn.close()

    for row in rows:
        delete_feedback(row["id"])


# --- Chat history CRUD ---


def load_chat_history(max_days: int = 7) -> list[dict]:
    """Load chat history from SQLite, filtered to last N days."""
    conn = get_conn()
    cur = conn.cursor()
    cutoff = time.time() - (max_days * 86400)
    cur.execute("""
        SELECT role, content, type, timestamp FROM chat_history
        WHERE timestamp >= ?
        ORDER BY timestamp ASC
    """, (cutoff,))
    messages = [{"role": row["role"], "content": row["content"],
                 "type": row["type"], "timestamp": row["timestamp"]}
                for row in cur.fetchall()]
    conn.close()
    return messages


def append_chat_message(role: str, content: str, msg_type: str = "text",
                        timestamp: float = None):
    """Append a chat message to SQLite."""
    if timestamp is None:
        timestamp = time.time()
    conn = get_conn()
    conn.execute("""
        INSERT INTO chat_history (role, content, type, timestamp)
        VALUES (?, ?, ?, ?)
    """, (role, content[:5000], msg_type, timestamp))
    # Clean up old messages (> 7 days)
    cutoff = time.time() - (7 * 86400)
    conn.execute("DELETE FROM chat_history WHERE timestamp < ?", (cutoff,))
    conn.commit()
    conn.close()


def clear_chat_history():
    """Clear all chat history."""
    conn = get_conn()
    conn.execute("DELETE FROM chat_history")
    conn.commit()
    conn.close()


# --- AdventureLog locations CRUD ---


def add_adventurelog_location(proposition_id: str, programme_item_id: int,
                               adventurelog_uuid: str, name: str,
                               lat: float, lon: float):
    """Record that a location was added to AdventureLog (with cached coords)."""
    conn = get_conn()
    conn.execute("""
        INSERT INTO adventurelog_locations
            (proposition_id, programme_item_id, adventurelog_uuid, name,
             latitude, longitude, date_added)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (proposition_id, programme_item_id, adventurelog_uuid, name,
          lat, lon, time.time()))
    conn.commit()
    conn.close()


def get_adventurelog_locations() -> list[dict]:
    """Get all AdventureLog locations from the local cache."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, proposition_id, programme_item_id, adventurelog_uuid,
               name, latitude, longitude, date_added
        FROM adventurelog_locations
        ORDER BY date_added DESC
    """)
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


# --- Listings cache CRUD ---


def upsert_findy_listings(listings: list[dict]):
    """Replace all findy listings in the cache."""
    conn = get_conn()
    now = time.time()
    conn.execute("DELETE FROM findy_listings")
    for item in listings:
        conn.execute("""
            INSERT INTO findy_listings (row_id, titre, prix, surface, url, statut,
                                        statut_label, created_at, cached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item.get("row_id"),
            item.get("titre", ""),
            item.get("prix", ""),
            item.get("surface", ""),
            item.get("url", ""),
            item.get("statut"),
            item.get("statut_label", ""),
            item.get("created_at", ""),
            now,
        ))
    conn.commit()
    conn.close()


def get_findy_listings() -> list[dict]:
    """Get all cached findy listings."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT row_id, titre, prix, surface, url, statut, statut_label,
               created_at, cached_at
        FROM findy_listings
    """)
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


def get_findy_cache_info() -> dict:
    """Get cache metadata (count, cached_at timestamp)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt, MAX(cached_at) as at FROM findy_listings")
    row = cur.fetchone()
    conn.close()
    return {"count": row["cnt"] if row else 0, "cached_at": row["at"] if row else None}


def upsert_spoty_listings(listings: list[dict]):
    """Replace all spoty listings in the cache."""
    conn = get_conn()
    now = time.time()
    conn.execute("DELETE FROM spoty_listings")
    for item in listings:
        conn.execute("""
            INSERT INTO spoty_listings (row_id, titre, prix, annee, kilometrage,
                                        carburant, portes, localisation, url,
                                        created_at, cached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            item.get("row_id"),
            item.get("titre", ""),
            item.get("prix", ""),
            item.get("annee", ""),
            item.get("kilometrage", ""),
            item.get("carburant", ""),
            item.get("portes", ""),
            item.get("localisation", ""),
            item.get("url", ""),
            item.get("created_at", ""),
            now,
        ))
    conn.commit()
    conn.close()


def get_spoty_listings() -> list[dict]:
    """Get all cached spoty listings."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT row_id, titre, prix, annee, kilometrage, carburant, portes,
               localisation, url, created_at, cached_at
        FROM spoty_listings
    """)
    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows


def get_spoty_cache_info() -> dict:
    """Get cache metadata (count, cached_at timestamp)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as cnt, MAX(cached_at) as at FROM spoty_listings")
    row = cur.fetchone()
    conn.close()
    return {"count": row["cnt"] if row else 0, "cached_at": row["at"] if row else None}


# --- Sync state helpers ---


def needs_sync(source_file: str, file_mtime: float) -> bool:
    """Check if a file needs to be re-synced (new or modified since last sync)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT file_mtime FROM sync_state WHERE source_file = ?",
                (source_file,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return True
    return file_mtime > row["file_mtime"]


def mark_synced(source_file: str, file_mtime: float):
    """Mark a file as synced."""
    conn = get_conn()
    conn.execute("""
        INSERT INTO sync_state (source_file, file_mtime, imported_at)
        VALUES (?, ?, ?)
        ON CONFLICT(source_file) DO UPDATE SET
            file_mtime=excluded.file_mtime,
            imported_at=excluded.imported_at
    """, (source_file, file_mtime, time.time()))
    conn.commit()
    conn.close()


# --- Config helpers (config stays in JSON for simplicity) ---


def load_config() -> dict:
    """Load dashboard config from JSON file (config stays JSON, not in SQLite)."""
    config_file = DB_DIR / "config.json"
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"tts_voice": "fr-FR-DeniseNeural", "chat_enabled": True}


def save_config(config: dict):
    """Save dashboard config to JSON file."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    config_file = DB_DIR / "config.json"
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


# --- Initialization ---

# Initialize the database when this module is imported
init_db()
