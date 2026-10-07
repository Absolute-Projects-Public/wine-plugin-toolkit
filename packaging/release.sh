#!/bin/bash
# Publish a release: build the artefacts, then print exactly what to do with the pieces.
#
# Order matters, and it is why this script exists:
#   1. Commit every shipped file FIRST. make-tarball.sh now refuses dirty input and archives HEAD,
#      so a working-tree copy (including untracked files under docs/tests) cannot become a release.
#   2. Build the tarball and take its sha256.
#   3. Put that hash in PKGBUILD's sha256sums and commit the pin. The tarball's PKGBUILD replaces
#      the pin with SKIP; rebuilding from the pin commit must yield the SAME tarball hash.
#   4. Build the Arch package, run the release gate from the extracted tarball, then tag/push.
# gh must be authenticated and reachable to prove the version is not already published; an
# unavailable gh or unexpected API response stops here rather than guessing from local tags.
#
# The package asset is the one that matters to `wpt update`: every release must attach
# wine-plugin-toolkit-<version>-<pkgrel>-any.pkg.tar.zst or the updater has nothing to install.
set -euo pipefail
CALLER_PWD=$(pwd -P)
TMPDIR_BASE="${TMPDIR:-/tmp}"
if [[ "$TMPDIR_BASE" != /* ]]; then TMPDIR_BASE="$CALLER_PWD/$TMPDIR_BASE"; fi
export TMPDIR="$TMPDIR_BASE"
cd "$(dirname "$0")/.."

# Read pkgver directly so the release gate and tarball builder use the same declared version.
VERSION=$(sed -n 's/^pkgver=//p' PKGBUILD)
PKGREL=$(sed -n 's/^pkgrel=//p' PKGBUILD)
if [[ ! "$PKGREL" =~ ^[0-9]+$ ]]; then
    echo "PKGBUILD has no numeric pkgrel; refusing to continue" >&2
    exit 2
fi
REPO="Absolute-Projects-Public/wine-plugin-toolkit"
TARBALL="dist/wpt-$VERSION.tar.gz"
ARCHIVE_DIR="${WPT_ARCHIVE_DIR:-$HOME/wpt-pkg}"
if [[ "$ARCHIVE_DIR" != /* ]]; then
    ARCHIVE_DIR="$CALLER_PWD/$ARCHIVE_DIR"
fi
if git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null; then
    echo "refusing to publish $VERSION: tag already exists; choose a new version" >&2
    exit 2
fi
if ! command -v gh >/dev/null; then
    echo "cannot verify remote release tags: gh is unavailable; refusing to publish" >&2
    exit 2
fi
if remote_tag=$(gh api "repos/$REPO/releases/tags/v$VERSION" --jq '.tag_name' 2>&1); then
    echo "refusing to publish $VERSION: already published as $remote_tag" >&2
    exit 2
elif [[ "$remote_tag" != *"HTTP 404"* ]]; then
    echo "cannot verify whether v$VERSION is already published; refusing to publish" >&2
    exit 2
fi

echo "==> building the source tarball"
bash packaging/make-tarball.sh

HASH=$(sha256sum "$TARBALL" | cut -d' ' -f1)
PINNED=$(grep -oP "sha256sums=\('\K[0-9a-f]{64}" PKGBUILD || true)
echo
if [ "$HASH" != "$PINNED" ]; then
    echo "==> the tarball hash has changed; pin it before building the package:"
    echo "      sha256sums=('$HASH')"
    echo "    commit that pin, run this script again, and confirm the hash is unchanged"
    exit 2
else
    echo "==> PKGBUILD already pins this tarball: $HASH"
fi

echo
echo "==> checksum files for the release (one per asset)"
( cd dist && sha256sum "wpt-$VERSION.tar.gz" > "wpt-$VERSION.tar.gz.sha256" )
cat "dist/wpt-$VERSION.tar.gz.sha256"

echo
echo "==> build the package from the tarball (check the hash above matches the PKGBUILD first)"
echo "      bash packaging/build-local.sh"

cat <<EOF

==> packaging/build-local.sh writes source and package checksum files under:
      $ARCHIVE_DIR

==> run the extracted-tarball gate on an Arch-family machine with the updater assets required:
  export WPT_RELEASE_DIR=/path/to/extracted-candidate
  export WPT_RELEASE_ASSET_DIR="$ARCHIVE_DIR"
  export WPT_REQUIRE_UPDATER_E2E=1
  bash "\$WPT_RELEASE_DIR/packaging/run-suites.sh" "\$WPT_RELEASE_DIR"

==> publish
  1. confirm the clean committed tree, fixed-point pin, and full extracted-tarball gate
  2. git tag -a v$VERSION -m "wpt $VERSION" && push fast-forward from the publishing machine
  3. create the GitHub release for v$VERSION and attach:
       $ARCHIVE_DIR/wpt-$VERSION.tar.gz
       $ARCHIVE_DIR/wpt-$VERSION.tar.gz.sha256
       $ARCHIVE_DIR/wine-plugin-toolkit-$VERSION-$PKGREL-any.pkg.tar.zst.sha256
       $ARCHIVE_DIR/wine-plugin-toolkit-$VERSION-$PKGREL-any.pkg.tar.zst   (what 'wpt update' installs)
  4. verify with a machine that has an older wpt installed:
       wpt update
EOF
