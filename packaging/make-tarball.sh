#!/bin/bash
# Build the source tarball the PKGBUILD consumes, then (optionally) makepkg.
#   bash packaging/make-tarball.sh [--build]
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(sed -n 's/^pkgver=//p' PKGBUILD)
NAME="wpt-$VERSION.tar.gz"
mkdir -p dist

# deterministic-ish: sorted names, no owner metadata, tarball root is wpt-<ver>/
# What ships: the package, the tests, the packaging scripts, and the documents written for
# whoever uses it. PROJECT.md and HANDOVER.md are deliberately NOT here - they are the working
# record, and they contain machine-specific paths and notes that do not belong in a release.
tar --exclude='__pycache__' --exclude='*.pyc' --exclude='dist' --exclude='screenshots' \
    --exclude='presets-exported-*' --exclude='presets-rescued-*' --exclude='wrapper-corpus-*' \
    --sort=name --owner=0 --group=0 --mtime='@0' \
    --transform "s,^\.,wine-plugin-toolkit-$VERSION," \
    -czf "dist/$NAME" ./wpt ./tests ./packaging ./README.md ./TESTING.md ./CHANGELOG.md \
        ./LICENSE ./pyproject.toml ./PKGBUILD ./docs

echo "wrote dist/$NAME ($(stat -c %s "dist/$NAME") bytes)"

if [ "${1:-}" = "--build" ]; then
    cp "dist/$NAME" .
    makepkg -f
    rm -f "$NAME"
    echo "package: $(ls -1 wine-plugin-toolkit-*.pkg.tar.zst)"
fi
