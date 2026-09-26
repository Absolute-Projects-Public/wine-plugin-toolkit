#!/bin/bash
# Build and install-check an unreleased tree, locally.
#
# PKGBUILD builds from the source tarball attached to the GitHub release (correct for everyone else,
# and its hash is pinned there). For a working tree, this stages the same tarball under the name the
# PKGBUILD declares and skips the checksum, because the release the hash refers to does not exist yet.
set -eu
cd "$(dirname "$0")/.."
VERSION=$(python3 -c "import sys; sys.path.insert(0,'.'); import wpt; print(wpt.__version__)")
ROOT=$(pwd)

bash packaging/make-tarball.sh
cp "dist/wpt-$VERSION.tar.gz" .

WORK=$(mktemp -d /tmp/wpt-local-build-XXXX)
trap 'rm -rf "$WORK"' EXIT
cp PKGBUILD "$WORK/"
# the name PKGBUILD declares for its source: wpt-<version>.tar.gz
cp "wpt-$VERSION.tar.gz" "$WORK/wpt-$VERSION.tar.gz"
cd "$WORK"
OLDPWD="$ROOT"
makepkg -f --skipchecksums "$@"

# keep every build: ~/wpt-pkg is the archive of record (0.1.0 onwards, never tidied)
ARCHIVE="${WPT_ARCHIVE_DIR:-$HOME/wpt-pkg}"
mkdir -p "$ARCHIVE"
cp -v wine-plugin-toolkit-*-any.pkg.tar.zst "$ARCHIVE/"
cp -v "$OLDPWD/dist/wpt-$VERSION.tar.gz" "$ARCHIVE/" 2>/dev/null || true
find "$ARCHIVE" -maxdepth 1 -name 'wine-plugin-toolkit-*.pkg.tar.zst' -printf '%f\n' | sort -V | tail -2
