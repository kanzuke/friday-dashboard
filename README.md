# Friday Dashboard

Dashboard web unifié pour la flotte d'agents Hermes de Mathieu.
Regroupe tous les outils en une seule interface accessible via ZeroTier.

## Description

Le Friday Dashboard est l'interface centrale qui unifie la supervision et
l'interaction avec la flotte d'agents Hermes (Friday, Findy, Spoty, Dev,
Travely). Il expose en une seule page web :

- **Home** — statut système en temps réel (uptime, services actifs), liste des
  agents avec statut gateway, compteurs de tâches kanban.
- **Voyages (Travely)** — propositions de voyage en cartes, images Wikimedia
  Commons + liens Wikipedia, boutons Valider / Réorienter / Refuser, système
  de commentaires, cartes interactives (Leaflet).
- **Immobilier (Findy)** — tableau des annonces depuis Nextcloud Tables (ID 4).
- **Véhicules (Spoty)** — tableau des annonces depuis Nextcloud Tables (ID 5).
- **Chat Friday** — interface de chat texte + vocal avec Friday.
  STT via faster-whisper (local), TTS via edge-tts.
- **Paramètres** — sélection de voix TTS, configuration du dashboard.

## Stack technique

| Composant | Technologie |
|-----------|-------------|
| Backend | Python 3.11+ / FastAPI / uvicorn |
| Frontend | HTML/CSS/JS SPA + Tailwind CSS |
| Cartes | Leaflet.js + OpenStreetMap |
| TTS | edge-tts (Microsoft Edge TTS, gratuit) |
| STT | faster-whisper (local, CPU) |
| Chat | Hermes CLI (`hermes chat -q`) |
| Données | JSON + Nextcloud Tables (OCS API) |
| Authentification | HTTP Basic Auth |
| SSL | Certificat auto-signé (HTTPS pour `getUserMedia`) |
| Déploiement | systemd (le projet n'est pas dockerisé) |

> **Note sur Docker** : le projet n'utilise pas Docker actuellement.
> Il s'exécute nativement via un service systemd sur le VPS. Un conteneur
> Docker pourrait être ajouté si besoin, mais le déploiement actuel repose
> sur le venv Hermes.

## Installation

### Prérequis

- Python 3.11+
- [Hermes Agent](https://hermes-agent.nousresearch.com) installé
  (`/usr/local/lib/hermes-agent/venv/`)
- ZeroTier configuré (réseau `cf719fd5405d8a17`)
- `edge-tts` et `faster-whisper` installés dans le venv Hermes

### Développement local

```bash
# 1. Cloner le dépôt
git clone https://github.com/kanzuke/friday-dashboard.git
cd friday-dashboard

# 2. Créer un environnement virtuel
python3 -m venv .venv
source .venv/bin/activate

# 3. Installer les dépendances
pip install -r requirements.txt

# 4. Configurer les variables d'environnement (voir section Configuration)

# 5. Lancer le dashboard
python app.py
```

Le serveur démarre sur `https://10.245.243.1:9120` (si les certificats SSL
sont présents) ou `http://10.245.243.1:9120` (fallback HTTP).

Pour un développement sans ZeroTier, on peut forcer l'écoute locale :

```bash
DASHBOARD_HOST=127.0.0.1 DASHBOARD_PORT=9120 python app.py
```

### Déploiement en production (systemd)

```bash
# Cloner vers /opt/hermes-dashboard
git clone https://github.com/kanzuke/friday-dashboard.git /opt/hermes-dashboard

# Installer le service systemd
cp /opt/hermes-dashboard/systemd/friday-dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable friday-dashboard
systemctl start friday-dashboard
```

## Configuration

### Variables d'environnement

Le service lit sa configuration depuis les variables d'environnement
(définies dans le fichier systemd ou exportées avant `python app.py`) :

| Variable | Description | Défaut |
|----------|-------------|--------|
| `DASHBOARD_HOST` | Adresse d'écoute | `10.245.243.1` (ZeroTier) |
| `DASHBOARD_PORT` | Port d'écoute | `9120` |
| `DASHBOARD_USER` | Utilisateur Basic Auth | `mathieu` |
| `DASHBOARD_PASS` | Mot de passe Basic Auth | `toto` |
| `HERMES_HOME` | Racine Hermes (`~/.hermes`) | `/root/.hermes` |

### Fichier `.env`

Le dashboard lit les credentials tiers depuis `~/.hermes/.env` (le fichier
`.env` à la racine de Hermes, pas celui du dépôt). Clés attendues :

```bash
# Nextcloud Tables (récupération des annonces Findy + Spoty)
NEXTCLOUD_URL=https://cloud.wired-lan.fr
NEXTCLOUD_USER=<utilisateur>
NEXTCLOUD_APP_PASSWORD=<app password>

# AdventureLog (création de locations depuis les propositions Travely)
ADVENTURELOG_API_KEY=<api key>
```

> Le fichier `.env` est ignoré par `.gitignore` et ne doit jamais être committé.

### Credentials GitHub pour le push

Le script `git_push.sh` effectue `git push origin main`. Il nécessite :

1. **Token GitHub** (Personal Access Token avec scope `repo`) configuré :
   ```bash
   git remote set-url origin https://<token>@github.com/kanzuke/friday-dashboard.git
   ```
   Ou via credential helper :
   ```bash
   git config --global credential.helper store
   # puis un premier push interactif pour sauvegarder le token

   ```

2. **Ou clé SSH** :
   ```bash
   git remote set-url origin git@github.com:kanzuke/friday-dashboard.git
   ```
   La clé SSH publique doit être ajoutée dans Settings → SSH keys sur GitHub.

## Accès

### Réseau ZeroTier

Le dashboard est accessible uniquement depuis le réseau ZeroTier privé :

- **Network ID** : `cf719fd5405d8a17`
- **IP du dashboard** : `10.245.243.1`
- **Port** : `9120`
- **URL** : `https://10.245.243.1:9120`
- **Auth** : Basic Auth (`mathieu` / mot de passe)

Pour accéder au dashboard, un appareil doit rejoindre le réseau ZeroTier
et être autorisé par l'administrateur.

### HTTPS (certificat auto-signé)

Le dashboard utilise HTTPS avec un certificat auto-signé, nécessaire pour
que le navigateur autorise l'accès au microphone (`getUserMedia`) utilisé
par le chat vocal.

**Générer les certificats** :

```bash
SSL_DIR=~/.hermes/dashboard-unified/ssl
mkdir -p "$SSL_DIR"
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$SSL_DIR/key.pem" \
  -cert "$SSL_DIR/cert.pem" \
  -days 365 \
  -subj "/CN=10.245.243.1"
```

Si les certificats sont absents, le serveur démarre en HTTP (fallback) mais
le chat vocal ne fonctionnera pas (les navigateurs bloquent `getUserMedia`
sur HTTP hors localhost).

Le navigateur affichera un avertissement de sécurité — accepter le risque
pour continuer.

## Structure du dépôt

```
friday-dashboard/
├── app.py                  # Backend FastAPI (2121 lignes)
├── geocode_helpers.py      # Utilitaires géocoding (OSM/Nominatim)
├── refresh_cache.py        # Refresh du cache propositions (NC Tables)
├── git_push.sh             # Auto-commit + push vers origin/main
├── static/
│   ├── index.html          # Frontend SPA
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
| POST | `/api/adventurelog/location` | Créer une location AdventureLog |

## Workflow de développement

Après chaque modification du code, exécuter `./git_push.sh` depuis la racine
du dépôt. Le script génère automatiquement un message de commit horodaté,
commit et pousse sur `origin/main` :

```bash
./git_push.sh
# → 'Auto-commit: 2025-01-15 14:30:00'

# Optionnel : ajouter une description au message
./git_push.sh "fix: correction du bug carte"
# → 'Auto-commit: 2025-01-15 14:30:00 — fix: correction du bug carte'
```

Ce script effectue automatiquement :
1. `git add -A` (respecte `.gitignore`)
2. Génère un message avec timestamp (`Auto-commit: YYYY-MM-DD HH:MM:SS`)
3. `git commit -m "<message>"`
4. `git push origin main`

> **Note :** les credentials GitHub (token PAT avec scope `repo` ou clé SSH)
> doivent être configurés au préalable (voir section
> « Credentials GitHub pour le push » ci-dessus).

## Licence

Projet privé — Mathieu (kanzuke)