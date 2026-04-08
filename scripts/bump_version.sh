#!/usr/bin/env bash
# bump_version.sh — Update version in manifest.json and create a git tag.
#
# Usage:
#   ./scripts/bump_version.sh <new_version>
#
# Example:
#   ./scripts/bump_version.sh 0.2.0
#
# What it does:
#   1. Validates the version format (semver: X.Y.Z)
#   2. Updates "version" in custom_components/never_dry/manifest.json
#   3. Creates a git commit with the version bump
#   4. Creates an annotated git tag v<new_version>
#
# To trigger the release workflow, push the tag:
#   git push origin v<new_version>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MANIFEST="$REPO_ROOT/custom_components/never_dry/manifest.json"

# ── Validate arguments ────────────────────────────────────
if [ $# -ne 1 ]; then
    echo "Usage: $0 <new_version>"
    echo "Example: $0 0.2.0"
    exit 1
fi

NEW_VERSION="$1"

if ! echo "$NEW_VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
    echo "ERROR: Version must be semver format (X.Y.Z), got: $NEW_VERSION"
    exit 1
fi

# ── Check clean working tree ──────────────────────────────
if ! git -C "$REPO_ROOT" diff --quiet || ! git -C "$REPO_ROOT" diff --cached --quiet; then
    echo "ERROR: Working tree has uncommitted changes. Commit or stash first."
    exit 1
fi

# ── Check tag doesn't already exist ──────────────────────
if git -C "$REPO_ROOT" rev-parse "v$NEW_VERSION" >/dev/null 2>&1; then
    echo "ERROR: Tag v$NEW_VERSION already exists."
    exit 1
fi

# ── Read current version ──────────────────────────────────
CURRENT_VERSION=$(python3 -c "import json; print(json.load(open('$MANIFEST'))['version'])")
echo "Current version: $CURRENT_VERSION"
echo "New version:     $NEW_VERSION"

# ── Update manifest.json ──────────────────────────────────
python3 -c "
import json, pathlib
p = pathlib.Path('$MANIFEST')
m = json.loads(p.read_text())
m['version'] = '$NEW_VERSION'
p.write_text(json.dumps(m, indent=2) + '\n')
"

echo "Updated $MANIFEST"

# ── Git commit + tag ──────────────────────────────────────
cd "$REPO_ROOT"
git add custom_components/never_dry/manifest.json
git commit -m "release: bump version to $NEW_VERSION"
git tag -a "v$NEW_VERSION" -m "Release v$NEW_VERSION"

echo ""
echo "Done! Version bumped to $NEW_VERSION."
echo "To publish the release, push the tag:"
echo "  git push origin main && git push origin v$NEW_VERSION"
