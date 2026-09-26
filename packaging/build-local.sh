#!/bin/bash
# Build and install-check an unreleased tree, locally.
#
# PKGBUILD builds from the GitHub tag archive (correct for everyone else, and the hash can only be
# filled in once the tag exists). For a working tree you have not tagged yet, this stages the local
# tarball where makepkg expects it and skips the checksum check.
set -eu
cd "$(dirname "$0")/.."
VERSION=$(python3 -c "import sys; sys.path.insert(0,'.'); import wpt; print(wpt.__version__)")

bash packaging/make-tarball.sh
cp "dist/wpt-$VERSION.tar.gz" .

WORK=$(mktemp -d /tmp/wpt-local-build-XXXX)
trap 'rm -rf "$WORK"' EXIT
cp PKGBUILD "$WORK/"
cp "wpt-$VERSION.tar.gz" "$WORK/"
mkdir -p "$WORK/src"
cp "wpt-$VERSION.tar.gz" "$WORK/$PKGNAME-$VERSION.tar.gz" 2>/dev/null || true
cp "wpt-$VERSION.tar.gz" "$WORK/wine-plugin-toolkit-$VERSION.tar.gz"
cd "$WORK"
makepkg -f --skipchecksums "$@"
ls -1 wine-plugin-toolkit-*.pkg.tar.zst
