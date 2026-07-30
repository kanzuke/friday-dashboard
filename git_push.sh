#!/bin/bash
# git_push.sh — Auto-commit + push pour le dashboard
# Usage: ./git_push.sh "Description du changement"
#
# Ce script est conçu pour être exécuté après chaque modification
# du code du dashboard. Il:
#   1. Ajoute tous les fichiers (respectant .gitignore)
#   2. Commit avec le message fourni
#   3. Pousse sur origin/main

set -euo pipefail

REPO_DIR="/opt/hermes-dashboard"
REMOTE="origin"
BRANCH="main"

cd "$REPO_DIR"

if [ -z "${1:-}" ]; then
    echo "Usage: $0 \"Description du changement\""
    exit 1
fi

MESSAGE="$1"

echo "=== git add -A ==="
git add -A

echo "=== git commit ==="
git commit -m "$MESSAGE"

echo "=== git push $REMOTE $BRANCH ==="
git push "$REMOTE" "$BRANCH"

echo "=== Done: pushed to $REMOTE/$BRANCH ==="