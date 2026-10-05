#!/bin/bash
# Build the source tarball the PKGBUILD consumes, then (optionally) makepkg.
#   bash packaging/make-tarball.sh [--build]
set -euo pipefail
umask 022
cd "$(dirname "$0")/.."
python3 packaging/check_version.py

items=(wpt tests packaging README.md TESTING.md CONTRIBUTING.md CHANGELOG.md LICENSE pyproject.toml PKGBUILD docs)
git rev-parse --is-inside-work-tree >/dev/null || { echo "release builder needs a git checkout" >&2; exit 2; }
if [ -n "$(git status --porcelain --untracked-files=all -- "${items[@]}")" ]; then
    echo "release input is dirty: commit or remove changes under the shipped paths first" >&2
    exit 2
fi

VERSION=$(sed -n 's/^pkgver=//p' PKGBUILD)
NAME="wpt-$VERSION.tar.gz"
mkdir -p dist

# Stage a copy first, because the tarball must not contain a hash of itself: the PKGBUILD inside
# the release tarball ships `sha256sums=('SKIP')` (nothing to verify -- the reader already has the
# file), while the PKGBUILD in git carries the real hash of this tarball. Without this the two
# would chase each other: editing the hash changes the tarball, which changes the hash.
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/wpt-tarball-XXXX")
trap 'rm -rf "$STAGE"' EXIT
ROOT="$STAGE/wine-plugin-toolkit-$VERSION"
mkdir -p "$ROOT"

# Ship only committed inputs. Recursive copying of the working tree silently included untracked
# files under docs/tests/etc, even though PROJECT.md and HANDOVER.md themselves were excluded.
# Requiring clean inputs and archiving HEAD also ties every release byte to a reviewable commit.
git archive --format=tar HEAD -- "${items[@]}" | tar -xf - -C "$ROOT"
find "$ROOT" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find "$ROOT" -name '*.pyc' -delete 2>/dev/null || true

sed -i "s|^sha256sums=(.*)$|sha256sums=('SKIP')   # the tarball cannot carry a hash of itself; the PKGBUILD in git has the real one|" \
    "$ROOT/PKGBUILD"

tar --sort=name --owner=0 --group=0 --numeric-owner --mtime='@0' \
    -czf "dist/$NAME" -C "$STAGE" "wine-plugin-toolkit-$VERSION"

echo "wrote dist/$NAME ($(stat -c %s "dist/$NAME") bytes)"
sha256sum "dist/$NAME"

if [ "${1:-}" = "--build" ]; then
    cp "dist/$NAME" .
    makepkg -f
    rm -f "$NAME"
    echo "package: $(find . -maxdepth 1 -name 'wine-plugin-toolkit-*.pkg.tar.zst' -printf '%f\n')"
fi
