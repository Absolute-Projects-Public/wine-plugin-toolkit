#!/usr/bin/env python3
"""A repeated rescue in the same timestamp must not discard either preset version."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt.presets import _save_one  # noqa: E402


def same_name_same_size_different_content() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-preset-collision-") as tmp:
        root = Path(tmp)
        source = root / "user" / "Tone.xml"
        target = root / "rescue" / "Tone.xml"
        source.parent.mkdir(); target.parent.mkdir()
        target.write_bytes(b"old preset")
        source.write_bytes(b"new preset")  # same byte count, different work
        status, _source, _note = _save_one(source, target, dry_run=False)
        assert target.read_bytes() == b"old preset", "original rescue was overwritten"
        assert status == "saved", status
        snapshots = [p for p in target.parent.iterdir() if p.is_file()]
        assert sorted(p.read_bytes() for p in snapshots) == [b"new preset", b"old preset"], snapshots
    print("ok same-size changed preset keeps both snapshots")


def same_name_different_size() -> None:
    with tempfile.TemporaryDirectory(prefix="wpt-preset-collision-") as tmp:
        root = Path(tmp)
        source = root / "user" / "Tone.xml"
        target = root / "rescue" / "Tone.xml"
        source.parent.mkdir(); target.parent.mkdir()
        target.write_bytes(b"first")
        source.write_bytes(b"second version")
        status, _source, _note = _save_one(source, target, dry_run=False)
        assert status == "saved", status
        assert target.read_bytes() == b"first", "older rescue was overwritten"
        assert any(p.read_bytes() == b"second version" for p in target.parent.iterdir()), "new state not saved"
    print("ok different-size changed preset keeps both snapshots")


if __name__ == "__main__":
    same_name_same_size_different_content()
    same_name_different_size()
    print("preset collision check: 2/2 passed")
