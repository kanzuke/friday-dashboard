#!/bin/bash
# git_push.sh — Auto-commit + push pour le dashboard
#
# Usage: ./git_push.sh [message optionnel]
#
# Run this script after making changes to the dashboard to commit and push to GitHub.
# If no message is provided, a timestamp-based message is generated automatically
# (e.g., 'Auto-commit: 2025-01-15 14:30:00').
# If a message is provided, it is appended to the timestamp:
#   ./git_push.sh "fix: correction du bug carte"
#   → 'Auto-commit: 2025-01-15 14:30:00 — fix: correction du bug carte'
#
# Le script :
#   1. Ajoute tous les fichiers (respectant .gitignore)
#   2. Génère un message de commit avec timestamp
#   3. Commit
#   4. Pousse sur origin/main

set -euo pipefail

# Détecter le répertoire du script (ne pas hardcoder /opt/hermes-dashboard)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE="origin"
BRANCH="main"

cd "$SCRIPT_DIR"

# Générer le timestamp
TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"

# Construire le message de commit
if [ -n "${1:-}" ]; then
    COMMIT_MSG="Auto-commit: ${TIMESTAMP} — ${1}"
else
    COMMIT_MSG="Auto-commit: ${TIMESTAMP}"
fi

echo "=== git add -A ==="
git add -A

# Vérifier qu'il y a des changements à committer
if git diff --cached --quiet; then
    echo "Rien à committer — le répertoire de travail est propre."
    exit 0
fi

echo "=== git commit ==="
git commit -m "$COMMIT_MSG"

echo "=== git push $REMOTE $BRANCH ==="
git push "$REMOTE" "$BRANCH"

echo "=== Done: pushed to $REMOTE/$BRANCH ==="
echo "    Commit: $COMMIT_MSG"
