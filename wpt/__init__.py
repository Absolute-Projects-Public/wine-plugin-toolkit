"""wpt -- Wine Plugin Toolkit.

Installs Windows audio plugins into an ableton-linux Wine prefix by unpacking
the vendor's MSI with msitools and placing the payload itself, then verifies the
result against the MSI's own File table. Also scans a prefix for installs that
registered but never copied their files.
"""

__version__ = "0.6.3"

from .environment import Environment, EnvironmentError_, detect, find_wine_trees  # noqa: F401
from .installer import (  # noqa: F401
    Plan,
    apply_plan,
    build_plan,
    filter_needing_repair,
    render_plan,
    uninstall,
    verify_plan,
)
from .inventory import Inventory, PluginEntry, build as build_inventory, render as render_inventory  # noqa: F401
from .scan import Report, render as render_report, scan as scan_prefix  # noqa: F401

__all__ = [
    "Environment",
    "EnvironmentError_",
    "detect",
    "find_wine_trees",
    "Plan",
    "apply_plan",
    "build_plan",
    "filter_needing_repair",
    "render_plan",
    "uninstall",
    "verify_plan",
    "Inventory",
    "PluginEntry",
    "build_inventory",
    "render_inventory",
    "Report",
    "scan_prefix",
    "render_report",
]