"""
Dashboard Unifié Hermes - Backend FastAPI

Application web unifiée regroupant:
- Page d'accueil avec statut système (agents, services, cron, kanban)
- Voyages (propositions Travely avec images/liens, commentaires, actions)
- Immobilier (annonces Findy depuis Nextcloud Tables)
- Véhicules (annonces Spoty depuis Nextcloud Tables)
- Chat vocal avec Friday (texte + STT via faster-whisper + TTS via edge-tts)
- Paramètres (sélection de voix TTS, configuration)

Bind sur 10.245.243.1 (ZeroTier), port 9120, auth basic (mathieu/toto).
"""

import json
import os
import glob
import hashlib
import uuid
import urllib.parse
import urllib.request
import subprocess
import tempfile
import asyncio
import base64
import time
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Depends, Request, Response, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# --- Geocoding helpers (Nominatim with fallbacks) ---
from geocode_helpers import init_cache_dir, geocode_with_fallbacks

# --- SQLite database layer ---
import db as dashboard_db

# --- Configuration ---
# Force HERMES_HOME to /root/.hermes regardless of profile context
_HERMES_HOME_CANDIDATES = [
    os.environ.get("HERMES_HOME"),
    "/root/.hermes",
    os.path.expanduser("~/.hermes"),
]
HERMES_HOME = None
for candidate in _HERMES_HOME_CANDIDATES:
    if candidate and Path(candidate).exists():
        HERMES_HOME = Path(candidate)
        break
if HERMES_HOME is None:
    HERMES_HOME = Path("/root/.hermes")
TRAVELY_DIR = HERMES_HOME / "travely"
PROPOSITIONS_DIR = TRAVELY_DIR / "propositions"
FEEDBACK_DIR = TRAVELY_DIR / "feedback"
CACHE_DIR = TRAVELY_DIR / "cache" / "enrich"
ARCHIVE_DIR = TRAVELY_DIR / "archive"
LOCKS_DIR = TRAVELY_DIR / "locks"
APP_DIR = Path(__file__).parent

# Travely CLI path
TRAVELY_BIN = "/root/.local/bin/travely"

# Valid proposition statuses (lifecycle)
VALID_STATUSES = ("en_cours", "validee", "refusee", "reorientee", "en_revision", "archivee")
CONFIG_DIR = HERMES_HOME / "dashboard-unified"
CONFIG_FILE = CONFIG_DIR / "config.json"
LISTINGS_CACHE_DIR = HERMES_HOME / "dashboard-unified" / "cache"
CHAT_HISTORY_FILE = HERMES_HOME / "dashboard-unified" / "chat_history.json"
CHAT_HISTORY_MAX_DAYS = 7

# Basic auth credentials
AUTH_USER = os.environ.get("DASHBOARD_USER", "mathieu")
AUTH_PASS = os.environ.get("DASHBOARD_PASS", "toto")

# Agents configuration
AGENTS = [
    {"name": "Friday", "profile": "default", "role": "Coordinatrice", "icon": "🌟"},
    {"name": "Findy", "profile": "findy", "role": "Veille immobilière", "icon": "🏠"},
    {"name": "Spoty", "profile": "spoty", "role": "Veille automobile", "icon": "🚗"},
    {"name": "Dev", "profile": "dev", "role": "Ingénieur", "icon": "⚙️"},
    {"name": "Travely", "profile": "travely", "role": "Organisation voyages", "icon": "✈️"},
]

# Available TTS voices (edge-tts French female voices)
TTS_VOICES = [
    {"id": "fr-FR-DeniseNeural", "name": "Denise", "desc": "Voix douce et naturelle"},
    {"id": "fr-FR-EloiseNeural", "name": "Eloise", "desc": "Voix claire et expressive"},
    {"id": "fr-FR-HenrietteNeural", "name": "Henriette", "desc": "Voix chaude et posée"},
    {"id": "fr-FR-VivienneMultilingualNeural", "name": "Vivienne", "desc": "Voix multilingue (FR+EN)"},
]

# Valid actions for propositions
VALID_ACTIONS = ("valider", "reorienter", "refuser")

# --- Models ---


class CommentCreate(BaseModel):
    proposition_id: str
    text: str
    author: str = "mathieu"  # "mathieu" or "travely"


class ActionCreate(BaseModel):
    proposition_id: str
    action: str
    reorientation: Optional[str] = None
    refus_raison: Optional[str] = None


class EnrichQuery(BaseModel):
    query: str


class ChatMessage(BaseModel):
    message: str


class VoiceConfig(BaseModel):
    voice_id: str


class AdventureLogLocation(BaseModel):
    name: str
    location: str
    latitude: Optional[str] = None
    longitude: Optional[str] = None
    is_public: bool = False


class AdventureLogAddRequest(BaseModel):
    """Request to add an activity location to AdventureLog.

    The frontend sends the proposition ID and the activity index (matching
    the position in the normalized 'activites' array shown in the dashboard).
    The backend loads the RAW proposition JSON to extract the proper place
    name and address (from the 'position' field in programme-based schemas),
    geocodes the address via Nominatim (cached), then POSTs to AdventureLog
    with latitude/longitude.
    """
    prop_id: str
    activity_index: int
    activity_name: str  # fallback name if extraction fails


# --- Auth ---


def check_auth(request: Request):
    """Basic auth dependency."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Basic "):
        raise HTTPException(
            status_code=401,
            detail="Authentification requise",
            headers={"WWW-Authenticate": "Basic"},
        )
    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        user, password = decoded.split(":", 1)
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Authentification invalide",
            headers={"WWW-Authenticate": "Basic"},
        )
    if user != AUTH_USER or password != AUTH_PASS:
        raise HTTPException(
            status_code=401,
            detail="Identifiants incorrects",
            headers={"WWW-Authenticate": "Basic"},
        )


# --- Config management ---


def load_config() -> dict:
    """Load dashboard config from JSON file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    # Default config
    return {"tts_voice": "fr-FR-DeniseNeural", "chat_enabled": True}


def save_config(config: dict):
    """Save dashboard config to JSON file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


# --- App ---

app = FastAPI(title="Dashboard Unifié Hermes", version="1.0.0")


def ensure_dirs():
    """Create data directories if they don't exist."""
    PROPOSITIONS_DIR.mkdir(parents=True, exist_ok=True)
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    # Sync JSON files to SQLite at startup (non-blocking, runs in background)
    try:
        import subprocess as _sp
        _sp.Popen(
            ["python3", str(APP_DIR / "sync_json_to_sqlite.py")],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            env={**os.environ, "HOME": "/root"},
        )
    except Exception as e:
        print(f"[DASHBOARD] Initial sync failed: {e}")


# --- Travely propositions (reused from existing dashboard) ---


def normalize_proposition(data: dict, file_id: str) -> dict:
    """Normalize a proposition to the dashboard's expected schema.

    Handles multiple proposition schemas used by Travely over time:
    1. Flat schema (Azores, Porto, Bretagne): top-level 'destination', 'pays',
       'duree_jours', 'activites' (list of strings), 'budget_total'.
    2. Londres schema: 'metadata.ville_principale', 'itineraires' (day-by-day
       with matin/apres_midi/soir), 'budget_estime', 'transport.aller.option_recommandee'.
    3. Norvège schema: 'metadata.destination_principale', 'activites' (list of
       day objects with nested 'programme' arrays), 'budget.BUDGET_TOTAL_ESTIME_EUR',
       'transport.aller.etapes'.
    4. Santa Monica schema: 'metadata.ville', top-level 'programme' array with
       'position' fields, 'budget_total_estime.total', 'transport_local'.

    All are normalized to the flat schema the dashboard expects:
    id, date, destination, pays, duree_jours, transport, hebergement,
    activites[], budget_total, saison, randonnee{}
    """
    # Already has the expected flat schema
    if "destination" in data and not data.get("metadata"):
        return data

    # If it has 'destination' but ALSO has metadata, still normalize (enriched flat)
    meta = data.get("metadata", {})
    if not meta:
        return data

    # --- Destination ---
    ville = (meta.get("ville_principale", "") or
             meta.get("destination_principale", "") or
             meta.get("ville", ""))
    pays = meta.get("pays", "")
    # Santa Monica: no 'pays' field, but 'ville' contains country at the end
    if not pays and ville:
        parts = ville.split(",")
        if len(parts) >= 2:
            pays = parts[-1].strip()
    destination = ville
    if pays and ville:
        # Avoid duplicating the country if ville already ends with it
        if not ville.rstrip().endswith(pays):
            destination = f"{ville}, {pays}"
        else:
            destination = ville
    elif pays:
        destination = pays
    elif not ville:
        # Fall back to titre if no ville-like field
        destination = meta.get("titre", "Destination inconnue")

    # --- Activities ---
    activites = []
    itineraires = data.get("itineraires", [])

    # Schema: itineraires (Londres) — day-by-day with matin/apres_midi/soir
    if itineraires:
        for jour in itineraires:
            titre_jour = jour.get("titre", f"Jour {jour.get('jour', '?')}")
            for periode in ("matin", "apres_midi", "soir"):
                bloc = jour.get(periode, {})
                if not isinstance(bloc, dict):
                    continue
                act = bloc.get("activite")
                if act:
                    activites.append(f"[{titre_jour}] {act}")
                details = bloc.get("details", [])
                if isinstance(details, list):
                    for d in details:
                        if isinstance(d, str) and len(d) < 200:
                            activites.append(f"[{titre_jour}] {d}")

    # Schema: activites as list of day objects (Norvège) — each day has titre + programme
    elif "activites" in data and isinstance(data["activites"], list):
        for entry in data["activites"]:
            if isinstance(entry, dict):
                titre_jour = entry.get("titre", f"Jour {entry.get('jour', '?')}")
                resume = entry.get("resume", "")
                if resume:
                    activites.append(f"[{titre_jour}] {resume[:150]}")
                # Also extract individual programme items
                prog = entry.get("programme", [])
                if isinstance(prog, list):
                    for item in prog:
                        if isinstance(item, dict):
                            act_text = item.get("activite", "")
                            if act_text and len(act_text) < 200:
                                activites.append(f"[{titre_jour}] {act_text}")
                elif isinstance(prog, dict):
                    # Norvège J5: programme is a dict with 'journee' and 'soiree_nuit'
                    for sub_key in ("journee", "soiree_nuit"):
                        sub_items = prog.get(sub_key, [])
                        if isinstance(sub_items, list):
                            for item in sub_items:
                                if isinstance(item, dict):
                                    act_text = item.get("activite", "")
                                    if act_text and len(act_text) < 200:
                                        activites.append(f"[{titre_jour}] {act_text}")
                        elif isinstance(sub_items, dict):
                            act_text = sub_items.get("activite", "")
                            if act_text and len(act_text) < 200:
                                activites.append(f"[{titre_jour}] {act_text}")
            elif isinstance(entry, str):
                activites.append(entry)

    # Schema: programme as flat list (Santa Monica) — items with activite + position
    elif "programme" in data and isinstance(data["programme"], list):
        for entry in data["programme"]:
            if isinstance(entry, dict):
                act_text = entry.get("activite", entry.get("nom", ""))
                if act_text:
                    heure = entry.get("heure", "")
                    if heure:
                        activites.append(f"[{heure}] {act_text}")
                    else:
                        activites.append(act_text)

    # Deduplicate and limit
    seen = set()
    unique_activites = []
    for a in activites:
        if a not in seen:
            seen.add(a)
            unique_activites.append(a)
    unique_activites = unique_activites[:20]  # cap at 20

    # --- Budget ---
    budget_total = "?"
    # Try budget_estime (Londres)
    budget_data = data.get("budget_estime", {})
    if isinstance(budget_data, dict) and budget_data:
        budget_min = budget_data.get("total_min_eur")
        budget_max = budget_data.get("total_max_eur")
        budget_moyen = budget_data.get("total_moyen_estime_eur")
        if budget_moyen:
            budget_total = f"~{budget_moyen}€"
        elif budget_min and budget_max:
            budget_total = f"{budget_min}-{budget_max}€"
    # Try budget (Norvège)
    if budget_total == "?" and "budget" in data:
        bud = data["budget"]
        if isinstance(bud, dict):
            total = (bud.get("BUDGET_TOTAL_ESTIME_EUR") or
                     bud.get("budget_total_estime_eur") or
                     bud.get("total_eur"))
            if total:
                budget_total = f"~{total}€"
            else:
                # Try sub-totals
                par_jour = bud.get("budget_par_jour_eur")
                if par_jour:
                    budget_total = f"~{par_jour}€/jour"
    # Try budget_total_estime (Santa Monica)
    if budget_total == "?" and "budget_total_estime" in data:
        bud = data["budget_total_estime"]
        if isinstance(bud, dict):
            total = bud.get("total", "")
            if total:
                budget_total = str(total)
    # Fall back to flat budget_total
    if budget_total == "?":
        budget_total = str(data.get("budget_total", "?"))

    # --- Transport ---
    transport_summary = "?"
    transport_data = data.get("transport", {})
    if isinstance(transport_data, dict) and transport_data:
        # Londres: transport.aller.option_recommandee.description
        aller = transport_data.get("aller", {})
        if isinstance(aller, dict):
            opt = aller.get("option_recommandee", {})
            if isinstance(opt, dict) and opt:
                transport_summary = opt.get("description", "")
            else:
                # Norvège: transport.aller.etapes (list of step dicts)
                etapes = aller.get("etapes", [])
                if isinstance(etapes, list) and etapes:
                    moyens = []
                    for e in etapes:
                        if isinstance(e, dict):
                            moyen = e.get("moyen", "")
                            if moyen:
                                moyens.append(moyen)
                    if moyens:
                        transport_summary = " → ".join(moyens)
        total_transport = (transport_data.get("transport_total_estime_eur") or
                           transport_data.get("cout_transport_total_eur"))
        if total_transport:
            transport_summary = f"{transport_summary} ({total_transport}€)" if transport_summary != "?" else f"{total_transport}€"
    elif isinstance(transport_data, str):
        transport_summary = transport_data
    # Santa Monica: transport_local (not 'transport')
    if transport_summary == "?" and "transport_local" in data:
        tl = data["transport_local"]
        if isinstance(tl, dict):
            total = tl.get("total_transport_local", "")
            if total:
                transport_summary = str(total)
        elif isinstance(tl, str):
            transport_summary = tl

    # --- Hebergement ---
    hebergement_summary = "?"
    heberg_data = data.get("hebergement", {})
    if isinstance(heberg_data, dict) and heberg_data:
        # Londres: hebergement.recommandation.quartier
        reco = heberg_data.get("recommandation", {})
        if isinstance(reco, dict) and reco:
            quartier = reco.get("quartier", "")
            total_heb = heberg_data.get("hebergement_total_estime_eur", "")
            hebergement_summary = quartier
            if total_heb:
                hebergement_summary = f"{quartier} ({total_heb}€)" if quartier else f"{total_heb}€"
        else:
            # Norvège: hebergement.type + options
            heb_type = heberg_data.get("type", "")
            total_heb = (heberg_data.get("cout_hebergement_total_eur") or
                         heberg_data.get("hebergement_total_estime_eur"))
            hebergement_summary = heb_type
            if total_heb:
                hebergement_summary = f"{heb_type} ({total_heb}€)" if heb_type else f"{total_heb}€"
    elif isinstance(heberg_data, str):
        hebergement_summary = heberg_data

    # --- Randonnée ---
    randonnee = data.get("randonnee")
    if not randonnee:
        # Look in itineraires (Londres)
        for jour in itineraires:
            if "rando" in jour.get("titre", "").lower() or "rando" in jour.get("note", "").lower():
                matin = jour.get("matin", {})
                rando_details = matin.get("details", [])
                rando_name = jour.get("titre", "Randonnée")
                rando_acces = matin.get("transfert_detail", "")
                if rando_details:
                    for d in rando_details:
                        if isinstance(d, str) and ("sentier" in d.lower() or "randonnée" in d.lower() or "km" in d.lower()):
                            rando_name = d[:80]
                            break
                randonnee = {"nom": rando_name, "acces": rando_acces}
                break
    if not randonnee:
        # Look in activites day objects (Norvège)
        for entry in data.get("activites", []):
            if isinstance(entry, dict):
                titre = entry.get("titre", "").lower()
                if "randonnée" in titre or "rando" in titre or "raquettes" in titre:
                    rando_name = entry.get("titre", "Randonnée")
                    # Try to get access info from programme
                    prog = entry.get("programme", [])
                    rando_acces = ""
                    if isinstance(prog, list):
                        for item in prog:
                            if isinstance(item, dict):
                                dep = item.get("deplacement", "") or item.get("deplacement_vers_depart", "")
                                if dep:
                                    rando_acces = dep
                                    break
                    randonnee = {"nom": rando_name, "acces": rando_acces}
                    break
    if not randonnee:
        # Look in programme items (Santa Monica)
        for entry in data.get("programme", []):
            if isinstance(entry, dict) and entry.get("type") == "randonnee":
                randonnee = {
                    "nom": entry.get("activite", "Randonnée"),
                    "acces": entry.get("deplacement", entry.get("description", "")),
                }
                break
    if not randonnee:
        # Check dedicated field (Norvège: activite_randonnee_details)
        rando_details = data.get("activite_randonnee_details")
        if isinstance(rando_details, dict):
            randonnee = {
                "nom": rando_details.get("nom", "Randonnée"),
                "acces": rando_details.get("deplacement_vers_depart", {}).get("moyen", ""),
            }

    # --- Season ---
    saison = meta.get("climat_prevu", "") or meta.get("saison", "")
    if not saison:
        # Try periode_recommandee (Norvège)
        periode_rec = meta.get("periode_recommandee", {})
        if isinstance(periode_rec, dict):
            saison = periode_rec.get("raison", "")[:100] or periode_rec.get("fenetre", "")
    if not saison:
        periode = meta.get("periode", "")
        mois = meta.get("mois_vise", "").lower()
        if mois:
            mois_num = mois.split("-")[-1] if "-" in mois else ""
            saison_map = {"01": "Hiver", "02": "Hiver", "12": "Hiver",
                          "03": "Printemps", "04": "Printemps", "05": "Printemps",
                          "06": "Été", "07": "Été", "08": "Été",
                          "09": "Automne", "10": "Automne", "11": "Automne"}
            saison = saison_map.get(mois_num, periode)
        else:
            saison = periode

    # --- Duration ---
    duree_jours = meta.get("duree_jours", 0)
    if not duree_jours:
        # Norvège: voyageur.duree_jours
        voyageur = data.get("voyageur", {})
        if isinstance(voyageur, dict):
            duree_jours = voyageur.get("duree_jours", 0)
    if not duree_jours:
        duree_jours = data.get("duree_jours", 0)
    # Santa Monica: metadata.duree is a string like "1 journée (08:00 - 22:00)"
    if not duree_jours and meta.get("duree"):
        duree_str = str(meta["duree"])
        # Extract leading number
        import re
        m = re.match(r"(\d+)", duree_str)
        if m:
            duree_jours = int(m.group(1))

    # --- Date ---
    date = meta.get("date_creation", "") or meta.get("date", "") or data.get("date", "")

    # --- ID ---
    prop_id = data.get("id", meta.get("id", file_id))

    return {
        "id": prop_id,
        "date": date,
        "destination": destination or "Destination inconnue",
        "pays": pays,
        "duree_jours": duree_jours,
        "transport": transport_summary or "?",
        "hebergement": hebergement_summary or "?",
        "activites": unique_activites,
        "budget_total": budget_total,
        "saison": saison or "?",
        "randonnee": randonnee,
    }


def load_propositions() -> list[dict]:
    """Load all propositions from SQLite, sorted by date descending.

    Reads from the dashboard SQLite database (synced from JSON files by
    sync_json_to_sqlite.py). Normalizes raw JSON to the flat schema the
    dashboard expects. Computes 'statut' from feedback.
    """
    props = []
    raw_props = dashboard_db.get_all_propositions()
    for rp in raw_props:
        prop_id = rp["id"]
        # Load raw JSON and normalize
        raw_json = dashboard_db.get_proposition_by_id(prop_id, raw=True)
        if raw_json:
            normalized = normalize_proposition(raw_json, prop_id)
            # Override with DB-stored fields if available
            if rp.get("date"):
                normalized["date"] = rp["date"]
            props.append(normalized)
        else:
            # Fallback to the DB fields if raw JSON is missing
            props.append(rp)
    props.sort(key=lambda p: p.get("date", ""), reverse=True)
    return props


def compute_statut(prop_id: str, feedback: dict) -> str:
    """Compute the status of a proposition from its feedback.

    Priority: en_revision (lock file) > reorientee > validee > refusee > en_cours.
    Archived propositions are loaded separately from the archive dir.
    """
    # Check if Travely is currently processing this proposition
    lock_file = LOCKS_DIR / f"{prop_id}.lock"
    if lock_file.exists():
        # Check if the lock is stale (older than 10 minutes)
        try:
            lock_age = time.time() - lock_file.stat().st_mtime
            if lock_age < 600:
                return "en_revision"
            # Stale lock - remove it
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass

    action = feedback.get("action")
    if action == "valider":
        return "validee"
    if action == "refuser":
        return "refusee"
    if action == "reorienter":
        return "reorientee"
    return "en_cours"


def load_archived_propositions() -> list[dict]:
    """Load archived propositions from SQLite (statut=archivee)."""
    props = []
    all_props = dashboard_db.get_all_propositions()
    for rp in all_props:
        if rp.get("statut") == "archivee":
            raw_json = dashboard_db.get_proposition_by_id(rp["id"], raw=True)
            if raw_json:
                normalized = normalize_proposition(raw_json, rp["id"])
                normalized["statut"] = "archivee"
                props.append(normalized)
            else:
                rp["statut"] = "archivee"
                props.append(rp)
    props.sort(key=lambda p: p.get("date", ""), reverse=True)
    return props


def load_feedback() -> dict:
    """Load all feedback (comments + actions) from SQLite, keyed by proposition_id."""
    return dashboard_db.get_all_feedback()


def save_feedback_entry(entry: dict) -> str:
    """Save a feedback entry to both SQLite and a JSON file (for Travely), returns the file id."""
    entry_id = dashboard_db.insert_feedback_direct(
        prop_id=entry.get("proposition_id", ""),
        fb_type=entry.get("type", "comment"),
        action=entry.get("action"),
        text=entry.get("text"),
        author=entry.get("author", "mathieu"),
        reorientation=entry.get("reorientation"),
        refus_raison=entry.get("refus_raison"),
    )
    return entry_id


def delete_feedback_file(feedback_id: str):
    """Delete a feedback entry from both SQLite and the JSON file."""
    dashboard_db.delete_feedback(feedback_id)


# --- Travely reorientation trigger ---


def trigger_travely_reorientation(prop_id: str, reorientation_text: str):
    """Trigger Travely to process a reorientation request.

    Creates a lock file to prevent double-triggering, archives the original
    proposition, then launches Travely as a background subprocess. The subprocess:
    1. Reads the original proposition from propositions/<prop_id>.json
    2. Reads the reorientation instructions from the feedback
    3. Generates a new version of the proposition
    4. Saves the new version (replacing the old one)
    5. Deletes the reorienter feedback (so statut goes back to en_cours)
    6. Removes the lock file when done

    The lock file also serves as a status indicator for the dashboard
    (proposition shows as 'en_revision' while the lock exists).

    The original proposition is archived BEFORE Travely runs, so the old
    version is preserved in the archive directory.
    """
    ensure_dirs()
    lock_file = LOCKS_DIR / f"{prop_id}.lock"

    # Prevent double-triggering
    if lock_file.exists():
        try:
            lock_age = time.time() - lock_file.stat().st_mtime
            if lock_age < 600:  # 10 min
                print(f"[TRAVELY] Lock already exists for {prop_id}, skipping")
                return
            # Stale lock - remove it
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass

    # Archive the original proposition BEFORE Travely overwrites it
    prop_file = PROPOSITIONS_DIR / f"{prop_id}.json"
    original_prop_text = ""
    if prop_file.exists():
        try:
            with open(prop_file, "r", encoding="utf-8") as f:
                original_data = json.load(f)
                original_prop_text = json.dumps(original_data, ensure_ascii=False, indent=2)
            # Create archived copy with timestamp in filename
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            archive_filename = f"{prop_id}-v1-{timestamp}.json"
            archive_path = ARCHIVE_DIR / archive_filename
            # Give the archived copy a versioned ID so it shows separately
            original_data["id"] = f"{prop_id}-v1"
            original_data["date_archivage"] = datetime.now(timezone.utc).isoformat()
            with open(archive_path, "w", encoding="utf-8") as f:
                json.dump(original_data, f, ensure_ascii=False, indent=2)
            print(f"[TRAVELY] Archived original {prop_id} to {archive_path}")
        except (IOError, json.JSONDecodeError) as e:
            print(f"[TRAVELY] Warning: could not archive original {prop_id}: {e}")

    # Create lock file
    lock_file.write_text(json.dumps({
        "proposition_id": prop_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2))

    # Build the prompt for Travely
    # Find the reorienter feedback file to tell Travely to delete it
    feedback_files_to_delete = []
    for fpath in glob.glob(str(FEEDBACK_DIR / "*.json")):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("type") == "action" and data.get("action") == "reorienter" and data.get("proposition_id") == prop_id:
                feedback_files_to_delete.append(fpath)
        except (json.JSONDecodeError, IOError):
            continue

    delete_feedback_cmd = ""
    if feedback_files_to_delete:
        delete_feedback_cmd = "\n".join([
            f"   rm -f {fp}" for fp in feedback_files_to_delete
        ])

    prompt = (
        f"Tu es Travely. Une réorientation a été demandée pour une proposition de voyage.\n\n"
        f"PROPOSITION ORIGINALE ({prop_id}):\n{original_prop_text}\n\n"
        f"DEMANDE DE RÉORIENTATION DE MATHIEU:\n{reorientation_text}\n\n"
        f"INSTRUCTIONS:\n"
        f"1. Prends en compte les consignes de Mathieu pour modifier la proposition\n"
        f"2. Génère une nouvelle version de la proposition au format JSON avec les mêmes champs"
        f" (id, date, destination, pays, duree_jours, transport, hebergement, activites, budget_total, saison, randonnee)\n"
        f"3. L'ID de la nouvelle proposition doit être '{prop_id}' (même ID pour remplacer l'ancienne)\n"
        f"4. La date doit être la date du jour: {datetime.now().strftime('%Y-%m-%d')}\n"
        f"5. Sauvegarde le nouveau fichier JSON directement dans le fichier:"
        f" {str(PROPOSITIONS_DIR / f'{prop_id}.json')}\n"
        f"6. Supprime les fichiers de feedback de réorientation suivants (pour que la proposition"
        f" repasse en statut 'en_cours'):\n{delete_feedback_cmd}\n"
        f"7. Supprime le fichier lock: {str(lock_file)}\n"
        f"8. Affiche un résumé clair de la proposition mise à jour\n\n"
        f"Contraintes: respecte les goûts de Mathieu (randonnée, découvertes, paysages uniques)."
        f" Réaliste sur le budget et la saisonnalité. Réponds en français."
    )

    # Launch Travely as a background subprocess
    try:
        log_file = LOCKS_DIR / f"{prop_id}.log"
        log_fh = open(log_file, "w", encoding="utf-8")
        subprocess.Popen(
            [TRAVELY_BIN, "chat", "-q", prompt, "--cli", "--max-turns", "10"],
            stdout=log_fh,
            stderr=log_fh,
            env={**os.environ, "HOME": "/root", "HERMES_NON_INTERACTIVE": "1"},
            start_new_session=True,  # detach from parent
        )
        print(f"[TRAVELY] Triggered reorientation for {prop_id}")
    except Exception as e:
        print(f"[TRAVELY] Failed to trigger reorientation for {prop_id}: {e}")
        # Remove lock on failure
        lock_file.unlink(missing_ok=True)


# --- Travely comment response trigger ---


def trigger_travely_comment(prop_id: str, comment_text: str, comment_id: str):
    """Trigger Travely to respond to a simple comment from Mathieu.

    Uses a lock file (comment locks, separate from reorientation locks) to
    prevent double-triggering. Launches Travely as a background subprocess.
    The subprocess:
    1. Reads the proposition from propositions/<prop_id>.json
    2. Reads Mathieu's comment
    3. Generates a response
    4. Saves the response as a new comment with author='travely'
    5. Removes the lock file when done

    The lock file also serves as a status indicator for the dashboard
    (shows "Travely is responding..." while the lock exists).
    """
    ensure_dirs()
    comment_locks_dir = LOCKS_DIR / "comments"
    comment_locks_dir.mkdir(parents=True, exist_ok=True)
    lock_file = comment_locks_dir / f"{prop_id}.lock"

    # Prevent double-triggering
    if lock_file.exists():
        try:
            lock_age = time.time() - lock_file.stat().st_mtime
            if lock_age < 600:  # 10 min
                print(f"[TRAVELY-COMMENT] Lock already exists for {prop_id}, skipping")
                return
            # Stale lock - remove it
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass

    # Read the proposition for context
    prop_file = PROPOSITIONS_DIR / f"{prop_id}.json"
    prop_text = ""
    if prop_file.exists():
        try:
            with open(prop_file, "r", encoding="utf-8") as f:
                prop_data = json.load(f)
                prop_text = json.dumps(prop_data, ensure_ascii=False, indent=2)
        except (IOError, json.JSONDecodeError) as e:
            print(f"[TRAVELY-COMMENT] Warning: could not read proposition {prop_id}: {e}")

    # Create lock file
    lock_file.write_text(json.dumps({
        "proposition_id": prop_id,
        "comment_id": comment_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2))

    # Build the prompt for Travely
    response_feedback_dir = str(FEEDBACK_DIR)

    prompt = (
        f"Tu es Travely. Mathieu a pose une question ou fait un commentaire sur une proposition de voyage.\n\n"
        f"PROPOSITION ({prop_id}):\n{prop_text}\n\n"
        f"COMMENTAIRE DE MATHIEU:\n{comment_text}\n\n"
        f"INSTRUCTIONS:\n"
        f"1. Reponds au commentaire de Mathieu de facon concise et utile\n"
        f"2. Ta reponse doit etre en francais, claire et directe\n"
        f"3. Utilise les informations de la proposition pour contextualiser ta reponse\n"
        f"4. Sauvegarde ta reponse en executant ce script Python:\n"
        f"```python\n"
        f"import json, uuid\n"
        f"from datetime import datetime, timezone\n"
        f"entry = {{\n"
        f"    \"type\": \"comment\",\n"
        f"    \"proposition_id\": \"{prop_id}\",\n"
        f"    \"text\": TA_REPONSE_ICI,\n"
        f"    \"author\": \"travely\",\n"
        f"}}\n"
        f"entry[\"feedback_id\"] = uuid.uuid4().hex[:8]\n"
        f"entry[\"date\"] = datetime.now(timezone.utc).isoformat()\n"
        f"with open(f\"{response_feedback_dir}/{{entry[\'feedback_id\']}}.json\", \"w\") as f:\n"
        f"    json.dump(entry, f, ensure_ascii=False, indent=2)\n"
        f"```\n"
        f"5. Remplace TA_REPONSE_ICI par ta reponse textuelle (sans guillemets autour, mets-la comme valeur string)\n"
        f"6. Apres avoir sauvegarde ta reponse, supprime le fichier lock: {str(lock_file)}\n"
        f"7. Affiche un resume de ta reponse\n\n"
        f"Contraintes: Reponds en francais. Sois concis mais complet. Respecte les gouts de Mathieu"
        f" (randonnee, decouvertes, paysages uniques)."
    )

    # Launch Travely as a background subprocess
    try:
        log_file = comment_locks_dir / f"{prop_id}.log"
        log_fh = open(log_file, "w", encoding="utf-8")
        subprocess.Popen(
            [TRAVELY_BIN, "chat", "-q", prompt, "--cli", "--max-turns", "10"],
            stdout=log_fh,
            stderr=log_fh,
            env={**os.environ, "HOME": "/root", "HERMES_NON_INTERACTIVE": "1"},
            start_new_session=True,  # detach from parent
        )
        print(f"[TRAVELY-COMMENT] Triggered comment response for {prop_id}")
    except Exception as e:
        print(f"[TRAVELY-COMMENT] Failed to trigger comment response for {prop_id}: {e}")
        # Remove lock on failure
        lock_file.unlink(missing_ok=True)


# --- Wikimedia enrichment (reused from existing dashboard) ---


def _cache_key(query: str) -> str:
    return hashlib.md5(query.encode("utf-8")).hexdigest()


def _load_cache(query: str) -> Optional[dict]:
    fpath = CACHE_DIR / f"{_cache_key(query)}.json"
    if fpath.exists():
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None
    return None


def _save_cache(query: str, data: dict):
    fpath = CACHE_DIR / f"{_cache_key(query)}.json"
    try:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except IOError:
        pass


def _http_get_json(url: str, timeout: int = 10) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": "HermesDashboard/1.0 (https://wired-lan.fr)"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _search_wikimedia_commons(query: str, limit: int = 3) -> list[dict]:
    images = []
    try:
        search_url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
            "action": "query", "list": "search", "srsearch": query,
            "srnamespace": "6", "srlimit": str(limit),
            "format": "json", "origin": "*",
        })
        data = _http_get_json(search_url)
        results = data.get("query", {}).get("search", [])
        if not results:
            return images
        titles = [r["title"] for r in results if r["title"].startswith("File:")]
        if not titles:
            return images
        info_url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({
            "action": "query", "titles": "|".join(titles),
            "prop": "imageinfo", "iiprop": "url|extmetadata",
            "iiurlwidth": "800", "format": "json", "origin": "*",
        })
        info_data = _http_get_json(info_url)
        pages = info_data.get("query", {}).get("pages", {})
        for page in pages.values():
            if page.get("page_id") == "-1" or "pageid" not in page:
                pass
            imageinfo = page.get("imageinfo", [{}])[0]
            thumb_url = imageinfo.get("thumburl", imageinfo.get("url", ""))
            full_url = imageinfo.get("url", "")
            title = page.get("title", "").replace("File:", "")
            if thumb_url:
                ext = thumb_url.lower().split(".")[-1].split("?")[0]
                if ext in ("jpg", "jpeg", "png", "gif", "svg", "webp", "tif", "tiff"):
                    images.append({
                        "url": full_url, "thumb_url": thumb_url,
                        "title": title, "source": "Wikimedia Commons",
                    })
    except Exception as e:
        print(f"[WARN] Wikimedia Commons search failed for '{query}': {e}")
    return images[:limit]


def _search_wikipedia(query: str) -> Optional[dict]:
    try:
        search_url = "https://fr.wikipedia.org/w/api.php?" + urllib.parse.urlencode({
            "action": "query", "list": "search", "srsearch": query,
            "srlimit": "1", "format": "json", "origin": "*",
        })
        data = _http_get_json(search_url)
        results = data.get("query", {}).get("search", [])
        if results:
            title = results[0]["title"]
            return {
                "url": f"https://fr.wikipedia.org/wiki/{urllib.parse.quote(title)}",
                "title": title, "source": "Wikipedia (fr)",
            }
    except Exception:
        pass
    return None


def enrich_activity(query: str) -> dict:
    cached = _load_cache(query)
    if cached is not None:
        return cached
    result = {"query": query, "images": [], "link": None}
    result["images"] = _search_wikimedia_commons(query, limit=3)
    result["link"] = _search_wikipedia(query)
    _save_cache(query, result)
    return result


# --- System status helpers ---


def get_service_status(service_name: str) -> dict:
    """Check if a systemd service is running."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service_name],
            capture_output=True, text=True, timeout=5
        )
        active = result.stdout.strip() == "active"
        return {"name": service_name, "active": active, "status": result.stdout.strip()}
    except Exception:
        return {"name": service_name, "active": False, "status": "unknown"}


def get_service_uptime(service_name: str) -> Optional[str]:
    """Get the uptime of a systemd service."""
    try:
        result = subprocess.run(
            ["systemctl", "show", service_name, "--property=ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=5
        )
        ts = result.stdout.strip().replace("ActiveEnterTimestamp=", "")
        if ts:
            # Parse the timestamp and compute uptime
            dt = datetime.fromisoformat(ts)
            delta = datetime.now() - dt
            days = delta.days
            hours, rem = divmod(delta.seconds, 3600)
            minutes, _ = divmod(rem, 60)
            if days > 0:
                return f"{days}j {hours}h"
            elif hours > 0:
                return f"{hours}h {minutes}m"
            else:
                return f"{minutes}m"
    except Exception:
        pass
    return None


def get_cron_jobs() -> list[dict]:
    """Load cron jobs from the Hermes cron config."""
    jobs = []
    cron_file = HERMES_HOME / "cron" / "jobs.json"
    if cron_file.exists():
        try:
            with open(cron_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                for job in data.get("jobs", []):
                    # Determine which agent this job belongs to
                    name = job.get("name", "")
                    agent = "unknown"
                    if "findy" in name.lower():
                        agent = "Findy"
                    elif "spoty" in name.lower():
                        agent = "Spoty"
                    elif "travely" in name.lower():
                        agent = "Travely"
                    elif "adventurelog" in name.lower() or "sync" in name.lower():
                        agent = "Dev"

                    jobs.append({
                        "id": job.get("id", ""),
                        "name": name,
                        "agent": agent,
                        "schedule": job.get("schedule_display", ""),
                        "enabled": job.get("enabled", False),
                        "last_status": job.get("last_status"),
                        "last_run_at": job.get("last_run_at"),
                        "next_run_at": job.get("next_run_at"),
                        "no_agent": job.get("no_agent", False),
                    })
        except Exception:
            pass
    return jobs


def get_kanban_task_counts() -> dict:
    """Get kanban task counts per assignee."""
    import sqlite3
    db_path = str(HERMES_HOME / "kanban.db")
    counts = {}
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT assignee, status, count(*) FROM tasks GROUP BY assignee, status"
        )
        for row in cur.fetchall():
            assignee, status, cnt = row
            if assignee not in counts:
                counts[assignee] = {}
            counts[assignee][status] = cnt
        conn.close()
    except Exception:
        pass
    return counts


def get_profile_status(profile: str) -> dict:
    """Check if a Hermes profile gateway is running.

    The default profile runs as hermes-gateway.service.
    Other profiles are checked via their PID file.
    """
    if profile == "default":
        # The default gateway is the main systemd service
        return {"profile": profile, "running": get_service_status("hermes-gateway.service")["active"]}

    # For other profiles, check PID file
    pid_file = HERMES_HOME / "profiles" / profile / "gateway.pid"
    if pid_file.exists():
        try:
            pid_str = pid_file.read_text().strip()
            if pid_str:
                pid = int(pid_str)
                try:
                    os.kill(pid, 0)
                    return {"profile": profile, "running": True}
                except (OSError, ProcessLookupError):
                    pass
        except (ValueError, IOError):
            pass
    return {"profile": profile, "running": False}


# --- Nextcloud Tables API ---


def get_nc_credentials() -> Optional[dict]:
    """Load Nextcloud credentials from .env file."""
    env_file = HERMES_HOME / ".env"
    if not env_file.exists():
        return None
    creds = {}
    try:
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("NEXTCLOUD_") and "=" in line:
                    key, value = line.split("=", 1)
                    creds[key] = value.strip('"').strip("'")
    except Exception:
        pass
    return creds if creds else None


# Column mappings for Nextcloud Tables
IMMO_COLUMNS = {19: "titre", 20: "prix", 21: "surface", 22: "url", 23: "statut"}
IMMO_STATUT_LABELS = {0: "Intéressant", 1: "Pas intéressant", 2: "Trop tard"}

VEHICULE_COLUMNS = {25: "titre", 26: "prix", 27: "annee", 28: "kilometrage",
                     29: "carburant", 30: "portes", 31: "localisation", 32: "url"}


def parse_nc_rows(rows: list, col_map: dict) -> list[dict]:
    """Parse Nextcloud Tables rows into flat dicts using column mapping."""
    result = []
    for row in rows:
        item = {"row_id": row.get("id"), "created_at": row.get("createdAt", "")}
        for cell in row.get("data", []):
            col_id = cell.get("columnId")
            val = cell.get("value")
            if col_id in col_map:
                field = col_map[col_id]
                # Parse URL fields (stored as JSON string with title/value)
                if field == "url" and val and isinstance(val, str):
                    try:
                        url_data = json.loads(val)
                        val = url_data.get("value", url_data.get("title", val))
                    except (json.JSONDecodeError, TypeError):
                        pass
                item[field] = val
        result.append(item)
    return result


_NC_CACHE = {}  # {table_id: (timestamp, data)}
_NC_CACHE_TTL = 120  # 2 min

def fetch_nc_tables(table_id: int) -> list[dict]:
    """Fetch rows from a Nextcloud Tables API and parse them."""
    cached = _NC_CACHE.get(table_id)
    if cached and (time.time() - cached[0]) < _NC_CACHE_TTL:
        return cached[1]
    
    creds = get_nc_credentials()
    if not creds:
        return []
    base_url = creds.get("NEXTCLOUD_BASE_URL", creds.get("NEXTCLOUD_URL", "https://cloud.wired-lan.fr"))
    user = creds.get("NEXTCLOUD_LOGIN", creds.get("NEXTCLOUD_USER", ""))
    password = creds.get("NEXTCLOUD_PASSWORD", creds.get("NEXTCLOUD_PASS", ""))
    if not user or not password:
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
            # Parse rows based on table ID
            if table_id == 4:
                parsed = parse_nc_rows(raw_rows, IMMO_COLUMNS)
                for item in parsed:
                    statut_val = item.get("statut")
                    item["statut_label"] = IMMO_STATUT_LABELS.get(statut_val, "Nouveau")
                _NC_CACHE[table_id] = (time.time(), parsed)
                return parsed
            elif table_id == 5:
                result = parse_nc_rows(raw_rows, VEHICULE_COLUMNS)
                _NC_CACHE[table_id] = (time.time(), result)
                return result
            result = raw_rows
            _NC_CACHE[table_id] = (time.time(), result)
            return result
    except Exception as e:
        print(f"[WARN] NC Tables fetch failed for table {table_id}: {e}")
        return []


# --- Routes: Page principale ---


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, _=Depends(check_auth)):
    """Serve the main dashboard SPA."""
    index_path = APP_DIR / "static" / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>index.html not found</h1>", status_code=404)


# --- Routes: API statut système ---


@app.get("/api/status")
async def api_status(_=Depends(check_auth)):
    """Return real-time system status: services, agents, cron, kanban."""
    import concurrent.futures

    # Services (fast - systemctl calls)
    services = []
    for svc in ["hermes-dashboard.service", "hermes-gateway.service", "hermes-unified-dashboard.service"]:
        s = get_service_status(svc)
        s["uptime"] = get_service_uptime(svc)
        services.append(s)

    # Cron jobs (fast - file read)
    cron_jobs = get_cron_jobs()

    # Kanban task counts (fast - SQLite read)
    kanban_counts = get_kanban_task_counts()

    # Agents status - run profile checks in parallel for speed
    agents_status = []
    loop = asyncio.get_event_loop()
    profile_tasks = []
    for agent in AGENTS:
        profile_tasks.append((agent, loop.run_in_executor(None, get_profile_status, agent["profile"])))

    for agent, task in zip(AGENTS, [pt[1] for pt in profile_tasks]):
        gw = await task
        agent_crons = [j for j in cron_jobs if j["agent"] == agent["name"]]
        last_cron = agent_crons[0] if agent_crons else None
        kanban_tasks = kanban_counts.get(agent["profile"], {})
        running_tasks = kanban_tasks.get("running", 0)
        ready_tasks = kanban_tasks.get("ready", 0)
        total_tasks = sum(kanban_tasks.values())

        agents_status.append({
            "name": agent["name"],
            "profile": agent["profile"],
            "role": agent["role"],
            "icon": agent["icon"],
            "gateway_running": gw["running"],
            "last_cron": last_cron,
            "kanban_running": running_tasks,
            "kanban_ready": ready_tasks,
            "kanban_total": total_tasks,
        })

    return {
        "services": services,
        "agents": agents_status,
        "cron_jobs": cron_jobs,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# --- Routes: Voyages (Travely) ---


@app.get("/api/propositions")
async def api_propositions(_=Depends(check_auth)):
    """Return all propositions with their associated feedback and computed status.

    Includes both active propositions (in propositions dir) and archived ones
    (replaced by reoriented versions). Each proposition gets a 'statut' field.
    """
    props = load_propositions()
    feedback = load_feedback()
    for prop in props:
        prop_id = prop["id"]
        fb = feedback.get(prop_id, {
            "comments": [], "action": None,
            "action_detail": None, "action_date": None,
        })
        prop["feedback"] = fb
        prop["statut"] = compute_statut(prop_id, fb)

    # Also load archived propositions
    archived = load_archived_propositions()
    for prop in archived:
        prop_id = prop["id"]
        fb = feedback.get(prop_id, {
            "comments": [], "action": None,
            "action_detail": None, "action_date": None,
        })
        prop["feedback"] = fb

    all_props = props + archived
    return {"propositions": all_props}


@app.post("/api/comments")
async def api_add_comment(comment: CommentCreate, _=Depends(check_auth)):
    if not comment.text.strip():
        raise HTTPException(status_code=400, detail="Le commentaire ne peut pas être vide")
    entry = {
        "type": "comment",
        "proposition_id": comment.proposition_id,
        "text": comment.text.strip(),
        "author": comment.author,  # "mathieu" or "travely"
    }
    fid = save_feedback_entry(entry)

    # Trigger Travely if the comment is from Mathieu (not from Travely itself)
    if comment.author == "mathieu":
        trigger_travely_comment(comment.proposition_id, comment.text.strip(), fid)

    return {"status": "ok", "comment_id": fid}


@app.delete("/api/comments/{comment_id}")
async def api_delete_comment(comment_id: str, _=Depends(check_auth)):
    fpath = FEEDBACK_DIR / f"{comment_id}.json"
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="Commentaire introuvable")
    delete_feedback_file(comment_id)
    return {"status": "ok"}


@app.post("/api/actions")
async def api_set_action(action: ActionCreate, _=Depends(check_auth)):
    if action.action not in VALID_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Action invalide")
    if action.action == "reorienter" and not action.reorientation:
        raise HTTPException(status_code=400, detail="La réorientation doit être expliquée")
    if action.action == "refuser" and not action.refus_raison:
        raise HTTPException(status_code=400, detail="La raison du refus est obligatoire")
    # Remove previous action for this proposition
    for fpath in glob.glob(str(FEEDBACK_DIR / "*.json")):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("type") == "action" and data.get("proposition_id") == action.proposition_id:
                delete_feedback_file(data.get("feedback_id", ""))
        except (json.JSONDecodeError, IOError):
            continue
    entry = {
        "type": "action",
        "proposition_id": action.proposition_id,
        "action": action.action,
        "reorientation": action.reorientation.strip() if action.reorientation else None,
        "refus_raison": action.refus_raison.strip() if action.refus_raison else None,
    }
    fid = save_feedback_entry(entry)

    # If reorientation, trigger Travely asynchronously
    if action.action == "reorienter":
        trigger_travely_reorientation(action.proposition_id, action.reorientation.strip())

    return {"status": "ok", "action_id": fid}


@app.delete("/api/actions/{proposition_id}")
async def api_clear_action(proposition_id: str, _=Depends(check_auth)):
    removed = 0
    for fpath in glob.glob(str(FEEDBACK_DIR / "*.json")):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("type") == "action" and data.get("proposition_id") == proposition_id:
                delete_feedback_file(data.get("feedback_id", ""))
                removed += 1
        except (json.JSONDecodeError, IOError):
            continue
    if removed == 0:
        raise HTTPException(status_code=404, detail="Aucune action à effacer")
    return {"status": "ok", "removed": removed}


@app.get("/api/reorientation/status/{proposition_id}")
async def api_reorientation_status(proposition_id: str, _=Depends(check_auth)):
    """Check the status of a Travely reorientation process.

    Returns whether Travely is currently processing (en_revision) or done.
    Also returns the log output if available.
    """
    ensure_dirs()
    lock_file = LOCKS_DIR / f"{proposition_id}.lock"
    log_file = LOCKS_DIR / f"{proposition_id}.log"

    processing = False
    log_content = ""

    if lock_file.exists():
        try:
            lock_age = time.time() - lock_file.stat().st_mtime
            if lock_age < 600:  # 10 min
                processing = True
            else:
                # Stale lock - clean up
                lock_file.unlink(missing_ok=True)
        except OSError:
            pass

    if log_file.exists():
        try:
            log_content = log_file.read_text(encoding="utf-8")[-2000:]  # last 2KB
        except IOError:
            pass

    return {
        "proposition_id": proposition_id,
        "processing": processing,
        "log": log_content,
    }


@app.get("/api/comment/status/{proposition_id}")
async def api_comment_status(proposition_id: str, _=Depends(check_auth)):
    """Check the status of a Travely comment response.

    Returns whether Travely is currently processing a comment response.
    Uses a separate lock directory from reorientation locks.
    """
    ensure_dirs()
    comment_locks_dir = LOCKS_DIR / "comments"
    lock_file = comment_locks_dir / f"{proposition_id}.lock"
    log_file = comment_locks_dir / f"{proposition_id}.log"

    processing = False
    log_content = ""

    if lock_file.exists():
        try:
            lock_age = time.time() - lock_file.stat().st_mtime
            if lock_age < 600:  # 10 min
                processing = True
            else:
                # Stale lock - clean up
                lock_file.unlink(missing_ok=True)
        except OSError:
            pass

    if log_file.exists():
        try:
            log_content = log_file.read_text(encoding="utf-8")[-2000:]  # last 2KB
        except IOError:
            pass

    return {
        "proposition_id": proposition_id,
        "processing": processing,
        "log": log_content,
    }


@app.post("/api/enrich")
async def api_enrich(query: EnrichQuery, _=Depends(check_auth)):
    if not query.query.strip():
        raise HTTPException(status_code=400, detail="La requête ne peut pas être vide")
    result = enrich_activity(query.query.strip())
    return result


@app.get("/api/proposition/{prop_id}/map")
async def api_proposition_map(prop_id: str, _=Depends(check_auth)):
    """Return map data for a proposition, using cached coordinates from SQLite.

    Coordinates are geocoded once (via Nominatim) and stored permanently in the
    programme_items table. On subsequent calls, the cached coordinates are read
    directly from SQLite — no Nominatim calls.

    Only points that have never been geocoded (latitude IS NULL) will trigger
    a Nominatim call. This respects Nominatim usage policy and makes map
    loading instant for already-geocoded propositions.
    """
    prop = _load_proposition_by_id(prop_id, raw=True)
    if prop is None:
        raise HTTPException(status_code=404, detail="Proposition introuvable")

    points = _extract_map_points(prop)
    display_dest = _get_display_destination(prop)
    if not points:
        return {"proposition_id": prop_id, "destination": display_dest, "points": [], "geocoded": 0, "failed": 0}

    # Check which points already have cached coordinates in programme_items
    cached_items = dashboard_db.get_programme_items(prop_id)
    # Build a lookup by activity_index (for programme-based schemas) or by address hash
    # Items with latitude IS NULL have never been geocoded → need geocoding
    # Items with latitude=0, lon=0 were tried and failed → skip (don't retry)
    # Items with real lat/lon are cached → use directly
    cached_by_index = {}
    failed_indices = set()
    for item in cached_items:
        idx = item["activity_index"]
        lat = item.get("latitude")
        lon = item.get("longitude")
        if lat is not None and lon is not None and lat != 0 and lon != 0:
            cached_by_index[idx] = item
        elif lat is not None and lon is not None and lat == 0 and lon == 0:
            failed_indices.add(idx)

    # Separate points into: already cached (instant) and needing geocoding (Nominatim)
    instant_results = []
    to_geocode = []
    for p in points:
        idx = p["index"]
        if idx in cached_by_index:
            cached = cached_by_index[idx]
            instant_results.append({
                "index": idx,
                "label": p["label"],
                "type": p["type"],
                "heure": p.get("heure", ""),
                "cout": p.get("cout", ""),
                "description": p.get("description", ""),
                "lat": cached["latitude"],
                "lon": cached["longitude"],
                "display_name": cached.get("display_name", "") or p["address"],
                "address": p["address"],
            })
        elif idx in failed_indices:
            # Already tried and failed — skip (don't retry Nominatim)
            pass
        else:
            to_geocode.append(p)

    # Geocode missing points in a background thread (Nominatim, rate-limited)
    newly_geocoded = []
    if to_geocode:
        loop = asyncio.get_event_loop()

        def geocode_missing():
            results = []
            for p in to_geocode:
                skip_dest = p["index"] > 0
                geo = geocode_with_fallbacks(p["address"], skip_destination_fallback=skip_dest)
                if geo:
                    results.append({
                        "index": p["index"],
                        "label": p["label"],
                        "type": p["type"],
                        "heure": p.get("heure", ""),
                        "cout": p.get("cout", ""),
                        "description": p.get("description", ""),
                        "lat": geo["lat"],
                        "lon": geo["lon"],
                        "display_name": geo["display_name"],
                        "address": p["address"],
                    })
                else:
                    # Mark as failed (lat=0, lon=0) so we don't retry Nominatim every time
                    _cache_map_point_failed(prop_id, p)
            return results

        newly_geocoded = await loop.run_in_executor(None, geocode_missing)

        # Persist the newly geocoded coordinates in programme_items (geocode once, forever)
        for geo_point in newly_geocoded:
            _cache_map_point_coords(prop_id, geo_point)

    all_results = instant_results + newly_geocoded
    # Sort by index to maintain consistent ordering
    all_results.sort(key=lambda r: r["index"])

    return {
        "proposition_id": prop_id,
        "destination": display_dest,
        "points": all_results,
        "geocoded": len(all_results),
        "failed": len(points) - len(all_results),
    }


def _cache_map_point_failed(prop_id: str, point: dict):
    """Mark a map point as failed geocoding (lat=0, lon=0) to avoid retrying."""
    conn = dashboard_db.get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id FROM programme_items WHERE proposition_id = ? AND activity_index = ?
    """, (prop_id, point["index"]))
    row = cur.fetchone()
    if row:
        cur.execute("""
            UPDATE programme_items SET latitude = 0, longitude = 0, geocoded_at = ?
            WHERE id = ?
        """, (time.time(), row["id"]))
    else:
        cur.execute("""
            INSERT INTO programme_items (proposition_id, activity_index, activite, type,
                                          latitude, longitude, geocoded_at)
            VALUES (?, ?, ?, ?, 0, 0, ?)
        """, (prop_id, point["index"], point.get("label", ""),
              point.get("type", "default"), time.time()))
    conn.commit()
    conn.close()


# --- Routes: Listings cache helpers ---


GEOCODE_CACHE_DIR = TRAVELY_DIR / "cache" / "geocode"
init_cache_dir(GEOCODE_CACHE_DIR)


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


def _extract_map_points(prop: dict) -> list[dict]:
    """Extract mappable points from a proposition.

    Handles two schemas:
    1. Structured: prop has "programme" array with objects having "position" and "type"
    2. Flat: prop has "activites" as list of strings — geocode destination + each activity

    Returns a list of dicts:
        {"label": str, "address": str, "type": str, "index": int}

    The "type" field is used for marker coloring:
        plage=blue, culture=red, gastronomie=orange, randonnee=green, transport=gray, default=sky
    """
    points = []

    # Schema 1: structured "programme" array with "position" fields
    if "programme" in prop and isinstance(prop["programme"], list):
        has_positions = any(
            isinstance(entry, dict) and entry.get("position")
            for entry in prop["programme"]
        )
        if has_positions:
            for i, entry in enumerate(prop["programme"]):
                if not isinstance(entry, dict):
                    continue
                position = entry.get("position", "")
                if not position:
                    continue
                points.append({
                    "label": entry.get("activite", entry.get("nom", f"Étape {i+1}")),
                    "address": position,
                    "type": entry.get("type", "default"),
                    "heure": entry.get("heure", ""),
                    "cout": entry.get("cout", ""),
                    "description": entry.get("description", ""),
                    "index": i,
                })
            return points
        # If programme has no positions, fall through to schema 1b/3/2

    # Schema 1b: "activites" as list of day objects (Norvège) — each day has titre + programme
    if not points and "activites" in prop and isinstance(prop["activites"], list):
        has_day_objects = any(
            isinstance(entry, dict) and (entry.get("programme") or entry.get("titre"))
            for entry in prop["activites"]
        )
        if has_day_objects:
            destination = _get_display_destination(prop)
            # First point: destination
            if destination:
                points.append({
                    "label": f"📍 {destination}",
                    "address": destination,
                    "type": "default",
                    "heure": "",
                    "cout": "",
                    "description": "",
                    "index": 0,
                })
            for entry in prop["activites"]:
                if not isinstance(entry, dict):
                    continue
                titre_jour = entry.get("titre", f"Jour {entry.get('jour', '?')}")
                prog = entry.get("programme", [])
                # programme can be a list (most days) or a dict (Norvège J5)
                prog_items = []
                if isinstance(prog, list):
                    prog_items = prog
                elif isinstance(prog, dict):
                    # journee is a list, soiree_nuit may be a dict or a list
                    j = prog.get("journee", [])
                    if isinstance(j, list):
                        prog_items.extend(j)
                    elif isinstance(j, dict):
                        prog_items.append(j)
                    s = prog.get("soiree_nuit", [])
                    if isinstance(s, list):
                        prog_items.extend(s)
                    elif isinstance(s, dict):
                        prog_items.append(s)
                for item in prog_items:
                    if not isinstance(item, dict):
                        continue
                    act_text = item.get("activite", "")
                    if not act_text or not act_text.strip():
                        continue
                    # Try to find a location in the deplacement field or use activity text
                    deplacement = (item.get("deplacement", "") or
                                   item.get("deplacement_vers_depart", "") or
                                   item.get("note_deplacement", ""))
                    if deplacement:
                        location_query = f"{deplacement}, {destination}" if destination else deplacement
                    else:
                        location_query = f"{act_text[:100]}, {destination}" if destination else act_text[:100]
                    points.append({
                        "label": f"[{titre_jour}] {act_text[:80]}",
                        "address": location_query,
                        "type": _infer_activity_type(act_text),
                        "heure": item.get("heure", ""),
                        "cout": str(item.get("cout_eur", item.get("cost_eur", ""))),
                        "description": "",
                        "index": len(points),
                    })
            if points:
                return points

    # Schema 3: "itineraires" (day-by-day with matin/apres_midi/soir, no positions)
    if "itineraires" in prop and isinstance(prop["itineraires"], list):
        destination = _get_display_destination(prop)
        for i, jour in enumerate(prop["itineraires"]):
            if not isinstance(jour, dict):
                continue
            titre_jour = jour.get("titre", f"Jour {jour.get('jour', i+1)}")
            for periode in ("matin", "apres_midi", "soir"):
                bloc = jour.get(periode, {})
                if not isinstance(bloc, dict):
                    continue
                act = bloc.get("activite")
                if act and isinstance(act, str):
                    query = f"{act}, {destination}" if destination else act
                    points.append({
                        "label": f"[{titre_jour}] {act}",
                        "address": query,
                        "type": _infer_activity_type(act),
                        "heure": bloc.get("horaires", ""),
                        "cout": bloc.get("budget_repas_eur", ""),
                        "description": bloc.get("transfert", ""),
                        "index": len(points),
                    })
                details = bloc.get("details", [])
                if isinstance(details, list):
                    for d in details:
                        if isinstance(d, str) and len(d) < 200:
                            query = f"{d}, {destination}" if destination else d
                            points.append({
                                "label": f"[{titre_jour}] {d[:80]}",
                                "address": query,
                                "type": _infer_activity_type(d),
                                "heure": "",
                                "cout": "",
                                "description": "",
                                "index": len(points),
                            })
        if points:
            return points

    # Schema 2: flat "activites" as strings OR day objects
    destination = _get_display_destination(prop) or str(prop.get("destination", "") or "")
    activites = prop.get("activites", [])
    if not isinstance(activites, list):
        activites = []

    # First point: the destination itself (center of the trip)
    if destination:
        points.append({
            "label": f"📍 {destination}",
            "address": destination,
            "type": "default",
            "heure": "",
            "cout": "",
            "description": "",
            "index": 0,
        })

    # Additional points: each activity (geocoded with destination as context)
    for i, act in enumerate(activites):
        if isinstance(act, str):
            if not act.strip():
                continue
            clean_act = act.strip()
            query = f"{clean_act}, {destination}" if destination else clean_act
            points.append({
                "label": clean_act,
                "address": query,
                "type": _infer_activity_type(clean_act),
                "heure": "",
                "cout": "",
                "description": "",
                "index": len(points),
            })
        elif isinstance(act, dict):
            # Day object: extract titre and resume
            titre = act.get("titre", "")
            resume = act.get("resume", "")
            if titre:
                query = f"{titre}, {destination}" if destination else titre
                points.append({
                    "label": titre,
                    "address": query,
                    "type": _infer_activity_type(titre),
                    "heure": act.get("date", ""),
                    "cout": "",
                    "description": resume[:200] if resume else "",
                    "index": len(points),
                })

    return points


def _infer_activity_type(activity: str) -> str:
    """Infer activity type from text for marker coloring."""
    text = activity.lower()
    if any(w in text for w in ("plage", "baignade", "bain", "nage", "surf")):
        return "plage"
    if any(w in text for w in ("musée", "visite", "abbaye", "cathédrale", "église", "château", "monument", "pier", "culture")):
        return "culture"
    if any(w in text for w in ("randonnée", "sentier", "gr34", "rando", "marche", "cratère", "crater", "cap", "pointe", "vallée", "pic")):
        return "randonnee"
    if any(w in text for w in ("dégustation", "vin", "cave", "cuisine", "restaurant", "gastronomie", "marché")):
        return "gastronomie"
    if any(w in text for w in ("vol", "train", "bus", "transport", "aéroport", "gare", "navette")):
        return "transport"
    return "default"


def _load_proposition_by_id(prop_id: str, raw: bool = False) -> Optional[dict]:
    """Load a single proposition by its ID from SQLite.

    If raw=True, return the raw JSON without normalization (preserves 'programme'
    field needed for map point extraction and AdventureLog place extraction).
    """
    return dashboard_db.get_proposition_by_id(prop_id, raw=raw)


def _get_display_destination(prop: dict) -> str:
    """Extract a human-readable destination from a proposition (raw or normalized).

    Tries multiple field names across the various proposition schemas:
    - Flat schema: prop['destination']
    - Dict schema (Maroc): prop['destination'] as {"ville_principale": "...", "pays": "..."}
    - Santa Monica metadata: metadata['ville']
    - Norge metadata: metadata['destination_principale'] + metadata['pays']
    - Londres metadata: metadata['ville_principale'] + metadata['pays']
    """
    dest = prop.get("destination")
    if dest:
        if isinstance(dest, dict):
            ville = dest.get("ville_principale", "") or dest.get("ville", "")
            pays = dest.get("pays", "")
            if ville and pays:
                return f"{ville}, {pays}"
            return ville or pays or ""
        return str(dest)
    meta = prop.get("metadata", {})
    if meta:
        ville = (meta.get("ville_principale", "") or
                 meta.get("destination_principale", "") or
                 meta.get("ville", ""))
        pays = meta.get("pays", "")
        titre = meta.get("titre", "")
        if ville and pays:
            return f"{ville}, {pays}"
        if ville:
            return ville
        if titre:
            return titre
    return ""


def _cache_map_point_coords(prop_id: str, geo_point: dict):
    """Persist geocoded coordinates for a map point in programme_items.

    Called after Nominatim geocoding to cache the result permanently.
    If the programme_item row doesn't exist yet (e.g. for flat-schema propositions
    where activities are stored as strings), create one.
    """
    conn = dashboard_db.get_conn()
    cur = conn.cursor()
    # Try to find an existing programme_item with this index
    cur.execute("""
        SELECT id FROM programme_items WHERE proposition_id = ? AND activity_index = ?
    """, (prop_id, geo_point["index"]))
    row = cur.fetchone()
    if row:
        cur.execute("""
            UPDATE programme_items SET latitude = ?, longitude = ?, display_name = ?,
                                       geocoded_at = ?
            WHERE id = ?
        """, (geo_point["lat"], geo_point["lon"], geo_point.get("display_name", ""),
              time.time(), row["id"]))
    else:
        # Create a new programme_item row for this point
        cur.execute("""
            INSERT INTO programme_items (proposition_id, activity_index, activite, type,
                                          latitude, longitude, display_name, geocoded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (prop_id, geo_point["index"], geo_point.get("label", ""),
              geo_point.get("type", "default"), geo_point["lat"], geo_point["lon"],
              geo_point.get("display_name", ""), time.time()))
    conn.commit()
    conn.close()


# --- Routes: Listings cache helpers ---


def load_listings_cache(cache_file: str) -> Optional[dict]:
    """Load listings from SQLite. Returns None if not found.

    cache_file is 'findy_listings.json' or 'spoty_listings.json' (kept for API compat).
    """
    if "findy" in cache_file:
        listings = dashboard_db.get_findy_listings()
        if listings:
            info = dashboard_db.get_findy_cache_info()
            return {"annonces": listings, "count": len(listings),
                    "fetched_at": info.get("cached_at")}
        return None
    elif "spoty" in cache_file:
        listings = dashboard_db.get_spoty_listings()
        if listings:
            info = dashboard_db.get_spoty_cache_info()
            return {"annonces": listings, "count": len(listings),
                    "fetched_at": info.get("cached_at")}
        return None
    return None


# --- Routes: Immobilier (Findy - from local cache) ---


@app.get("/api/immo")
async def api_immo(_=Depends(check_auth)):
    """Return apartment listings from local cache (refreshed by refresh_cache.py).

    Falls back to direct Nextcloud Tables fetch if cache is empty/missing.
    """
    cached = load_listings_cache("findy_listings.json")
    if cached and cached.get("annonces"):
        return {
            "annonces": cached["annonces"],
            "count": cached.get("count", len(cached["annonces"])),
            "cached_at": cached.get("fetched_at"),
            "source": "local_cache",
        }
    # Fallback: direct fetch from Nextcloud
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, fetch_nc_tables, 4)
    return {"annonces": rows, "count": len(rows), "source": "nextcloud_direct"}


# --- Routes: Véhicules (Spoty - from local cache) ---


@app.get("/api/vehicules")
async def api_vehicules(_=Depends(check_auth)):
    """Return vehicle listings from local cache (refreshed by refresh_cache.py).

    Falls back to direct Nextcloud Tables fetch if cache is empty/missing.
    """
    cached = load_listings_cache("spoty_listings.json")
    if cached and cached.get("annonces"):
        return {
            "annonces": cached["annonces"],
            "count": cached.get("count", len(cached["annonces"])),
            "cached_at": cached.get("fetched_at"),
            "source": "local_cache",
        }
    # Fallback: direct fetch from Nextcloud
    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, fetch_nc_tables, 5)
    return {"annonces": rows, "count": len(rows), "source": "nextcloud_direct"}


@app.post("/api/cache/refresh")
async def api_refresh_cache(_=Depends(check_auth)):
    """Manually trigger a cache refresh from Nextcloud Tables."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, _run_cache_refresh)
    return result


def _run_cache_refresh() -> dict:
    """Run the cache refresh script and return status."""
    try:
        proc = subprocess.run(
            ["python3", str(APP_DIR / "refresh_cache.py")],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "HOME": "/root"},
        )
        return {
            "status": "ok" if proc.returncode == 0 else "error",
            "stdout": proc.stdout[-500:] if proc.stdout else "",
            "stderr": proc.stderr[-500:] if proc.stderr else "",
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


# --- Routes: Chat avec Friday ---


@app.post("/api/chat")
async def api_chat(msg: ChatMessage, _=Depends(check_auth)):
    """Send a message to Friday (default Hermes profile) and get a response.

    Uses `hermes chat -q` in non-interactive mode to communicate with the agent.
    """
    if not msg.message.strip():
        raise HTTPException(status_code=400, detail="Le message ne peut pas être vide")

    user_text = msg.message.strip()

    # Save user message to history
    append_chat_message("user", user_text, "text")

    try:
        # Use hermes chat -q for non-interactive mode with the default profile
        # --cli ensures non-interactive output, --no-restore-cwd prevents directory changes
        proc = await asyncio.create_subprocess_exec(
            "/usr/local/lib/hermes-agent/venv/bin/hermes", "chat", "-q", msg.message.strip(), "--cli",
            "--no-restore-cwd", "--max-turns", "5",
            "--profile", "default",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HOME": "/root", "HERMES_NON_INTERACTIVE": "1"},
        )

        # Wait with timeout (90 seconds)
        try:
            stdout_data, stderr_data = await asyncio.wait_for(
                proc.communicate(), timeout=90
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"response": "Désolé, le traitement a pris trop de temps. Friday est peut-être occupée.", "error": True}

        output = stdout_data.decode("utf-8", errors="replace").strip()
        stderr_output = stderr_data.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0 and not output:
            return {"response": f"Erreur: {stderr_output[:200]}", "error": True}

        # Clean up the output - remove non-response lines
        lines = output.split("\n")
        response_lines = []
        skip_patterns = [
            "Initializing", "Query:", "─", "HERMES_CWD", "export -p",
            "Resume this session", "hermes --resume", "Session:", "Duration:",
            "Messages:", "__HERMES"
        ]
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if any(p in line for p in skip_patterns):
                continue
            response_lines.append(line)

        response = "\n".join(response_lines).strip()
        if not response:
            response = "Pas de réponse."

        # Save Friday response to history
        append_chat_message("friday", response, "text")

        return {"response": response, "error": False}

    except Exception as e:
        error_msg = f"Erreur de communication: {str(e)}"
        append_chat_message("friday", error_msg, "text")
        return {"response": error_msg, "error": True}


# --- Chat history helpers ---


class ChatHistoryEntry(BaseModel):
    role: str  # "user" or "friday"
    content: str
    msg_type: str = "text"  # "text" or "audio"


def load_chat_history() -> list[dict]:
    """Load chat history from SQLite, filtered to last 7 days."""
    return dashboard_db.load_chat_history(max_days=CHAT_HISTORY_MAX_DAYS)


def save_chat_history(messages: list[dict]):
    """Save chat history — no-op for SQLite (messages are appended individually)."""
    pass  # SQLite handles persistence via append_chat_message()


def append_chat_message(role: str, content: str, msg_type: str = "text"):
    """Append a single message to chat history (SQLite)."""
    dashboard_db.append_chat_message(role, content, msg_type)


@app.get("/api/chat/history")
async def api_chat_history(_=Depends(check_auth)):
    """Return chat history (last 7 days)."""
    return {"messages": load_chat_history()}


@app.delete("/api/chat/history")
async def api_clear_chat_history(_=Depends(check_auth)):
    """Clear all chat history."""
    dashboard_db.clear_chat_history()
    return {"status": "ok"}


# --- Routes: STT (Speech-to-Text) ---


@app.post("/api/transcribe")
async def api_transcribe(file: UploadFile = File(...), _=Depends(check_auth)):
    """Transcribe audio file using faster-whisper.

    Accepts audio/webm (Chrome default) or audio/ogg.
    Returns transcribed text in French. Logs errors for debugging.
    """
    # Read uploaded audio
    content = await file.read()
    if len(content) < 100:
        return {"text": "", "error": "Audio trop court (vide ou corrompu)"}

    # Determine file extension from content type
    suffix = ".webm"
    ct = file.content_type or ""
    if "ogg" in ct:
        suffix = ".ogg"
    elif "wav" in ct:
        suffix = ".wav"
    elif "mp3" in ct or "mpeg" in ct:
        suffix = ".mp3"

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    print(f"[STT] Transcribing {len(content)} bytes ({ct}) from {file.filename}")
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, info = model.transcribe(tmp_path, language="fr", beam_size=5)
        text = " ".join([segment.text for segment in segments]).strip()

        print(f"[STT] Result: '{text[:100]}' (duration={info.duration:.1f}s)")
        return {"text": text, "language": info.language, "duration": info.duration}
    except Exception as e:
        import traceback
        print(f"[STT] ERROR: {e}")
        traceback.print_exc()
        return {"text": "", "error": str(e)}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# --- Routes: TTS (Text-to-Speech) ---


@app.get("/api/voices")
async def api_voices(_=Depends(check_auth)):
    """Return available TTS voices."""
    config = load_config()
    return {"voices": TTS_VOICES, "current": config.get("tts_voice", TTS_VOICES[0]["id"])}


@app.post("/api/voice")
async def api_set_voice(voice: VoiceConfig, _=Depends(check_auth)):
    """Set the current TTS voice."""
    valid_ids = [v["id"] for v in TTS_VOICES]
    if voice.voice_id not in valid_ids:
        raise HTTPException(status_code=400, detail="Voix invalide")
    config = load_config()
    config["tts_voice"] = voice.voice_id
    save_config(config)
    return {"status": "ok", "voice_id": voice.voice_id}


@app.post("/api/tts")
async def api_tts(msg: ChatMessage, _=Depends(check_auth)):
    """Generate TTS audio from text using edge-tts."""
    if not msg.message.strip():
        raise HTTPException(status_code=400, detail="Le texte ne peut pas être vide")

    config = load_config()
    voice_id = config.get("tts_voice", "fr-FR-DeniseNeural")

    # Generate audio file
    audio_dir = HERMES_HOME / "dashboard-unified" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_file = audio_dir / f"tts_{int(time.time())}_{uuid.uuid4().hex[:6]}.mp3"

    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/local/lib/hermes-agent/venv/bin/edge-tts",
            "--voice", voice_id, "--text", msg.message.strip(),
            "--write-media", str(audio_file),
            "--rate", "+0%",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.wait(), timeout=30)

        if not audio_file.exists():
            raise HTTPException(status_code=500, detail="Erreur génération audio")

        # Read and return audio
        audio_data = audio_file.read_bytes()
        audio_b64 = base64.b64encode(audio_data).decode()

        # Clean up old audio files (keep last 20)
        old_files = sorted(audio_dir.glob("tts_*.mp3"), key=os.path.getmtime)
        for f in old_files[:-20]:
            f.unlink(missing_ok=True)

        return {"audio": audio_b64, "format": "mp3", "voice": voice_id}

    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Timeout génération audio")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/voice-sample/{voice_id}")
async def api_voice_sample(voice_id: str, _=Depends(check_auth)):
    """Generate a short sample audio for a voice."""
    valid_ids = [v["id"] for v in TTS_VOICES]
    if voice_id not in valid_ids:
        raise HTTPException(status_code=400, detail="Voix invalide")

    sample_text = "Bonjour, je suis Friday. C'est un test de ma voix."
    audio_dir = HERMES_HOME / "dashboard-unified" / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_file = audio_dir / f"sample_{voice_id}.mp3"

    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/local/lib/hermes-agent/venv/bin/edge-tts",
            "--voice", voice_id, "--text", sample_text,
            "--write-media", str(audio_file),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.wait(), timeout=30)

        if not audio_file.exists():
            raise HTTPException(status_code=500, detail="Erreur génération sample")

        return FileResponse(str(audio_file), media_type="audio/mpeg")

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Routes: AdventureLog ---


def _extract_place_info_for_adventurelog(prop_id: str, activity_index: int,
                                          activity_name: str) -> dict:
    """Extract a clean place name and address for an AdventureLog location.

    Loads the RAW proposition JSON (before normalization) to access fields
    that normalization discards (e.g., 'position' in programme-based schemas).

    Returns:
        {"name": str, "address": str}
            - name: clean place name (e.g., "Temescal Gateway Park")
            - address: physical address or location query for geocoding

    The activity_index corresponds to the position in the normalized
    'activites' array as displayed in the dashboard. We replay the same
    normalization logic to map the index back to the raw entry.
    """
    raw = _load_proposition_by_id(prop_id, raw=True)
    if not raw:
        return {"name": activity_name, "address": activity_name}

    destination = _get_display_destination(raw)

    # --- Schema: Santa Monica — top-level 'programme' with 'position' ---
    if "programme" in raw and isinstance(raw["programme"], list):
        prog = raw["programme"]
        # Build the same activites list as normalize_proposition does
        normalized_acts = []
        for entry in prog:
            if isinstance(entry, dict):
                act_text = entry.get("activite", entry.get("nom", ""))
                if act_text:
                    heure = entry.get("heure", "")
                    if heure:
                        normalized_acts.append(f"[{heure}] {act_text}")
                    else:
                        normalized_acts.append(act_text)
        # Find the matching entry by index
        if 0 <= activity_index < len(normalized_acts):
            # Find the raw entry at the same position
            raw_idx = 0
            for i, entry in enumerate(prog):
                if isinstance(entry, dict):
                    act_text = entry.get("activite", entry.get("nom", ""))
                    if act_text:
                        if raw_idx == activity_index:
                            position = entry.get("position", "")
                            # Clean the name: strip time prefix, activity prefixes
                            clean_name = _clean_place_name(act_text)
                            address = position or clean_name
                            if not position and destination:
                                address = f"{clean_name}, {destination}"
                            return {"name": clean_name, "address": address}
                        raw_idx += 1

    # --- Schema: Norvège — 'activites' as day objects with nested programme ---
    if "activites" in raw and isinstance(raw["activites"], list):
        activites = raw["activites"]
        has_day_objects = any(
            isinstance(e, dict) and (e.get("programme") or e.get("titre"))
            for e in activites
        )
        if has_day_objects:
            normalized_acts = []
            raw_entries = []  # parallel list of (label, address_candidate)
            for entry in activites:
                if isinstance(entry, dict):
                    titre_jour = entry.get("titre", f"Jour {entry.get('jour', '?')}")
                    resume = entry.get("resume", "")
                    if resume:
                        normalized_acts.append(f"[{titre_jour}] {resume[:150]}")
                        raw_entries.append((resume[:150], ""))
                    prog = entry.get("programme", [])
                    prog_items = []
                    if isinstance(prog, list):
                        prog_items = prog
                    elif isinstance(prog, dict):
                        j = prog.get("journee", [])
                        if isinstance(j, list):
                            prog_items.extend(j)
                        elif isinstance(j, dict):
                            prog_items.append(j)
                        s = prog.get("soiree_nuit", [])
                        if isinstance(s, list):
                            prog_items.extend(s)
                        elif isinstance(s, dict):
                            prog_items.append(s)
                    for item in prog_items:
                        if not isinstance(item, dict):
                            continue
                        act_text = item.get("activite", "")
                        if act_text and act_text.strip():
                            normalized_acts.append(f"[{titre_jour}] {act_text}")
                            deplacement = (item.get("deplacement", "") or
                                           item.get("deplacement_vers_depart", ""))
                            raw_entries.append((act_text, deplacement))
            if 0 <= activity_index < len(normalized_acts):
                act_text, deplacement = raw_entries[activity_index]
                clean_name = _clean_place_name(act_text)
                if deplacement:
                    address = f"{deplacement}, {destination}" if destination else deplacement
                else:
                    address = f"{clean_name}, {destination}" if destination else clean_name
                return {"name": clean_name, "address": address}

    # --- Schema: Londres — 'itineraires' with matin/apres_midi/soir ---
    if "itineraires" in raw and isinstance(raw["itineraires"], list):
        itineraires = raw["itineraires"]
        normalized_acts = []
        raw_entries = []
        for jour in itineraires:
            titre_jour = jour.get("titre", f"Jour {jour.get('jour', '?')}")
            for periode in ("matin", "apres_midi", "soir"):
                bloc = jour.get(periode, {})
                if not isinstance(bloc, dict):
                    continue
                act = bloc.get("activite")
                if act:
                    normalized_acts.append(f"[{titre_jour}] {act}")
                    raw_entries.append((act, ""))
                details = bloc.get("details", [])
                if isinstance(details, list):
                    for d in details:
                        if isinstance(d, str) and len(d) < 200:
                            normalized_acts.append(f"[{titre_jour}] {d}")
                            raw_entries.append((d, ""))
        if 0 <= activity_index < len(normalized_acts):
            act_text, _ = raw_entries[activity_index]
            clean_name = _clean_place_name(act_text)
            address = f"{clean_name}, {destination}" if destination else clean_name
            return {"name": clean_name, "address": address}

    # --- Flat schema (Azores, Porto, Bretagne) — activites as strings ---
    activites = raw.get("activites", [])
    if isinstance(activites, list) and 0 <= activity_index < len(activites):
        act = activites[activity_index]
        if isinstance(act, str):
            clean_name = _clean_place_name(act)
            address = f"{clean_name}, {destination}" if destination else clean_name
            return {"name": clean_name, "address": address}

    # Fallback: use the activity name as-is
    clean_name = _clean_place_name(activity_name)
    address = f"{clean_name}, {destination}" if destination else clean_name
    return {"name": clean_name, "address": address}


def _clean_place_name(text: str) -> str:
    """Extract a clean place name from an activity description.

    Strips time prefixes, activity type prefixes, parenthetical content,
    and compound descriptions to get just the place name.

    Strategy:
    1. Remove leading [time]/[Jour] prefixes and time stamps
    2. Remove parenthetical content (before dash splitting so word counts are clean)
    3. Split on em-dash/pipe separators:
       - If BEFORE the dash looks like an activity prefix -> take AFTER
       - If AFTER the dash looks like a description -> take BEFORE
    4. Remove trailing compound descriptions after "+"
    5. Remove leading French activity verbs (Marche, Visite, etc.)
    6. If the result still starts with a French preposition (sur, a, au...),
       try to extract the place name after it

    Examples:
        "Randonnee - Temescal Gateway Park (boucle)" -> "Temescal Gateway Park"
        "[08:00] Marche matinale sur Santa Monica Beach" -> "Santa Monica Beach"
        "Petit-dejeuner - The Misfit Restaurant + Bar" -> "The Misfit Restaurant"
        "Visite des caves de Porto (degustation)" -> "Caves de Porto"
        "Venice Canals - balade pittoresque" -> "Venice Canals"
        "Getty Villa - musee d'antiquites" -> "Getty Villa"
        "GR34 sentier des douaniers - Pointe de Pen-Hir" -> "Pointe de Pen-Hir"
    """
    import re
    text = text.strip()

    # Step 1: Remove leading [time] or [Jour X] prefix
    text = re.sub(r'^\[[^\]]+\]\s*', '', text)

    # Remove leading time like "08:00" or "08:00 -"
    text = re.sub(r'^\d{1,2}:\d{2}\s*(?:-\s*)?', '', text)

    # Step 2: Remove parenthetical content EARLY (before dash splitting
    # so word counts are not inflated by parenthetical descriptions)
    text = re.sub(r'\([^)]*\)', '', text)

    # Step 3: Split on em-dash, en-dash, or pipe
    parts = re.split(r'\s+[\u2014\u2013|]\s+', text, maxsplit=1)
    if len(parts) == 2:
        left, right = parts[0].strip(), parts[1].strip()

        # Known activity-type prefixes (what comes BEFORE the dash)
        activity_prefixes = [
            'Randonn\u00e9e', 'Rando', 'Marche', 'Visite', 'Observation',
            'Baignade', 'Promenade', 'Tour', 'D\u00e9couverte', 'Balade',
            'Arriv\u00e9e', 'D\u00e9part', 'Retour', 'Installation', 'Location',
            'Trajet', 'D\u00eener', 'D\u00e9jeuner', 'Petit-d\u00e9jeuner',
            'Soir\u00e9e', 'Excursion', 'Fin de journ\u00e9e', 'Bain', 'Bains',
        ]
        left_lower = left.lower()
        left_starts_with_activity = any(
            left_lower.startswith(p.lower()) for p in activity_prefixes
        )

        # Check if right side looks like a description (lowercase, starts with
        # common French description words)
        desc_starts = [
            'balade', 'mus\u00e9e', 'musee', 'visite', 'randonn\u00e9e', 'randonnee',
            'marche', 'd\u00e9couverte', 'decouverte', 'excursion',
            'promenade', 'observation', 'sentier', 'boucle',
            'crat\u00e8re', 'cratere', 'crater', 'panorama', 'vue',
            'd\u00e9tente', 'detente', 'pause',
            'installation', 'trajet', 'retour', 'arriv\u00e9e', 'arrivee',
            'd\u00e9part', 'depart',
            'soir\u00e9e', 'soiree', 'd\u00eener', 'diner', 'd\u00e9jeuner', 'dejeuner',
            'petit-d\u00e9jeuner', 'petit-dejeuner',
            'programme', 'itin\u00e9raire', 'itineraire',
            '\u00e9tape', 'etape', 'r\u00e9sum\u00e9', 'resume',
            'pittoresque', 'panoramique', 'culturelle',
        ]
        right_lower = right.lower()
        right_is_description = right_lower.startswith(tuple(desc_starts))

        if left_starts_with_activity and not right_is_description:
            # "Randonnee - Temescal Gateway Park" -> "Temescal Gateway Park"
            text = right
        elif right_is_description and not left_starts_with_activity:
            # "Venice Canals - balade pittoresque" -> "Venice Canals"
            text = left
        elif left_starts_with_activity and right_is_description:
            # Left has activity prefix, right is a description.
            # Clean left (strip activity prefix) and if a place name remains, use it.
            # e.g., "Retour vers Santa Monica - detente & pause" -> "Santa Monica"
            left_cleaned = left
            for p in activity_prefixes:
                if left_cleaned.lower().startswith(p.lower()):
                    left_cleaned = left_cleaned[len(p):]
                    break
            left_cleaned = left_cleaned.strip()
            # Try to extract place name from cleaned left (e.g., "vers Santa Monica" -> "Santa Monica")
            import re as _re
            if left_cleaned:
                prep_match = _re.search(
                    r'\b(?:vers|sur|\u00e0|au|de la|du|des|de|dans le|dans la)\s+(.+)',
                    left_cleaned
                )
                if prep_match:
                    extracted_left = prep_match.group(1).strip()
                    stop_words = (' et ', ', ', 'puis', 'avec', 'pour', 'vers')
                    if len(extracted_left) < 80 and not any(w in extracted_left.lower() for w in stop_words):
                        left_cleaned = extracted_left
            # Only use left_cleaned if it looks like a place name (not a preposition fragment)
            # Reject if it starts with a preposition or is too short (< 3 chars)
            preposition_starts = ('de ', 'du ', 'des ', 'vers ', 'sur ', 'au ', 'la ', 'le ', 'à ')
            looks_like_place = (
                left_cleaned and
                len(left_cleaned) >= 3 and
                not any(left_cleaned.lower().startswith(p) for p in preposition_starts)
            )
            if looks_like_place:
                text = left_cleaned
            else:
                text = right
        else:
            # Ambiguous: prefer the side that looks more like a place name.
            left_words = len(left.split())
            right_words = len(right.split())
            if right_words > 0 and right_words <= 4 and left_words > 2:
                # Right is short enough to be a place name
                text = right
            elif left_words > 0 and left_words <= 4 and not left_lower.startswith(
                tuple(p.lower() for p in activity_prefixes)
            ):
                # Left is short and not an activity prefix -> it is the place
                text = left
            else:
                text = right  # default: take after the dash

    # Step 4: Split on "+" or "&" to take just the first place if compound
    text = re.split(r'\s*(?:\+|&)\s*', text)[0]

    # Step 5: Remove leading French activity prefixes (longest first)
    prefixes = [
        'Randonn\u00e9e ', 'Rando ', 'Marche matinale ', 'Marche ',
        'Visite du ', 'Visite de la ', 'Visite des ', 'Visite de ',
        'Visite ',
        'Observation des ', 'Observation du ', 'Observation ',
        'Baignade ', 'Promenade ', 'Tour de ', 'Tour ',
        'D\u00e9couverte de ', 'D\u00e9couverte du ', 'D\u00e9couverte ',
        'Balade ', 'Arriv\u00e9e ', 'D\u00e9part ', 'Retour ',
        'Installation ', 'Location de v\u00e9lo ', 'Location ',
        'Trajet ', 'D\u00eener ', 'D\u00e9jeuner ',
        'Petit-d\u00e9jeuner ', 'Soir\u00e9e ', 'Excursion ',
        'Fin de journ\u00e9e ', 'Bain ', 'Bains ',
        'Arr\u00eat \u00e0 ', 'Arr\u00eat ',
    ]
    for p in prefixes:
        if text.startswith(p):
            text = text[len(p):]
            break

    # Step 6: If the remaining text still looks like a sentence (starts with
    # a French preposition), try to extract the place name after it.
    # Only do this if the text is long enough to be a sentence (> 15 chars)
    # AND the text before the preposition looks like a description word
    # (not a place name like "Pointe de Pen-Hir").
    if len(text) > 15:
        place_match = re.search(
            r'\b(?:sur|\u00e0|au|de la|du|des|de|dans le|dans la|vers)\s+(.+)',
            text
        )
        if place_match:
            # Check the text BEFORE the preposition: if it is a known activity
            # verb or description word, extraction is safe.
            before_prep = text[:place_match.start()].strip().lower()
            # Activity/description words that indicate the text before the
            # preposition is a description, not part of a place name
            activity_desc_words = (
                'matinale', 'matin', 'thermale', 'thermaux',
                'nocturne', 'observation',
            )
            should_extract = (
                not before_prep or
                any(v in before_prep for v in activity_desc_words)
            )
            extracted = place_match.group(1).strip()
            stop_words = (' et ', ', ', 'puis', 'avec', 'pour', 'vers')
            if (should_extract and
                len(extracted) < 80 and
                not any(w in extracted.lower() for w in stop_words)):
                text = extracted

    # Clean whitespace
    text = ' '.join(text.split())

    # Capitalize first letter if the result is all lowercase
    if text and text[0].islower():
        text = text[0].upper() + text[1:]

    return text.strip()



def get_adventurelog_api_key() -> Optional[str]:
    """Load AdventureLog API key from .env file."""
    env_file = HERMES_HOME / ".env"
    if not env_file.exists():
        return None
    try:
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("ADVENTURELOG_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return None


@app.post("/api/adventurelog/location")
async def api_add_adventurelog_location(loc: AdventureLogLocation, _=Depends(check_auth)):
    """Create a location in AdventureLog via its REST API (legacy endpoint).

    Kept for backward compatibility. Prefer /api/adventurelog/add which
    handles place name extraction and geocoding automatically.
    """
    api_key = get_adventurelog_api_key()
    if not api_key:
        raise HTTPException(status_code=500, detail="Clé API AdventureLog introuvable dans .env")

    api_url = "https://adventurelog.wired-lan.fr/api/locations"
    body_dict = {
        "name": loc.name,
        "location": loc.location,
        "is_public": loc.is_public,
    }
    # Include lat/long if provided
    if loc.latitude is not None:
        body_dict["latitude"] = loc.latitude
    if loc.longitude is not None:
        body_dict["longitude"] = loc.longitude
    body = json.dumps(body_dict).encode("utf-8")

    req = urllib.request.Request(api_url, data=body, headers={
        "Authorization": f"Api-Key {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            response_data = json.loads(resp.read().decode("utf-8"))
            return {"status": "ok", "location": response_data}
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise HTTPException(
            status_code=e.code,
            detail=f"AdventureLog API error {e.code}: {error_body}",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur AdventureLog: {str(e)}")


@app.post("/api/adventurelog/add")
async def api_add_adventurelog_from_proposition(req: AdventureLogAddRequest, _=Depends(check_auth)):
    """Add a proposition activity as a location in AdventureLog.

    Full pipeline:
    1. Load the RAW proposition JSON to extract the proper place name and address
    2. Geocode the address via Nominatim (cached, rate-limited)
    3. POST to AdventureLog with name, location, latitude, longitude

    The geocoding step is async (runs in a thread pool) so it does not block
    the event loop. Nominatim results are cached on disk to avoid repeated calls.

    Returns the created location data from AdventureLog, including the
    extracted place name and geocoded coordinates.
    """
    api_key = get_adventurelog_api_key()
    if not api_key:
        raise HTTPException(status_code=500, detail="Clé API AdventureLog introuvable dans .env")

    # Step 1: Extract place name and address from the raw proposition
    place_info = _extract_place_info_for_adventurelog(
        req.prop_id, req.activity_index, req.activity_name
    )
    place_name = place_info["name"]
    address = place_info["address"]

    # Step 2: Geocode the address via Nominatim (async, cached)
    # Run in thread pool since geocode_with_fallbacks does blocking I/O
    loop = asyncio.get_event_loop()
    geo_result = await loop.run_in_executor(
        None, geocode_with_fallbacks, address
    )

    latitude = None
    longitude = None
    if geo_result:
        # Truncate to 6 decimal places to satisfy AdventureLog's
        # validation: latitude max 6 decimals, longitude max 9 total digits.
        lat_val = geo_result.get("lat", "")
        lon_val = geo_result.get("lon", "")
        if isinstance(lat_val, (float, int)):
            latitude = f"{lat_val:.6f}"
        elif lat_val:
            latitude = str(lat_val)
        if isinstance(lon_val, (float, int)):
            longitude = f"{lon_val:.6f}"
        elif lon_val:
            longitude = str(lon_val)
        # Use the display_name from Nominatim as a better address if available
        display = geo_result.get("display_name", "")
        if display:
            address = display

    # Step 3: POST to AdventureLog
    api_url = "https://adventurelog.wired-lan.fr/api/locations"
    body_dict = {
        "name": place_name,
        "location": address,
        "is_public": False,
    }
    if latitude is not None:
        body_dict["latitude"] = latitude
    if longitude is not None:
        body_dict["longitude"] = longitude
    body = json.dumps(body_dict).encode("utf-8")

    req_to_al = urllib.request.Request(api_url, data=body, headers={
        "Authorization": f"Api-Key {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }, method="POST")

    try:
        with urllib.request.urlopen(req_to_al, timeout=15) as resp:
            response_data = json.loads(resp.read().decode("utf-8"))
            # Store the geocoded location in SQLite for future reference
            al_uuid = None
            if isinstance(response_data, dict):
                al_uuid = response_data.get("id") or response_data.get("uuid")
            lat_float = float(latitude) if latitude else None
            lon_float = float(longitude) if longitude else None
            dashboard_db.add_adventurelog_location(
                proposition_id=req.prop_id,
                programme_item_id=None,
                adventurelog_uuid=str(al_uuid) if al_uuid else "",
                name=place_name,
                lat=lat_float,
                lon=lon_float,
            )
            return {
                "status": "ok",
                "location": response_data,
                "place_name": place_name,
                "address": address,
                "geocoded": latitude is not None,
            }
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise HTTPException(
            status_code=e.code,
            detail=f"AdventureLog API error {e.code}: {error_body}",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur AdventureLog: {str(e)}")



@app.get("/api/adventurelog/locations")
async def api_list_adventurelog_locations(_=Depends(check_auth)):
    """List existing AdventureLog locations to check for duplicates.

    Returns a list of location names so the frontend can mark already-added ones.
    """
    api_key = get_adventurelog_api_key()
    if not api_key:
        raise HTTPException(status_code=500, detail="Clé API AdventureLog introuvable dans .env")

    api_url = "https://adventurelog.wired-lan.fr/api/locations"
    req = urllib.request.Request(api_url, headers={
        "Authorization": f"Api-Key {api_key}",
        "Accept": "application/json",
    })

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            # AdventureLog returns paginated results
            results = data if isinstance(data, list) else data.get("results", [])
            names = {item.get("name", "") for item in results if isinstance(item, dict)}
            return {"names": list(names)}
    except Exception as e:
        return {"names": [], "error": str(e)}


# --- Routes: Config ---


@app.get("/api/config")
async def api_get_config(_=Depends(check_auth)):
    """Return current dashboard config."""
    config = load_config()
    return {**config, "voices": TTS_VOICES}


# --- Static files ---

app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("DASHBOARD_HOST", "10.245.243.1")
    port = int(os.environ.get("DASHBOARD_PORT", "9120"))

    # --- HTTPS with self-signed certificate ---
    # Needed for browser permissions (getUserMedia / microphone) which require
    # a secure context (HTTPS or localhost). The cert is self-signed, so the
    # browser will show a security warning — accept the risk to proceed.
    SSL_DIR = HERMES_HOME / "dashboard-unified" / "ssl"
    cert_path = SSL_DIR / "cert.pem"
    key_path = SSL_DIR / "key.pem"

    if cert_path.exists() and key_path.exists():
        uvicorn.run(app, host=host, port=port,
                    ssl_certfile=str(cert_path), ssl_keyfile=str(key_path))
    else:
        print(f"[WARN] SSL certs not found in {SSL_DIR}, falling back to HTTP")
        uvicorn.run(app, host=host, port=port)
