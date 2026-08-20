#!/usr/bin/env bash
#
# Build the .mcpb bundle for one-click install in Claude Desktop.
#
#   ./scripts/build-mcpb.sh
#
# Output: dist/whatsapp-mcp-macos-<version>.mcpb
#
# Files are copied into a clean staging directory by explicit allowlist rather
# than packed from the repo root. This repo holds live WhatsApp session keys
# (bridge/auth_info/) and registry tokens; the bundle is a public release asset,
# so nothing gets in unless it is named here.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

MCPB="npx --yes @anthropic-ai/mcpb@2.1.2"

version_of() {
    # Reads the first `version = "x.y.z"` from a pyproject/manifest.
    sed -n 's/^[[:space:]]*"\{0,1\}version"\{0,1\}[[:space:]]*[:=][[:space:]]*"\([^"]*\)".*/\1/p' "$1" | head -1
}

PY_VERSION="$(version_of "$REPO/pyproject.toml")"
MF_VERSION="$(version_of "$REPO/manifest.json")"

if [[ "$PY_VERSION" != "$MF_VERSION" ]]; then
    echo "error: version mismatch — pyproject.toml is $PY_VERSION, manifest.json is $MF_VERSION" >&2
    exit 1
fi

echo "Building whatsapp-mcp-macos $PY_VERSION"

# ── Stage (allowlist) ────────────────────────────────────────────────────────
mkdir -p "$STAGE/src/whatsapp_mcp"
cp "$REPO/manifest.json"   "$STAGE/"
cp "$REPO/pyproject.toml"  "$STAGE/"
cp "$REPO/README.md"       "$STAGE/"
cp "$REPO/LICENSE"         "$STAGE/"
cp "$REPO"/src/whatsapp_mcp/*.py "$STAGE/src/whatsapp_mcp/"

# ── Validate & pack ──────────────────────────────────────────────────────────
$MCPB validate "$STAGE/manifest.json"

mkdir -p "$REPO/dist"
OUT="$REPO/dist/whatsapp-mcp-macos-$PY_VERSION.mcpb"
rm -f "$OUT"
$MCPB pack "$STAGE" "$OUT"

# ── Verify nothing sensitive shipped ─────────────────────────────────────────
CONTENTS="$(unzip -Z1 "$OUT")"
if echo "$CONTENTS" | grep -Ei 'auth_info|mcpregistry|\.venv/|__pycache__|creds\.json|\.git/'; then
    echo "error: bundle contains files that must not ship (listed above)" >&2
    rm -f "$OUT"
    exit 1
fi

# Stable-named copy so the download link on the site survives version bumps:
# https://github.com/.../releases/latest/download/whatsapp-mcp-macos.mcpb
STABLE="$REPO/dist/whatsapp-mcp-macos.mcpb"
cp "$OUT" "$STABLE"

echo
echo "$CONTENTS"
echo
echo "Built $OUT ($(du -h "$OUT" | cut -f1))"
echo "Copied  $STABLE"
