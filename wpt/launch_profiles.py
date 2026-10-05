"""Saved Wine launch profiles for the WPT GUI loader."""
from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from types import MappingProxyType
from typing import Mapping
import uuid

from .environment import Environment, EnvironmentError_, detect, validate_env_overrides

SCHEMA_VERSION = 1
_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")
_PROFILE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class ProfileConfigError(ValueError):
    """A saved launch profile is invalid or could not be read safely."""


def parse_environment_text(text: str) -> dict[str, str]:
    """Parse literal NAME=VALUE lines without splitting or trimming values."""
    if not isinstance(text, str):
        raise ProfileConfigError("environment overrides must be text")
    values: dict[str, str] = {}
    for line_number, line in enumerate(text.split("\n"), start=1):
        if line.endswith("\r"):
            line = line[:-1]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            raise ProfileConfigError(f"line {line_number}: expected NAME=VALUE")
        name, value = line.split("=", 1)
        name = name.strip()
        if name in values:
            raise ProfileConfigError(f"line {line_number}: duplicate variable {name}")
        values[name] = value
    try:
        return validate_env_overrides(values)
    except ValueError as exc:
        raise ProfileConfigError(str(exc)) from exc


def format_environment_text(values: Mapping[str, str]) -> str:
    """Format literal overrides; one environment key per line."""
    return "\n".join(f"{name}={value}" for name, value in sorted(values.items()))


def _absolute_path(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProfileConfigError(f"{field_name} must be a non-empty path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ProfileConfigError(f"{field_name} must be an absolute path (or start with ~)")
    return str(path)


@dataclass(frozen=True)
class LaunchProfile:
    name: str
    prefix: str
    wine_tree: str
    environment: Mapping[str, str]
    profile_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME_RE.fullmatch(self.name.strip()):
            raise ProfileConfigError("profile name must be 1-64 printable characters")
        object.__setattr__(self, "name", self.name.strip())
        profile_id = self.profile_id
        if profile_id is None:
            profile_id = uuid.uuid4().hex
        if not isinstance(profile_id, str) or not _PROFILE_ID_RE.fullmatch(profile_id):
            raise ProfileConfigError("profile id must be a 32-character lowercase hex string")
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "prefix", _absolute_path(self.prefix, "prefix"))
        object.__setattr__(self, "wine_tree", _absolute_path(self.wine_tree, "Wine tree"))
        try:
            overrides = validate_env_overrides(self.environment)
        except (TypeError, ValueError) as exc:
            raise ProfileConfigError(str(exc)) from exc
        object.__setattr__(self, "environment", MappingProxyType(overrides))

    def resolve(self, *, home: str | Path | None = None) -> Environment:
        """Resolve this profile to the same Environment used by WPT's other actions."""
        return detect(
            home=home,
            prefix=self.prefix,
            wine_tree=self.wine_tree,
            env_overrides=self.environment,
            profile_name=self.name,
        )

    def to_json(self) -> dict:
        return {
            "id": self.profile_id,
            "name": self.name,
            "prefix": self.prefix,
            "wine_tree": self.wine_tree,
            "environment": dict(sorted(self.environment.items())),
        }


@dataclass(frozen=True)
class ProfileStore:
    profiles: tuple[LaunchProfile, ...] = ()
    active: str | None = None
    source_digest: str | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        profiles = tuple(self.profiles)
        if any(not isinstance(profile, LaunchProfile) for profile in profiles):
            raise ProfileConfigError("profiles must contain LaunchProfile entries")
        names = [profile.name.casefold() for profile in profiles]
        if len(names) != len(set(names)):
            raise ProfileConfigError("profile names must be unique (case-insensitive)")
        identifiers = [profile.profile_id for profile in profiles]
        if len(identifiers) != len(set(identifiers)):
            raise ProfileConfigError("profile IDs must be unique")
        if self.active is not None:
            if not isinstance(self.active, str) or self.active not in {p.name for p in profiles}:
                raise ProfileConfigError("active profile must name a saved profile or be empty")
        object.__setattr__(self, "profiles", profiles)


def profile_config_path(home: str | Path | None = None) -> Path:
    """Return the dedicated profiles file, respecting the XDG config root."""
    home_path = Path(home).expanduser() if home else Path.home()
    configured = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(configured).expanduser() if configured else home_path / ".config"
    if not base.is_absolute():
        base = home_path / ".config"
    return base / "wpt" / "launch_profiles.json"


def load_store(home: str | Path | None = None) -> ProfileStore:
    path = profile_config_path(home)
    try:
        raw_bytes = path.read_bytes()
        payload = json.loads(raw_bytes.decode("utf-8"))
    except FileNotFoundError:
        return ProfileStore()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProfileConfigError(f"cannot read launch profiles at {path}: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload.get("version") != SCHEMA_VERSION
    ):
        raise ProfileConfigError(f"unsupported launch profile format in {path}")
    raw_profiles = payload.get("profiles")
    if not isinstance(raw_profiles, list):
        raise ProfileConfigError(f"profiles must be a list in {path}")
    profiles = []
    for index, profile_raw in enumerate(raw_profiles):
        if not isinstance(profile_raw, dict):
            raise ProfileConfigError(f"profile {index + 1} must be an object")
        try:
            name = profile_raw.get("name")
            prefix = profile_raw.get("prefix")
            wine_tree = profile_raw.get("wine_tree")
            environment = profile_raw.get("environment", {})
            profile_id = profile_raw.get("id")
            if not isinstance(name, str) or not isinstance(prefix, str) or not isinstance(wine_tree, str):
                raise ProfileConfigError("name, prefix and wine_tree must be strings")
            if not isinstance(environment, Mapping):
                raise ProfileConfigError("environment must be an object")
            if profile_id is not None and not isinstance(profile_id, str):
                raise ProfileConfigError("id must be a string")
            profiles.append(LaunchProfile(
                name=name,
                prefix=prefix,
                wine_tree=wine_tree,
                environment=environment,
                profile_id=profile_id,
            ))
        except (ProfileConfigError, TypeError) as exc:
            raise ProfileConfigError(f"profile {index + 1}: {exc}") from exc
    active = payload.get("active")
    if active == "":
        active = None
    return ProfileStore(
        profiles=tuple(profiles),
        active=active,
        source_digest=hashlib.sha256(raw_bytes).hexdigest(),
    )


def save_store(store: ProfileStore, home: str | Path | None = None) -> ProfileStore:
    """Atomically save only if the file still matches this store's last read."""
    if not isinstance(store, ProfileStore):
        raise ProfileConfigError("expected a ProfileStore")
    path = profile_config_path(home)
    payload = {
        "version": SCHEMA_VERSION,
        "active": store.active or "",
        "profiles": [profile.to_json() for profile in store.profiles],
    }
    temp_path = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = path.with_name(f".{path.name}.lock")
        with lock_path.open("a+b") as lock_stream:
            os.fchmod(lock_stream.fileno(), 0o600)
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
            current = path.read_bytes() if path.exists() else None
            current_digest = hashlib.sha256(current).hexdigest() if current is not None else None
            if current_digest != store.source_digest:
                raise ProfileConfigError(
                    "launch profiles changed since this window loaded them; re-detect and retry"
                )
            encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".launch_profiles-", suffix=".tmp",
                dir=path.parent, delete=False,
            ) as stream:
                temp_path = Path(stream.name)
                os.fchmod(stream.fileno(), 0o600)
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            current = path.read_bytes() if path.exists() else None
            current_digest = hashlib.sha256(current).hexdigest() if current is not None else None
            if current_digest != store.source_digest:
                raise ProfileConfigError(
                    "launch profiles changed while saving; no update was written"
                )
            os.replace(temp_path, path)
            temp_path = None
    except ProfileConfigError:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    except OSError as exc:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise ProfileConfigError(f"cannot save launch profiles at {path}: {exc}") from exc
    return ProfileStore(
        profiles=store.profiles,
        active=store.active,
        source_digest=hashlib.sha256(encoded).hexdigest(),
    )


def resolve_profile(profile: LaunchProfile, *, home: str | Path | None = None) -> Environment:
    """Public resolver used by the GUI and tests."""
    if not isinstance(profile, LaunchProfile):
        raise ProfileConfigError("expected a LaunchProfile")
    try:
        return profile.resolve(home=home)
    except EnvironmentError_:
        raise
