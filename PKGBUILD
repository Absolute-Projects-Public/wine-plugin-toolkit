# Maintainer: Absolute-Projects-Public <334208341+Absolute-Projects-Public@users.noreply.github.com>
# Builds from the source tarball attached to the GitHub release, so what is verified here is the
# exact tree that was tested. The copy of this file *inside* that tarball says sha256sums=('SKIP')
# -- a tarball cannot carry a hash of itself; this one, in git, pins it.
# For a local test build of an unreleased tree, use packaging/build-local.sh instead.
pkgname=wine-plugin-toolkit
pkgver=0.6.5
pkgrel=1
pkgdesc="Install, repair and inventory Windows audio plugins in an ableton-linux Wine prefix"
arch=('any')
url="https://github.com/Absolute-Projects-Public/wine-plugin-toolkit"
license=('MIT')
depends=('python' 'msitools')
optdepends=(
    'pyside6: graphical front end (wpt-gui)'
    '7zip: unpack NSIS / 7-Zip SFX / Burn-bundle wrappers without Wine'
    'cabextract: unpack CAB and IExpress wrappers, and the cabinets inside them, without Wine'
    'innoextract: unpack Inno Setup wrappers without Wine'
    'unshield: unpack InstallShield wrappers without Wine'
)
provides=('wpt')
options=('!strip')

# The release asset, not GitHub's auto-generated tag archive: the asset is the exact tree this
# package was built and tested from, and its hash is stable and verifiable.
source=("wpt-$pkgver.tar.gz::https://github.com/Absolute-Projects-Public/wine-plugin-toolkit/releases/download/v$pkgver/wpt-$pkgver.tar.gz")
sha256sums=('62bd74f441ddc2212d5d3c416494993df0ae4a3c786dd5059bf7e3efaab880a6')

check() {
    # The core suites are stdlib-only, so they run wherever this package is built.
    # The offscreen GUI suites need PySide6 (an optdepend); see packaging/run-suites.sh.
    cd "$srcdir/$pkgname-$pkgver"
    python3 tests/test_core.py
    python3 tests/test_prefix_integration.py
}

package() {
    install -d "$pkgdir/usr/lib/wpt"
    cp -r "$srcdir/$pkgname-$pkgver/wpt" "$pkgdir/usr/lib/wpt/"
    # check() runs the test suite in $srcdir, which leaves __pycache__ behind; bytecode has no
    # business in a package (pacman regenerates it, or does not - either way it is not ours to ship)
    find "$pkgdir" -type d -name __pycache__ -prune -exec rm -rf {} +

    install -Dm644 "$srcdir/$pkgname-$pkgver/README.md" "$pkgdir/usr/share/doc/$pkgname/README.md"
    install -Dm644 "$srcdir/$pkgname-$pkgver/TESTING.md" "$pkgdir/usr/share/doc/$pkgname/TESTING.md"
    install -Dm644 "$srcdir/$pkgname-$pkgver/pyproject.toml" "$pkgdir/usr/share/doc/$pkgname/pyproject.toml"
    install -Dm644 "$srcdir/$pkgname-$pkgver/CONTRIBUTING.md" "$pkgdir/usr/share/doc/$pkgname/CONTRIBUTING.md"
    install -Dm644 "$srcdir/$pkgname-$pkgver/LICENSE" "$pkgdir/usr/share/doc/$pkgname/LICENSE"
    # the README embeds these two images; without them the packaged docs have broken links
    cp -r "$srcdir/$pkgname-$pkgver/docs" "$pkgdir/usr/share/doc/$pkgname/"

    install -Dm755 "$srcdir/$pkgname-$pkgver/packaging/wpt" "$pkgdir/usr/bin/wpt"
    install -Dm755 "$srcdir/$pkgname-$pkgver/packaging/wpt-gui" "$pkgdir/usr/bin/wpt-gui"

    # the launcher entry and its icon: without these, wpt-gui has no menu entry and no icon
    install -Dm644 "$srcdir/$pkgname-$pkgver/packaging/wpt-gui.desktop" \
        "$pkgdir/usr/share/applications/wpt-gui.desktop"
    for size in 16 32 48 64 128 256 512; do
        install -Dm644 "$srcdir/$pkgname-$pkgver/wpt/data/icons/wpt-$size.png" \
            "$pkgdir/usr/share/icons/hicolor/${size}x${size}/apps/wpt-gui.png"
    done

    install -Dm644 "$srcdir/$pkgname-$pkgver/LICENSE" "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
    install -Dm644 "$srcdir/$pkgname-$pkgver/CHANGELOG.md" "$pkgdir/usr/share/doc/$pkgname/CHANGELOG.md"

    # completions are generated from the real argument parser, so they cannot drift
    ( cd "$srcdir/$pkgname-$pkgver" && PYTHONPATH=. python3 -m wpt.cli completions fish ) \
        > "$srcdir/wpt.fish"
    ( cd "$srcdir/$pkgname-$pkgver" && PYTHONPATH=. python3 -m wpt.cli completions bash ) \
        > "$srcdir/wpt.bash"
    ( cd "$srcdir/$pkgname-$pkgver" && PYTHONPATH=. python3 -m wpt.cli completions zsh ) \
        > "$srcdir/_wpt"
    install -Dm644 "$srcdir/wpt.fish" "$pkgdir/usr/share/fish/vendor_completions.d/wpt.fish"
    install -Dm644 "$srcdir/wpt.bash" "$pkgdir/usr/share/bash-completion/completions/wpt"
    install -Dm644 "$srcdir/_wpt" "$pkgdir/usr/share/zsh/site-functions/_wpt"
}
