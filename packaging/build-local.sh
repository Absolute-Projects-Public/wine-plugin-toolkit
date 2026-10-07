#!/bin/bash
# Build and archive an unreleased tree from its pinned source tarball, locally.
# The source tree must be committed and clean; never bypass the tarball checksum.
set -euo pipefail
CALLER_PWD=$(pwd -P)
TMP_BASE="${TMPDIR:-/tmp}"
if [[ "$TMP_BASE" != /* ]]; then
    TMP_BASE="$PWD/$TMP_BASE"
fi
[[ -d "$TMP_BASE" ]] || { echo "TMPDIR is not an existing directory: $TMP_BASE" >&2; exit 2; }
TMP_BASE=$(cd "$TMP_BASE" && pwd -P)
export TMPDIR="$TMP_BASE"
cd "$(dirname "$0")/.."
ROOT=$(pwd)
VERSION=$(sed -n 's/^pkgver=//p' PKGBUILD)
[[ -n "$VERSION" ]] || { echo "could not read pkgver from PKGBUILD" >&2; exit 2; }

if (($#)); then
    echo "refusing makepkg options: build-local.sh uses a fixed checksum-verifying invocation" >&2
    exit 2
fi

bash packaging/make-tarball.sh

TARBALL="dist/wpt-$VERSION.tar.gz"
HASH=$(sha256sum "$TARBALL" | cut -d' ' -f1)
PINNED=$(grep -oP "sha256sums=\('\K[0-9a-f]{64}" PKGBUILD || true)
if [[ -z "$PINNED" || "$HASH" != "$PINNED" ]]; then
    echo "refusing package build: $TARBALL hash $HASH does not match PKGBUILD pin ${PINNED:-<missing>}" >&2
    exit 2
fi

ARCHIVE="${WPT_ARCHIVE_DIR:-$HOME/wpt-pkg}"
if [[ "$ARCHIVE" != /* ]]; then ARCHIVE="$CALLER_PWD/$ARCHIVE"; fi
mkdir -p "$ARCHIVE"
ARCHIVE=$(cd "$ARCHIVE" && pwd -P)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/wpt-local-build-XXXX")
WORK=$(cd "$WORK" && pwd -P)
trap 'rm -rf "$WORK"' EXIT
cp PKGBUILD "$WORK/"
# The declared source basename lets makepkg verify the local copy against the pinned sha256sum.
cp "$TARBALL" "$WORK/wpt-$VERSION.tar.gz"
mkdir -p "$WORK/out" "$WORK/build" "$WORK/envroot" "$WORK/tmp"
cd "$WORK"
# The package check suite must not see personal XDG state, a live prefix, or stale release assets.
HOME="$WORK/envroot" \
XDG_CONFIG_HOME="$WORK/envroot/.config" \
XDG_CACHE_HOME="$WORK/envroot/.cache" \
XDG_DATA_HOME="$WORK/envroot/.local/share" \
TMPDIR="$WORK/tmp" \
WINEPREFIX="$WORK/envroot/synthetic-prefix" \
WPT_NO_UPDATE_CHECK=1 \
WPT_RELEASE_DIR="$WORK/envroot/release-source" \
WPT_RELEASE_ASSET_DIR="$WORK/envroot/release-assets" \
PKGDEST="$WORK/out" \
PKGEXT='.pkg.tar.zst' \
SRCDEST="$WORK" \
BUILDDIR="$WORK/build" \
makepkg -f

PACKAGE=$(find "$WORK/out" -maxdepth 1 -name "wine-plugin-toolkit-$VERSION-*-any.pkg.tar.zst" -print -quit)
[[ -n "$PACKAGE" && -f "$PACKAGE" ]] || { echo "makepkg did not produce the expected package" >&2; exit 2; }
for built in "$PACKAGE" "$ROOT/$TARBALL"; do
    destination="$ARCHIVE/$(basename "$built")"
    if [[ -L "$destination" || ( -e "$destination" && ! -f "$destination" ) ]]; then
        echo "refusing to overwrite non-regular archive path: $destination" >&2
        exit 2
    fi
    sidecar="$destination.sha256"
    if [[ -L "$sidecar" || ( -e "$sidecar" && ! -f "$sidecar" ) ]]; then
        echo "refusing to overwrite non-regular archive path: $sidecar" >&2
        exit 2
    fi
    if [[ -f "$destination" ]] && ! cmp -s "$built" "$destination"; then
        echo "refusing to overwrite different archived asset: $destination" >&2
        exit 2
    fi
done
STAGE=$(mktemp -d "$ARCHIVE/.wpt-build-stage-XXXXXX")
trap 'rm -rf "$WORK" "$STAGE"' EXIT
PACKAGE_NAME=$(basename "$PACKAGE")
cp -v "$PACKAGE" "$STAGE/$PACKAGE_NAME"
cp -v "$ROOT/$TARBALL" "$STAGE/wpt-$VERSION.tar.gz"
(
    cd "$STAGE"
    sha256sum "$PACKAGE_NAME" > "$PACKAGE_NAME.sha256"
    sha256sum "wpt-$VERSION.tar.gz" > "wpt-$VERSION.tar.gz.sha256"
)
for staged in "$STAGE"/*; do
    destination="$ARCHIVE/$(basename "$staged")"
    if [[ -L "$destination" || ( -e "$destination" && ! -f "$destination" ) ]]; then
        echo "refusing to overwrite non-regular archive path: $destination" >&2
        exit 2
    fi
    if [[ "$destination" != *.sha256 && -f "$destination" ]] && ! cmp -s "$staged" "$destination"; then
        echo "refusing to overwrite different archived asset: $destination" >&2
        exit 2
    fi
done
for staged in "$STAGE"/*; do
    mv -T -- "$staged" "$ARCHIVE/$(basename "$staged")"
done
find "$ARCHIVE" -maxdepth 1 -type f \( -name 'wine-plugin-toolkit-*.pkg.tar.zst*' -o -name 'wpt-*.tar.gz*' \) -printf '%f\n' | sort
