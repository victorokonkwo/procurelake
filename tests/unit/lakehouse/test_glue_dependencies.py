"""The Glue dependency bundle (ADR 0048): wheel selection from uv.lock, and a reproducible zip.

No network: selection runs on the real uv.lock; downloads are exercised through the cache with
in-test bytes.
"""

from __future__ import annotations

import hashlib
import tomllib
import zipfile
from pathlib import Path

import pytest

from procurelake.lakehouse.glue_dependencies import (
    BundleError,
    LockedWheel,
    build_bundle,
    fetch,
    read_pins,
    requirements_text,
    select_wheels,
    wheel_rank,
    write_bundle,
)

REPO = Path(__file__).resolve().parents[3]
PINS = REPO / "jobs" / "glue" / "silver_python_modules.txt"
LOCK = tomllib.loads((REPO / "uv.lock").read_text())


@pytest.mark.parametrize(
    "filename",
    [
        "pydantic-2.14.3-py3-none-any.whl",
        "pydantic_core-2.50.4-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl",
        "pyyaml-6.0.3-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl",
        "x-1.0-cp311-cp311-manylinux_2_34_x86_64.whl",
        "x-1.0-cp39-abi3-manylinux_2_28_x86_64.whl",
        "x-1.0-1-py3-none-any.whl",  # build tag
    ],
)
def test_wheels_for_glue_51_are_accepted(filename):
    assert wheel_rank(filename) is not None


@pytest.mark.parametrize(
    "filename",
    [
        "x-1.0-cp311-cp311-manylinux2014_aarch64.manylinux_2_17_aarch64.whl",  # arm64
        "x-1.0-cp311-cp311-musllinux_1_1_x86_64.whl",  # not glibc
        "x-1.0-cp311-cp311-manylinux_2_35_x86_64.whl",  # newer than AL2023's glibc 2.34
        "x-1.0-cp311-cp311-linux_x86_64.whl",  # not a portable tag
        "x-1.0-cp312-cp312-manylinux2014_x86_64.whl",  # Python 3.12
        "x-1.0-cp312-abi3-manylinux2014_x86_64.whl",  # stable ABI newer than 3.11
        "x-1.0-cp311-cp311-macosx_11_0_arm64.whl",
        "x-1.0-cp311-cp311-win_amd64.whl",
        "x-1.0-py2-none-any.whl",
        "x-1.0.tar.gz",
    ],
)
def test_other_platforms_are_rejected(filename):
    assert wheel_rank(filename) is None


def test_interpreter_specific_wheel_beats_stable_abi_and_pure_python():
    specific = wheel_rank("x-1.0-cp311-cp311-manylinux2014_x86_64.whl")
    abi3 = wheel_rank("x-1.0-cp38-abi3-manylinux_2_28_x86_64.whl")
    pure = wheel_rank("x-1.0-py3-none-any.whl")
    assert specific > abi3 > pure


def test_every_pin_resolves_to_one_glue_wheel_from_uv_lock():
    pins = read_pins(PINS)
    wheels = select_wheels(LOCK, pins)
    assert {(w.name, w.version) for w in wheels} == set(pins.items())
    by_name = {w.name: w for w in wheels}
    # The two native packages get CPython 3.11 x86_64 glibc builds, not another platform's.
    for native in ("pydantic-core", "pyyaml"):
        assert "-cp311-cp311-" in by_name[native].filename
        assert "manylinux2014_x86_64" in by_name[native].filename
    for wheel in wheels:
        assert wheel.url.startswith("https://files.pythonhosted.org/")
        assert len(wheel.sha256) == 64


def test_a_pin_not_in_uv_lock_is_refused():
    with pytest.raises(BundleError, match="locked 0 times"):
        select_wheels(LOCK, {"pydantic": "0.0.1"})


def test_a_package_without_a_glue_wheel_is_refused():
    lock = {
        "package": [
            {
                "name": "only-mac",
                "version": "1.0",
                "wheels": [
                    {
                        "url": "https://files.pythonhosted.org/p/only_mac-1.0-cp311-cp311-macosx_11_0_arm64.whl",
                        "hash": "sha256:" + "0" * 64,
                    }
                ],
            }
        ]
    }
    with pytest.raises(BundleError, match=r"runs on Glue 5\.1"):
        select_wheels(lock, {"only-mac": "1.0"})


@pytest.mark.parametrize(
    "line", ["pydantic>=2", "pydantic", "pydantic==2.*", "pydantic==2; python_version>'3'"]
)
def test_only_exact_pins_are_read(tmp_path, line):
    pins = tmp_path / "pins.txt"
    pins.write_text(f"# comment\n\n{line}\n")
    with pytest.raises(BundleError):
        read_pins(pins)


def test_pins_are_normalized(tmp_path):
    pins = tmp_path / "pins.txt"
    pins.write_text("Typing_Extensions==4.16.0\n")
    assert read_pins(pins) == {"typing-extensions": "4.16.0"}


def _bundle(tmp_path: Path, name: str, files: dict[str, bytes]) -> tuple[str, bytes]:
    out = tmp_path / name / "deps.gluewheels.zip"
    digest = write_bundle(out, files)
    return digest, out.read_bytes()


def test_bundle_is_reproducible_and_independent_of_input_order(tmp_path):
    files = {
        "b-1.0-py3-none-any.whl": b"b" * 100,
        "a-1.0-py3-none-any.whl": b"a" * 50,
        "requirements.txt": b"x",
    }
    first = _bundle(tmp_path, "one", files)
    second = _bundle(tmp_path, "two", dict(reversed(list(files.items()))))
    assert first == second
    assert first[0] == hashlib.sha256(first[1]).hexdigest()


def test_bundle_layout_follows_the_glue_docs_example(tmp_path):
    out = tmp_path / "deps.gluewheels.zip"
    write_bundle(out, {"a-1.0-py3-none-any.whl": b"a", "requirements.txt": b"a==1.0\n"})
    with zipfile.ZipFile(out) as bundle:
        infos = bundle.infolist()
        assert [i.filename for i in infos] == ["wheels/a-1.0-py3-none-any.whl", "wheels/requirements.txt"]
        assert {i.compress_type for i in infos} == {zipfile.ZIP_STORED}
        assert {i.date_time for i in infos} == {(1980, 1, 1, 0, 0, 0)}
        assert {i.external_attr >> 16 for i in infos} == {0o100644}
        assert bundle.read("wheels/requirements.txt") == b"a==1.0\n"


def test_bundle_name_must_carry_the_gluewheels_suffix(tmp_path):
    with pytest.raises(BundleError, match=r"\.gluewheels\.zip"):
        write_bundle(tmp_path / "deps.zip", {"requirements.txt": b""})


def _wheel(data: bytes, sha256: str | None = None) -> LockedWheel:
    return LockedWheel(
        name="a",
        version="1.0",
        filename="a-1.0-py3-none-any.whl",
        url="https://files.pythonhosted.org/packages/a-1.0-py3-none-any.whl",
        sha256=sha256 or hashlib.sha256(data).hexdigest(),
    )


def test_cached_wheel_is_used_when_its_hash_matches(tmp_path):
    wheel = _wheel(b"wheel bytes")
    cached = tmp_path / wheel.sha256 / wheel.filename
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"wheel bytes")
    assert fetch(wheel, tmp_path) == b"wheel bytes"


def test_a_wheel_that_does_not_match_uv_lock_is_refused(tmp_path):
    wheel = _wheel(b"x", sha256="0" * 64)
    cached = tmp_path / wheel.sha256 / wheel.filename
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"tampered")
    with pytest.raises(BundleError, match=r"does not match uv\.lock"):
        fetch(wheel, tmp_path)


def test_build_from_the_real_lock_is_reproducible_offline(tmp_path):
    # Fill the cache with stand-in bytes and a lock whose hashes match them, so the whole
    # build (selection, verification, requirements, zip) runs twice without the network.
    pins = read_pins(PINS)
    real = {w.name: w for w in select_wheels(LOCK, pins)}
    cache = tmp_path / "cache"
    lock = {"package": []}
    for name, wheel in real.items():
        data = f"stand-in for {wheel.filename}".encode()
        digest = hashlib.sha256(data).hexdigest()
        (cache / digest).mkdir(parents=True)
        (cache / digest / wheel.filename).write_bytes(data)
        lock["package"].append(
            {
                "name": name,
                "version": wheel.version,
                "wheels": [{"url": wheel.url, "hash": f"sha256:{digest}"}],
            }
        )
    lock_path = tmp_path / "uv.lock"
    lock_path.write_text(_toml(lock))
    first = build_bundle(lock_path, PINS, tmp_path / "1" / "d.gluewheels.zip", cache)
    second = build_bundle(lock_path, PINS, tmp_path / "2" / "d.gluewheels.zip", cache)
    assert first == second
    with zipfile.ZipFile(tmp_path / "1" / "d.gluewheels.zip") as bundle:
        names = bundle.namelist()
        assert sorted(f"wheels/{w.filename}" for w in real.values()) == sorted(
            n for n in names if n.endswith(".whl")
        )
        assert bundle.read("wheels/requirements.txt").decode() == requirements_text(list(real.values()))


def test_builder_uses_only_the_standard_library():
    # make glue-wheel runs it with `uv run --no-project` (no project environment in CI).
    import ast
    import sys

    source = (REPO / "src" / "procurelake" / "lakehouse" / "glue_dependencies.py").read_text()
    modules = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert modules <= set(sys.stdlib_module_names) | {"__future__"}


def _toml(lock: dict) -> str:
    lines = []
    for p in lock["package"]:
        lines += ["[[package]]", f'name = "{p["name"]}"', f'version = "{p["version"]}"', "wheels = ["]
        lines += [f'  {{ url = "{w["url"]}", hash = "{w["hash"]}" }},' for w in p["wheels"]]
        lines += ["]", ""]
    return "\n".join(lines)
