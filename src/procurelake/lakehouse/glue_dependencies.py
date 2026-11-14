"""Build the Glue jobs' dependency bundle: a reproducible ``.gluewheels.zip`` (ADR 0048).

The Silver Glue jobs install the procurelake wheel plus the exact pins in
``jobs/glue/silver_python_modules.txt``. This module turns those pins into one zip of wheels
that AWS Glue 5.0 and later install with ``--python-modules-installer-option --no-index``, so a
job run never contacts PyPI (AWS Glue docs, "Using Python libraries with AWS Glue", "Installing
additional Python libraries in AWS Glue 5.0 or above using Zip of Wheels").

Inputs and target:

- the wheels come from ``uv.lock``: each pin must be locked at that exact version, and the wheel
  for the Glue 5.1 platform is chosen from the lock's wheel list and checked against the lock's
  SHA-256;
- Glue 5.1 runs Python 3.11 on Amazon Linux 2023 (glibc 2.34), x86_64, and lists the
  compatible platform tags ``manylinux_2_34_x86_64``, ``manylinux_2_28_x86_64`` and
  ``manylinux2014_x86_64`` (same docs, "Appendix B: AWS Glue environment details"). A manylinux
  tag is compatible when its glibc version is at most the runtime's (PEP 600), so
  ``manylinux_2_17_x86_64`` (the PEP 600 name of ``manylinux2014``) is accepted too.

The layout follows the docs' example (Appendix A): a ``wheels/`` directory holding the wheels
and a ``requirements.txt`` with the exact pins. The zip is reproducible: the same lock and pins
give the same bytes. Entries are sorted, stored uncompressed (wheels are already zip files, and
stored entries do not depend on the zlib version), with fixed timestamps and permissions.

Standard library only, so `make glue-wheel` runs it without syncing the project environment
(CI): ``uv run --no-project python src/procurelake/lakehouse/glue_dependencies.py``
``--out dist/<name>.gluewheels.zip``
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tomllib
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

BUNDLE_SUFFIX = ".gluewheels.zip"
BUNDLE_DIR = "wheels"

# AWS Glue 5.1 (AWS Glue docs, Appendix B): Python 3.11, AL2023 with glibc 2.34, x86_64.
GLUE_PYTHON = (3, 11)
GLUE_GLIBC = (2, 34)
GLUE_ARCH = "x86_64"

# Legacy manylinux names and the glibc version each stands for (PEP 600).
_LEGACY_MANYLINUX = {"manylinux1": (2, 5), "manylinux2010": (2, 12), "manylinux2014": (2, 17)}
_MANYLINUX = re.compile(r"^manylinux_(\d+)_(\d+)_(\w+)$")
_ALLOWED_URL = "https://files.pythonhosted.org/"
_FIXED_DATE = (1980, 1, 1, 0, 0, 0)  # earliest date a zip entry can hold


class BundleError(Exception):
    """The bundle cannot be built from the given lock and pins."""


@dataclass(frozen=True)
class LockedWheel:
    name: str
    version: str
    filename: str
    url: str
    sha256: str


def normalize(name: str) -> str:
    """PEP 503 normalized project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def read_pins(path: Path) -> dict[str, str]:
    """``name==version`` lines (comments and blank lines skipped), keyed by normalized name."""
    pins: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, version = line.partition("==")
        if not sep or not name or not version or any(c in version for c in "<>=!~*,; "):
            raise BundleError(f"not an exact pin: {line!r}")
        key = normalize(name)
        if key in pins:
            raise BundleError(f"pinned twice: {name}")
        pins[key] = version
    return pins


def _manylinux_glibc(platform: str) -> tuple[int, int] | None:
    """The glibc version of an x86_64 manylinux tag, or None if it is not one."""
    for legacy, glibc in _LEGACY_MANYLINUX.items():
        if platform == f"{legacy}_{GLUE_ARCH}":
            return glibc
    match = _MANYLINUX.match(platform)
    if match and match.group(3) == GLUE_ARCH:
        return int(match.group(1)), int(match.group(2))
    return None


def _tag_rank(python: str, abi: str, platform: str) -> tuple[int, int, int] | None:
    """Preference of one (python, abi, platform) tag on Glue 5.1, or None if incompatible.

    Higher is better: an interpreter-specific build over the stable ABI over pure Python, then
    the newest compatible glibc.
    """
    cp = f"cp{GLUE_PYTHON[0]}{GLUE_PYTHON[1]}"
    if platform == "any":
        plat_rank: tuple[int, int] = (0, 0)
    else:
        glibc = _manylinux_glibc(platform)
        if glibc is None or glibc > GLUE_GLIBC:
            return None
        plat_rank = glibc
    if abi == cp and python == cp:
        abi_rank = 2
    elif abi == "abi3" and re.fullmatch(r"cp3\d+", python) and int(python[3:]) <= GLUE_PYTHON[1]:
        abi_rank = 1
    elif abi == "none" and python in {"py3", f"py{GLUE_PYTHON[0]}{GLUE_PYTHON[1]}", cp}:
        abi_rank = 0
    else:
        return None
    return abi_rank, *plat_rank


def wheel_rank(filename: str) -> tuple[int, int, int] | None:
    """Best compatible tag in a wheel filename (compressed tag sets allowed), or None."""
    if not filename.endswith(".whl"):
        return None
    parts = filename[: -len(".whl")].split("-")
    if len(parts) not in (5, 6):
        return None
    pythons, abis, platforms = (p.split(".") for p in parts[-3:])
    ranks = [
        r
        for py in pythons
        for abi in abis
        for plat in platforms
        if (r := _tag_rank(py, abi, plat)) is not None
    ]
    return max(ranks) if ranks else None


def select_wheels(lock: dict, pins: dict[str, str]) -> list[LockedWheel]:
    """The Glue 5.1 wheel of every pin, from ``uv.lock``; sorted by name."""
    selected = []
    for name, version in sorted(pins.items()):
        entries = [
            p for p in lock.get("package", []) if normalize(p["name"]) == name and p["version"] == version
        ]
        if len(entries) != 1:
            raise BundleError(f"{name}=={version} is locked {len(entries)} times in uv.lock (need 1)")
        candidates = []
        for wheel in entries[0].get("wheels", []):
            filename = wheel["url"].rsplit("/", 1)[-1]
            rank = wheel_rank(filename)
            if rank is not None:
                candidates.append((rank, filename, wheel))
        if not candidates:
            raise BundleError(f"no wheel of {name}=={version} in uv.lock runs on Glue 5.1")
        candidates.sort(key=lambda c: (c[0], c[1]))
        _, filename, wheel = candidates[-1]
        algorithm, _, digest = wheel["hash"].partition(":")
        if algorithm != "sha256" or not digest:
            raise BundleError(f"{filename}: uv.lock hash is not sha256")
        if not wheel["url"].startswith(_ALLOWED_URL):
            raise BundleError(f"{filename}: unexpected wheel host {wheel['url']}")
        selected.append(LockedWheel(name, version, filename, wheel["url"], digest))
    return selected


def fetch(wheel: LockedWheel, cache_dir: Path) -> bytes:
    """Wheel bytes from the cache or PyPI's file host, verified against the lock's SHA-256."""
    cached = cache_dir / wheel.sha256 / wheel.filename
    if cached.exists():
        data = cached.read_bytes()
    else:
        with urllib.request.urlopen(wheel.url, timeout=60) as response:  # noqa: S310 - https URL checked in select_wheels
            data = response.read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != wheel.sha256:
        raise BundleError(f"{wheel.filename}: sha256 {actual} does not match uv.lock {wheel.sha256}")
    if not cached.exists():
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(data)
    return data


def requirements_text(wheels: list[LockedWheel]) -> str:
    return "".join(f"{w.name}=={w.version}\n" for w in sorted(wheels, key=lambda w: w.name))


def write_bundle(out: Path, files: dict[str, bytes]) -> str:
    """Write ``files`` (name -> bytes) under ``wheels/`` reproducibly; return the zip's SHA-256."""
    if not out.name.endswith(BUNDLE_SUFFIX):
        raise BundleError(f"the bundle name must end with {BUNDLE_SUFFIX} (AWS Glue docs)")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as bundle:
        for name in sorted(files):
            if "/" in name or name.startswith("."):
                raise BundleError(f"bad bundle entry name: {name!r}")
            info = zipfile.ZipInfo(f"{BUNDLE_DIR}/{name}", date_time=_FIXED_DATE)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3  # Unix, so the permissions below are read as such
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, files[name])
    tmp.replace(out)
    return hashlib.sha256(out.read_bytes()).hexdigest()


def build_bundle(lock_path: Path, pins_path: Path, out: Path, cache_dir: Path) -> str:
    pins = read_pins(pins_path)
    if not pins:
        raise BundleError(f"no pins in {pins_path}")
    wheels = select_wheels(tomllib.loads(lock_path.read_text()), pins)
    files = {w.filename: fetch(w, cache_dir) for w in wheels}
    files["requirements.txt"] = requirements_text(wheels).encode()
    return write_bundle(out, files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lock", type=Path, default=Path("uv.lock"))
    parser.add_argument("--pins", type=Path, default=Path("jobs/glue/silver_python_modules.txt"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=Path("build/glue_wheels_cache"))
    args = parser.parse_args(argv)
    try:
        digest = build_bundle(args.lock, args.pins, args.out, args.cache_dir)
    except BundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{args.out} sha256={digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
