"""Where to get more presets, IRs and tones: the community side of this toolkit.

This tool installs and repairs the plugins; it deliberately does **not** download presets from
these sites, because it cannot: several need a sign-in, one sits behind a bot filter, and a
shop needs a purchase. What it can do honestly is take you to the right page in *your own*
browser, pre-filled with the plugin you are looking at, and say what you will find there.

Every entry was checked by hand (2026-09-26). Only sources whose URL structure was verified
carry a search template, inventing a `?search=` parameter that does not exist would be worse
than sending you to the front page.
"""

from __future__ import annotations

import webbrowser
from dataclasses import dataclass


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    kind: str                # "community" | "forum" | "vault" | "shop"
    note: str
    search: str = ""         # template containing {q}; empty when unverified
    sign_in: bool = False

    @property
    def searchable(self) -> bool:
        return bool(self.search)

    def for_product(self, product: str) -> str:
        """The URL to open for a product: its own search where there is one, else the site."""
        if not (self.search and product.strip()):
            return self.url
        return self.search.format(q="+".join(product.split()))


SOURCES: tuple[Source, ...] = (
    Source(
        name="Preset Junkie",
        url="https://presetjunkie.com/",
        kind="community",
        note=(
            "Community repository of Neural DSP presets: free, no subscription, thousands of "
            "presets across the whole Archetype/Fortin range. Your browser is where you sign in"
        ),
        sign_in=True,
    ),
    Source(
        name="Neural DSP forum, preset threads",
        url="https://unity.neuraldsp.com/c/plugins/18",
        kind="forum",
        note=(
            "The vendor's own forum: each plugin has a long-running preset thread where people "
            "post their own .xml files (Nolly's is past 100 posts). The search goes straight to it"
        ),
        search="https://unity.neuraldsp.com/search?q={q}+presets",
    ),
    Source(
        name="Honest Amp Sims, Preset Vault",
        url="https://honestampsimreviews.com/preset-vault/",
        kind="vault",
        note=(
            "Free preset vault, filed by plugin (its Nolly folder alone holds ~180 presets split "
            "into clean/crunch/lead/rhythm)"
        ),
    ),
    Source(
        name="r/NeuralDSP, presets thread",
        url="https://www.reddit.com/r/NeuralDSP/comments/p86qh2/presets_thread/",
        kind="community",
        note="The subreddit's standing preset-sharing thread, plus links to Dropbox and Discord dumps",
    ),
    Source(
        name="Develop Device, NDSP packs",
        url="https://developdevice.com/collections/neural-dsp-presets-and-irs",
        kind="shop",
        note="Paid expansion packs and cabinet IRs per plugin (Gojira, Nameless, Nolly, Petrucci)",
    ),
    Source(
        name="Komposition 101, NDSP packs",
        url="https://www.komposition101.com/ndsp-presets",
        kind="shop",
        note="Paid NDSP preset packs, sold individually or as a bundle",
    ),
)


def find(name: str) -> Source | None:
    """Look a source up by name, exactly or by a unique-ish fragment."""
    wanted = name.strip().lower()
    for source in SOURCES:
        if source.name.lower() == wanted:
            return source
    matches = [s for s in SOURCES if wanted and wanted in s.name.lower()]
    if len(matches) == 1:
        return matches[0]
    return None


def open_source(name: str, product: str = "") -> tuple[bool, str]:
    """Open a source in the user's browser. Returns (opened, url).

    Nothing is fetched from here and no credentials are involved: the page opens in whatever
    browser session the user already has, which is the only way a sign-in-gated or bot-walled
    site can be looked at.
    """
    source = find(name)
    if source is None:
        return False, ""
    url = source.for_product(product)
    try:
        opened = webbrowser.open(url)
    except Exception:  # noqa: BLE001 - no browser available is not a crash worth having
        opened = False
    return bool(opened), url


def render(sources: tuple[Source, ...] = SOURCES) -> str:
    lines = ["preset and IR sources (opened in your own browser - nothing is downloaded for you)"]
    width = max(len(s.name) for s in sources)
    for source in sources:
        flags = []
        if source.searchable:
            flags.append("search per plugin")
        if source.sign_in:
            flags.append("sign-in")
        lines.append(
            f"  {source.name.ljust(width)}  {source.kind:<9}  {source.url}"
            + (f"   [{', '.join(flags)}]" if flags else "")
        )
        lines.append(f"      {source.note}")
    lines.append("")
    lines.append("  wpt presets --open \"Preset Junkie\" --product Nolly     open one, for a plugin")
    lines.append("  wpt presets --open \"forum\" --product \"Tim Henson\"      (source names match loosely)")
    return "\n".join(lines)
