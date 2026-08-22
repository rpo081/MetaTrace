#!/usr/bin/env bash
# docker-build-push.sh — Multi-arch build & push to GHCR (linux/amd64 + linux/arm64)
# Usage: ./docker-build-push.sh [tag] [username]
#        Ohne Args: Tag = git tag bzw. Short-SHA, Username = gh CLI Login

set -euo pipefail

# Immer vom Repo-Root aus bauen (Build-Kontext = Dockerfile-Lage),
# egal von wo das Skript aufgerufen wird.
cd "$(dirname "$0")"

VERSION="${1:-}"
USERNAME="${2:-}"

# Username aus gh CLI falls nicht übergeben
if [[ -z "$USERNAME" ]]; then
  USERNAME=$(gh api user --jq .login 2>/dev/null) || {
    echo "❌ Username nicht gefunden. Als 2. Argument übergeben oder: gh auth login"
    exit 1
  }
fi

# Tag aus git falls nicht übergeben
if [[ -z "$VERSION" ]]; then
  VERSION=$(git describe --tags --exact-match 2>/dev/null || git rev-parse --short HEAD)
fi

IMAGE="ghcr.io/${USERNAME}/metatrace"
TAG_VERSION="${IMAGE}:${VERSION}"
TAG_LATEST="${IMAGE}:latest"

echo "📦 Build & Push: ${TAG_VERSION} + ${TAG_LATEST}"
echo "   Platforms: linux/amd64,linux/arm64"
echo ""

# Buildx Builder sicherstellen
BUILDER="metatrace-multiarch"
if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
  echo "🔧 Erstelle buildx builder: $BUILDER"
  docker buildx create --name "$BUILDER" --driver docker-container --use
else
  docker buildx use "$BUILDER"
fi

# Build & Push
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t "$TAG_VERSION" \
  -t "$TAG_LATEST" \
  --push \
  .

echo ""
echo "✅ Fertig!"
echo "   Version: $TAG_VERSION"
echo "   Latest:  $TAG_LATEST"
echo ""
echo "🔍 Prüfen: https://github.com/${USERNAME}?tab=packages"
