#!/bin/bash
# Build the source tarball the PKGBUILD consumes, then (optionally) makepkg.
#   bash packaging/make-tarball.sh [--build]
set -euo pipefail
cd "$(dirname "$0")/.."

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

# What ships: the package, the tests, the packaging scripts, and the documents written for whoever
# uses it. PROJECT.md and HANDOVER.md are deliberately NOT here - they are the working record, and
# they contain machine-specific paths and notes that do not belong in a release.
for item in wpt tests packaging README.md TESTING.md CONTRIBUTING.md CHANGELOG.md LICENSE pyproject.toml PKGBUILD docs; do
    cp -r "$item" "$ROOT/"
done
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
