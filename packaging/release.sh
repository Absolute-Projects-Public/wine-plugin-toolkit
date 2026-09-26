#!/bin/bash
# Publish a release: tag it, build both artefacts, and hand the assets to GitHub.
#
# Run from the repository root on a machine with push access. It never force-pushes and never
# deletes anything. The GitHub release itself is created afterwards (browser or `gh release create`)
# with the files this script prints.
set -eu
VERSION=$(python3 -c "import sys; sys.path.insert(0,'.'); import wpt; print(wpt.__version__)")
TARBALL="dist/wpt-$VERSION.tar.gz"

echo "==> building the source tarball"
bash packaging/make-tarball.sh

echo "==> checksums"
( cd dist && sha256sum "wpt-$VERSION.tar.gz" > "wpt-$VERSION.tar.gz.sha256" )
cat "$TARBALL.sha256"

echo
echo "next:"
echo "  1. git tag -a v$VERSION -m 'wpt $VERSION' && git push origin main --tags"
echo "  2. build the package from the tag archive, then copy its sha256 into PKGBUILD:"
echo "       curl -sL https://github.com/Absolute-Projects-Public/wine-plugin-toolkit/archive/refs/tags/v$VERSION.tar.gz | sha256sum"
echo "  3. create the GitHub release for v$VERSION with these assets:"
echo "       $TARBALL"
echo "       wpt-$VERSION.tar.gz.sha256"
echo "       wine-plugin-toolkit-$VERSION-1-any.pkg.tar.zst"
echo
echo "The toolkit's own update check reads the newest release and installs the .pkg.tar.zst asset,"
echo "so that asset is the one that must be attached to every release."
