#!/usr/bin/env bash
# docker-build-push.sh — Multi-arch CPU build & push to GHCR
# Platforms: linux/amd64 (Linux x64, macOS Intel) + linux/arm64 (macOS Apple Silicon)
#
# Usage:
#   ./docker-build-push.sh                     # git tag/SHA + gh CLI username
#   ./docker-build-push.sh v1.0.0              # explicit tag, gh CLI username
#   ./docker-build-push.sh v1.0.0 rpo081       # explicit tag + username
#   ./docker-build-push.sh "" rpo081           # git tag/SHA, explicit username
#
# Requires: docker buildx, gh CLI (for auto username detection)

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

echo "📦 CPU Build & Push: ${TAG_VERSION} + ${TAG_LATEST}"
echo "   Platforms: linux/amd64 (x64/macOS Intel), linux/arm64 (macOS Apple Silicon)"
echo ""

# Buildx Builder sicherstellen
BUILDER="metatrace-multiarch"
if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
  echo "🔧 Erstelle buildx builder: $BUILDER"
  docker buildx create --name "$BUILDER" --driver docker-container --use
else
  docker buildx use "$BUILDER"
fi

# Build & Push (CPU-only: explicit torch CPU index URL)
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu \
  --label "org.opencontainers.image.version=${VERSION}" \
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
