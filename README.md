# Friday Dashboard

Dashboard web unifié pour la flotte d'agents Hermes de Mathieu. Regroupe tous les outils en une seule interface accessible via ZeroTier.

## Fonctionnalités

### Home
- Statut système en temps réel (uptime, services actifs)
- Liste des agents Hermes (Friday, Findy, Spoty, Dev, Travely) avec statut gateway
- Compteurs de tâches kanban par agent

### Voyages (Travely)
- Propositions de voyage en cartes
- Images Wikimedia Commons + liens Wikipedia pour chaque activité
- Boutons Valider / Réorienter / Refuser
- Système de commentaires
- Cartes interactives (Leaflet) pour visualiser les propositions

### Immobilier (Findy)
- Tableau des annonces depuis Nextcloud Tables (ID 4)
- Colonnes: titre, prix, surface, €/m², statut, lien

### Véhicules (Spoty)
- Tableau des annonces depuis Nextcloud Tables (ID 5)
- Colonnes: titre, prix, année, kilométrage, carburant, portes, localisation, lien

### Chat Friday
- Interface de chat texte avec Friday (profil default)
- Input vocal: bouton micro → Whisper (local) → texte
- Output vocal: TTS edge-tts (bouton haut-parleur)
- 4 voix françaises disponibles

## Stack technique

- **Backend**: Python FastAPI + uvicorn
- **Frontend**: HTML/CSS/JS + Tailwind CSS (SPA)
- **Cartes**: Leaflet.js + OpenStreetMap
- **TTS**: edge-tts (Microsoft Edge TTS, gratuit)
- **STT**: faster-whisper (local, CPU)
- **Chat**: Hermes CLI (`hermes chat -q`)
- **Données**: JSON + Nextcloud Tables (OCS API)
- **Auth**: Basic Auth

## Installation

### Prérequis

- Python 3.11+
- Hermes Agent installé (`/usr/local/lib/hermes-agent/venv/`)
- ZeroTier configuré (réseau `cf719fd5405d8a17`)
- edge-tts, faster-whisper installés dans le venv Hermes

### Déploiement

```bash
# Cloner le dépôt
git clone https://github.com/kanzuke/friday-dashboard.git /opt/hermes-dashboard

# Installer le service systemd
cp /opt/hermes-dashboard/systemd/friday-dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable friday-dashboard
systemctl start friday-dashboard
```

### Configuration

Le service lit sa configuration depuis les variables d'environnement systemd:

| Variable | Description | Défaut |
|----------|-------------|--------|
| `DASHBOARD_HOST` | Adresse d'écoute | `10.245.243.1` (ZeroTier) |
| `DASHBOARD_PORT` | Port d'écoute | `9120` |
| `DASHBOARD_USER` | Utilisateur Basic Auth | `mathieu` |
| `DASHBOARD_PASS` | Mot de passe Basic Auth | (à définir) |

## Accès

- **URL**: `http://10.245.243.1:9120` (via ZeroTier)
- **Auth**: Basic Auth (mathieu / mot de passe)
- **Réseau**: ZeroTier network `cf719fd5405d8a17`

## Structure du dépôt

```
friday-dashboard/
├── app.py                  # Backend FastAPI (2121 lignes)
├── geocode_helpers.py      # Utilitaires géocoding (OSM/Nominatim)
├── refresh_cache.py        # Script de refresh du cache propositions
├── static/
│   ├── index.html          # Frontend SPA (1672 lignes)
│   └── map.js              # Logique cartes Leaflet
├── systemd/
│   └── friday-dashboard.service  # Unit systemd
├── requirements.txt        # Dépendances Python
├── .gitignore
└── README.md
```

## API Endpoints

| Méthode | Route | Description |
|---------|-------|-------------|
| GET | `/api/status` | Statut système (services, agents, cron, kanban) |
| GET | `/api/propositions` | Propositions de voyage |
| POST | `/api/comments` | Ajouter un commentaire |
| DELETE | `/api/comments/{id}` | Supprimer un commentaire |
| POST | `/api/actions` | Valider/réorienter/refuser une proposition |
| DELETE | `/api/actions/{id}` | Réinitialiser une action |
| POST | `/api/enrich` | Enrichir une activité (Wikimedia) |
| GET | `/api/proposition/{prop_id}/map` | Carte interactive d'une proposition |
| GET | `/api/immo` | Annonces immobilières (NC Tables 4) |
| GET | `/api/vehicules` | Annonces véhicules (NC Tables 5) |
| POST | `/api/chat` | Chat avec Friday |
| POST | `/api/transcribe` | Speech-to-text (Whisper) |
| POST | `/api/tts` | Text-to-speech (edge-tts) |
| GET | `/api/voices` | Liste des voix TTS |
| POST | `/api/voice` | Changer la voix TTS |
| GET | `/api/voice-sample/{id}` | Sample audio d'une voix |
| GET | `/api/config` | Config du dashboard |

## Workflow de développement

Après chaque modification du code, exécuter:

```bash
cd /opt/hermes-dashboard
./git_push.sh "Description du changement"
```

Ce script effectue automatiquement:
1. `git add -A`
2. `git commit -m "<message>"`
3. `git push origin main`

## Licence

Projet privé — Mathieu (kanzuke)