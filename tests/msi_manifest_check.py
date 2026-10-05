#!/usr/bin/env python3
"""The MSI File table must retain full relative paths, sizes, and duplicate basenames.

The fixture mirrors the Directory/Component/File table shape observed with msiinfo export. No MSI,
Wine prefix, or real user data is touched.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wpt import msi  # noqa: E402


BASE_TABLES = {
    "Directory": [
        ["TARGETDIR", "", "SourceDir"],
        ["VST3DIR", "TARGETDIR", "VST3DIR"],
        ["Bundle", "VST3DIR", "PLUGIN~1|Plugin.vst3"],
        ["Contents", "Bundle", "Contents"],
        ["A", "Contents", "A"],
        ["B", "Contents", "B"],
        ["Dot", "A", "."],
        ["PREDIR", "TARGETDIR", "PREDIR"],
        ["Factory", "PREDIR", "FACT~1|Factory:SrcFactory"],
    ],
    "Component": [["C1", "{1}", "A"], ["C2", "{2}", "B"],
                  ["C3", "{3}", "Dot"], ["C4", "{4}", "Factory"]],
    "File": [
        ["f1", "C1", "PLUGIN~1.ICO|Plugin.ico", "4096"],
        ["f2", "C2", "PLUGIN~2.ICO|Plugin.ico", "999999"],
        ["f3", "C3", "Empty.bin", "0"],
        ["f4", "C4", "Factory.xml", "7"],
    ],
}


def distinct_files_share_basename_without_collapsing() -> None:
    with patch.object(msi, "export_table", side_effect=lambda _msi, table: BASE_TABLES[table]):
        entries = msi.file_manifest(Path("fixture.msi"))
    actual = {(e.root, e.relative.as_posix(), e.size) for e in entries}
    expected = {
        ("VST3DIR", "Plugin.vst3/Contents/A/Plugin.ico", 4096),
        ("VST3DIR", "Plugin.vst3/Contents/B/Plugin.ico", 999999),
        ("VST3DIR", "Plugin.vst3/Contents/A/Empty.bin", 0),
        ("PREDIR", "Factory/Factory.xml", 7),
    }
    assert actual == expected, (actual, expected)
    assert {e.file_key for e in entries} == {"f1", "f2", "f3", "f4"}
    print("ok duplicate basenames, dot directories and target:source yield four distinct files")


def ambiguous_same_destination_refuses() -> None:
    tables = {key: [list(row) for row in value] for key, value in BASE_TABLES.items()}
    tables["File"].append(["f5", "C1", "plugin.ICO", "100"])
    with patch.object(msi, "export_table", side_effect=lambda _msi, table: tables[table]):
        try:
            msi.file_manifest(Path("fixture.msi"))
        except msi.MsiError as exc:
            assert "ambiguous" in str(exc).lower(), str(exc)
        else:
            raise AssertionError("two File rows resolved to the same case-insensitive destination")
    print("ok case-insensitive destination collision refuses instead of guessing")


if __name__ == "__main__":
    distinct_files_share_basename_without_collapsing()
    ambiguous_same_destination_refuses()
    print("MSI manifest check: 2/2 passed")
