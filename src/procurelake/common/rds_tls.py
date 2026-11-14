"""Verified TLS to the dev/cdc RDS PostgreSQL source (ADR 0050, open question 37).

The operator scripts connect with ``sslmode=verify-full``: libpq checks that the server
certificate chains to a root in ``sslrootcert`` **and** that the host name it connected to is
in the certificate. The root is the AWS RDS CA bundle for ca-central-1, committed at
``configs/tls/rds-ca-central-1-bundle.pem`` and pinned by its SHA-256 below. The file is read
and its checksum checked before every connection, so an edited or replaced bundle fails loudly
instead of silently trusting another CA.

Official download (AWS RDS User Guide, "Using SSL/TLS to encrypt a connection to a DB instance
or cluster", certificate bundles by AWS Region):
``https://truststore.pki.rds.amazonaws.com/ca-central-1/ca-central-1-bundle.pem``. Downloaded
2026-11-14; three roots: Amazon RDS ca-central-1 Root CA RSA2048 G1, RSA4096 G1 and ECC384 G1.
``make rds-ca-check`` downloads it again and compares it with the pin.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from pathlib import Path

import psycopg

DEFAULT_REGION = "ca-central-1"
BUNDLE_URL = "https://truststore.pki.rds.amazonaws.com/ca-central-1/ca-central-1-bundle.pem"
BUNDLE_SHA256 = "7e458e5489c48a92217efbe527da1203dc0fddebd802261fae9eed85fedb1e9f"
BUNDLE_PATH = Path(__file__).resolve().parents[3] / "configs" / "tls" / "rds-ca-central-1-bundle.pem"

# An RDS instance endpoint: <identifier>.<random>.<region>.rds.amazonaws.com (lower case).
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_RDS_HOST = re.compile(rf"^(?:{_LABEL}\.)+(?P<region>[a-z]{{2}}(?:-[a-z]+)+-\d)\.rds\.amazonaws\.com$")


class BundleChecksumError(RuntimeError):
    """The CA bundle on disk is not the pinned one."""


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verified_bundle(path: Path = BUNDLE_PATH, expected_sha256: str = BUNDLE_SHA256) -> Path:
    """Return ``path`` if its SHA-256 is ``expected_sha256``; raise otherwise."""
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise BundleChecksumError(f"RDS CA bundle not found at {path}") from exc
    actual = sha256_of(data)
    if actual != expected_sha256:
        raise BundleChecksumError(
            f"RDS CA bundle {path} has SHA-256 {actual}, expected {expected_sha256}: "
            "do not connect; restore the committed file or re-pin it after checking the AWS source "
            "(make rds-ca-check, ADR 0050)"
        )
    return path


def check_rds_host(host: str, region: str = DEFAULT_REGION) -> str:
    """Return ``host`` if it is an RDS endpoint name in ``region``; raise otherwise.

    verify-full matches the host name against the server certificate, so an IP address (or a
    name the certificate does not carry) would fail the handshake or, worse, invite turning
    verification off. Reject it before connecting.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError(f"connect to the RDS endpoint host name, not an IP address ({host!r})")
    match = _RDS_HOST.match(host)
    if not match:
        raise ValueError(f"{host!r} is not an RDS endpoint (<id>.<suffix>.<region>.rds.amazonaws.com)")
    if match.group("region") != region:
        raise ValueError(f"{host!r} is in {match.group('region')}, but the pinned CA bundle is for {region}")
    return host


def rds_conninfo(
    host: str,
    port: int,
    dbname: str,
    user: str,
    password: str,
    *,
    connect_timeout: int = 15,
    bundle: Path = BUNDLE_PATH,
    expected_sha256: str = BUNDLE_SHA256,
    region: str = DEFAULT_REGION,
) -> str:
    """libpq connection string with ``sslmode=verify-full`` against the pinned RDS CA bundle."""
    if not 1 <= int(port) <= 65535:
        raise ValueError(f"invalid port {port!r}")
    root = verified_bundle(bundle, expected_sha256)
    return psycopg.conninfo.make_conninfo(
        host=check_rds_host(host, region),
        port=int(port),
        dbname=dbname,
        user=user,
        password=password,
        sslmode="verify-full",
        sslrootcert=str(root),
        connect_timeout=connect_timeout,
    )
