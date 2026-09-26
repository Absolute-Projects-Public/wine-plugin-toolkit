"""The vendor's own download catalogue.

Neural DSP keeps every plugin's installers on one page. Reading that page gives the
toolkit the product list it cannot get from a prefix: what exists, which version is
current, and where the Windows installer lives. The Downloads tab and `wpt catalogue`
are both built on this.

Two things worth knowing about those links, because they shape the whole feature:

* plugin links go to `neuraldsp.com/download-confirmation/...`, which **requires being
  signed in** and owning the plugin -- so the toolkit opens that page in *your* browser
  rather than pretending it can download it for you;
* hardware/manual links go straight to `downloads.neuraldsp.com/...` and are public, so
  those can be fetched directly.

A parsed snapshot is bundled, so the catalogue works offline; `refresh` re-reads the page.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DOWNLOADS_URL = "https://neuraldsp.com/downloads"
CONFIRMATION_HOST = "neuraldsp.com/download-confirmation"
DIRECT_HOST = "downloads.neuraldsp.com"
USER_AGENT = "wpt/0.4 (+wine plugin toolkit; reads the public downloads page)"
SNAPSHOT = Path(__file__).with_name("data") / "neuraldsp-catalogue.json"

HEADING_RE = re.compile(r"<h([23])[^>]*>(.*?)</h\1>|^(#{2,3})\s+(.*)$", re.IGNORECASE | re.MULTILINE | re.DOTALL)
LINK_RE = re.compile(
    r"\[([^\]]+)\]\(([^\s)]+)\)"                                   # markdown link
    r"|<a[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>",                   # html link
    re.IGNORECASE | re.DOTALL,
)
VERSION_RE = re.compile(
    r"(\d+\.\d+(?:\.\d+)?(?:\s*\(X\)|\s*Ultimate|\s*Beta)?)\s*[-–]\s*([A-Z][a-z]{2} \d{1,2}, \d{4})"
)
TAG_RE = re.compile(r"<[^>]+>")
SITE = "https://neuraldsp.com"
# Headings on the page that are structure, not products.
STRUCTURE = {"hardware", "plugins", "software", "other", "installers", "changelog",
             "past releases", "manual", "downloads"}


def _clean(text: str) -> str:
    text = TAG_RE.sub(" ", text or "")
    text = text.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    return re.sub(r"\s+", " ", text).strip()


def _absolute(url: str) -> str:
    url = url.replace("&amp;", "&").strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return SITE + url
    return url


def parse(text: str, group: str = "Plugins") -> list[Release]:
    """Parse the downloads page (the served HTML, or the markdown rendering of it).

    The page is React-rendered but server-side: products are `<h3>` blocks, the version
    line is `1.1.0 (X) - Jul 29, 2026`, and plugin links are **relative**
    (`/download-confirmation/<slug>?version=...&platform=pc`) while the hardware ones are
    absolute CDN links. Both shapes are handled, and headings of either level move the
    current group.
    """
    releases: list[Release] = []
    current_group = group
    blocks = list(HEADING_RE.finditer(text))
    for index, match in enumerate(blocks):
        heading = _clean(match.group(2) or match.group(4) or "")
        level = match.group(1) or "#" * len(match.group(3) or "")
        if not heading:
            continue
        start = match.end()
        end = blocks[index + 1].start() if index + 1 < len(blocks) else len(text)
        body = text[start:end]

        lowered = heading.lower()
        if lowered in STRUCTURE or (len(level) == 2 and ":" not in heading and lowered != "archetype"):
            current_group = heading if lowered in STRUCTURE else current_group
            continue

        version = released = ""
        version_match = VERSION_RE.search(_clean(body))
        if version_match:
            version, released = version_match.group(1).strip(), version_match.group(2)

        release = Release(product=heading, version=version, released=released, group=current_group)
        for link in LINK_RE.finditer(body):
            label = _clean(link.group(1) or link.group(4) or "")
            url = _absolute(link.group(2) or link.group(3) or "")
            if not url:
                continue
            lowered_label = label.lower()
            if "windows" in lowered_label:
                release.windows = url
            elif "mac" in lowered_label:
                release.macos = url
            elif "manual" in lowered_label or url.lower().endswith(".pdf"):
                release.manual = url
        if release.windows or release.macos:
            releases.append(release)
    return releases


@dataclass
class Release:
    product: str
    version: str = ""
    released: str = ""
    windows: str = ""
    macos: str = ""
    manual: str = ""
    group: str = "Plugins"

    @property
    def slug(self) -> str:
        """The last path segment of the download link -- stable per product."""
        for url in (self.windows, self.macos):
            match = re.search(r"/download-confirmation/([^?]+)", url)
            if match:
                return match.group(1)
        return re.sub(r"[^a-z0-9]+", "-", self.product.lower()).strip("-")

    @property
    def needs_sign_in(self) -> bool:
        return CONFIRMATION_HOST in self.windows

    @property
    def direct_download(self) -> bool:
        return bool(self.windows) and DIRECT_HOST in self.windows


@dataclass
class Catalogue:
    fetched: str = ""
    source: str = ""
    releases: list[Release] = field(default_factory=list)

    def find(self, needle: str) -> list[Release]:
        needle = needle.strip().lower()
        exact = [r for r in self.releases if r.slug == needle or r.product.lower() == needle]
        if exact:
            return exact
        return [r for r in self.releases if needle in r.product.lower() or needle in r.slug]

    def versions(self) -> dict[str, str]:
        return {r.product: r.version for r in self.releases}


def load_snapshot(path: Path | None = None) -> Catalogue:
    path = Path(path) if path else SNAPSHOT
    if not path.is_file():
        return Catalogue(source="none")
    payload = json.loads(path.read_text())
    return Catalogue(
        fetched=payload.get("fetched", ""),
        source=payload.get("source", str(path)),
        releases=[Release(**item) for item in payload.get("releases", [])],
    )


def save_snapshot(catalogue: Catalogue, path: Path | None = None) -> Path:
    path = Path(path) if path else SNAPSHOT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "fetched": catalogue.fetched,
                "source": catalogue.source,
                "releases": [r.__dict__ for r in catalogue.releases],
            },
            indent=2,
        )
        + "\n"
    )
    return path


def fetch(url: str = DOWNLOADS_URL, timeout: int = 30) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed https URL
        return response.read().decode("utf-8", errors="replace")


def refresh(url: str = DOWNLOADS_URL, log=None) -> Catalogue:
    """Re-read the vendor's page; falls back to the bundled snapshot if that fails."""
    say = log or (lambda _message: None)
    import time

    try:
        say(f"reading {url} ...")
        text = fetch(url)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        say(f"could not read the vendor page ({exc}) - using the bundled snapshot")
        return load_snapshot()

    releases = parse(text)
    if not releases:
        say("the page parsed to nothing (layout changed?) - using the bundled snapshot")
        return load_snapshot()

    catalogue = Catalogue(
        fetched=time.strftime("%Y-%m-%d %H:%M"),
        source=url,
        releases=releases,
    )
    say(f"{len(releases)} releases found")
    return catalogue


def download(url: str, destination: Path, log=None) -> Path:
    """Fetch a direct link (public CDN only) into `destination`."""
    say = log or (lambda _message: None)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    name = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name) or "download.bin"
    target = destination / name
    say(f"downloading {name} ...")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as handle:  # noqa: S310
        total = 0
        while chunk := response.read(1024 * 256):
            handle.write(chunk)
            total += len(chunk)
    say(f"saved {target} ({total / 1e6:.1f} MB)")
    return target


def render(catalogue: Catalogue, limit: int | None = None) -> str:
    lines: list[str] = []
    if catalogue.fetched:
        lines.append(f"Neural DSP downloads ({catalogue.source}), read {catalogue.fetched}")
    else:
        lines.append("Neural DSP downloads (bundled snapshot - run with --refresh for the live page)")
    lines.append("-" * 72)
    groups: dict[str, list[Release]] = {}
    for release in catalogue.releases:
        groups.setdefault(release.group, []).append(release)
    for group, releases in groups.items():
        lines.append("")
        lines.append(f"{group} ({len(releases)})")
        width = max(len(r.product) for r in releases)
        for release in releases[:limit]:
            where = "page (sign in)" if release.needs_sign_in else ("direct" if release.direct_download else "?")
            lines.append(
                f"  {release.product.ljust(width)}  {release.version or '?':<14} "
                f"{release.released or '':<13} {where}"
            )
            lines.append(f"      {release.windows}")
        if limit and len(releases) > limit:
            lines.append(f"  ... and {len(releases) - limit} more")
    lines.append("")
    lines.append("Plugin links need you signed in to Neural DSP, so open them in your browser:")
    lines.append("  wpt catalogue --open <product>      or the Download Plugins tab in the GUI")
    lines.append("The file lands in ~/Downloads, and `wpt pending` (or that tab) then offers to install it.")
    return "\n".join(lines)
