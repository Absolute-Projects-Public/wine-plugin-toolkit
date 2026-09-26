#!/bin/bash
# Publish a release: build the artefacts, then print exactly what to do with the pieces.
#
# Order matters, and it is why this script exists:
#   1. Everything that ships must be final FIRST - including this file, packaging/build-local.sh and
#      the PKGBUILD - because they are all inside the tarball.
#   2. Build the tarball and take its sha256.
#   3. Put that hash in the PKGBUILD's sha256sums (the line is replaced with SKIP inside the tarball,
#      so pinning it does not change the tarball - that is what makes this a fixed point).
#   4. Build the Arch package from that tarball.
#   5. Commit, tag v<version>, push, and attach the assets to a GitHub release.
#
# The package asset is the one that matters to `wpt update`: every release must attach
# wine-plugin-toolkit-<version>-1-any.pkg.tar.zst or the updater has nothing to install.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(python3 -c "import sys; sys.path.insert(0,'.'); import wpt; print(wpt.__version__)")
REPO="Absolute-Projects-Public/wine-plugin-toolkit"
TARBALL="dist/wpt-$VERSION.tar.gz"

echo "==> building the source tarball"
bash packaging/make-tarball.sh

HASH=$(sha256sum "$TARBALL" | cut -d' ' -f1)
PINNED=$(grep -oP "sha256sums=\('\K[0-9a-f]{64}" PKGBUILD || true)
echo
if [ "$HASH" != "$PINNED" ]; then
    echo "==> the tarball hash has changed; pin it before building the package:"
    echo "      sha256sums=('$HASH')"
    echo "    (a file that ships was edited after the last build)"
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

==> also write the package's checksum (the updater verifies against it):
      ( cd ~/wpt-pkg && sha256sum "wine-plugin-toolkit-$VERSION-1-any.pkg.tar.zst" \
            > "wine-plugin-toolkit-$VERSION-1-any.pkg.tar.zst.sha256" )

==> publish
  1. git add -A && git commit
  2. git tag -a v$VERSION -m "wpt $VERSION" && git push origin main --tags
  3. create the GitHub release for v$VERSION and attach:
       $TARBALL                     (source)
       dist/wpt-$VERSION.tar.gz.sha256
       wine-plugin-toolkit-$VERSION-1-any.pkg.tar.zst.sha256
       wine-plugin-toolkit-$VERSION-1-any.pkg.tar.zst   (what 'wpt update' installs)
  4. verify with a machine that has an older wpt installed:
       wpt update
EOF
