"""Verified TLS connection strings for the dev/cdc RDS source (ADR 0050; no network, no AWS)."""

from __future__ import annotations

import hashlib

import psycopg
import pytest

from procurelake.common import rds_tls

HOST = "procurelake-dev-cdc-source.abcdefghijkl.ca-central-1.rds.amazonaws.com"


def test_committed_bundle_matches_the_pin_and_holds_the_three_ca_central_1_roots():
    data = rds_tls.verified_bundle().read_bytes()
    assert hashlib.sha256(data).hexdigest() == rds_tls.BUNDLE_SHA256
    text = data.decode()
    assert text.count("-----BEGIN CERTIFICATE-----") == 3
    assert "PRIVATE KEY" not in text
    assert (
        rds_tls.BUNDLE_URL == "https://truststore.pki.rds.amazonaws.com/ca-central-1/ca-central-1-bundle.pem"
    )


def test_conninfo_verifies_the_server_certificate_and_host_name():
    params = psycopg.conninfo.conninfo_to_dict(
        rds_tls.rds_conninfo(HOST, 5432, "procurelake", "admin", "s3cret")
    )
    assert params["sslmode"] == "verify-full"
    assert params["sslrootcert"] == str(rds_tls.BUNDLE_PATH)
    assert params["host"] == HOST and params["port"] == "5432" and params["dbname"] == "procurelake"
    assert params["user"] == "admin" and params["password"] == "s3cret"
    assert params["connect_timeout"] == "15"


def test_password_with_libpq_special_characters_is_quoted_not_injected():
    dsn = rds_tls.rds_conninfo(HOST, 5432, "procurelake", "admin", "a b' sslmode=disable")
    params = psycopg.conninfo.conninfo_to_dict(dsn)
    assert params["password"] == "a b' sslmode=disable" and params["sslmode"] == "verify-full"


@pytest.mark.parametrize(
    "host",
    [
        "15.222.1.2",  # verify-full matches the host name, not an address
        "::1",
        "localhost",
        "example.com",
        "db.abcdefghijkl.us-east-1.rds.amazonaws.com",  # the pinned bundle is ca-central-1's
        "db.abcdefghijkl.ca-central-1.rds.amazonaws.com.evil.example",
        "DB.ABCDEFGHIJKL.CA-CENTRAL-1.RDS.AMAZONAWS.COM",
        "db..ca-central-1.rds.amazonaws.com",
        "",
    ],
)
def test_only_rds_endpoint_names_in_ca_central_1_are_accepted(host):
    with pytest.raises(ValueError):
        rds_tls.rds_conninfo(host, 5432, "procurelake", "admin", "pw")


@pytest.mark.parametrize("port", [0, 65536, -1])
def test_invalid_port_is_refused(port):
    with pytest.raises(ValueError):
        rds_tls.rds_conninfo(HOST, port, "procurelake", "admin", "pw")


def test_a_changed_bundle_is_refused(tmp_path):
    forged = tmp_path / "bundle.pem"
    forged.write_bytes(rds_tls.BUNDLE_PATH.read_bytes() + b"\n")
    with pytest.raises(rds_tls.BundleChecksumError, match="expected"):
        rds_tls.rds_conninfo(HOST, 5432, "procurelake", "admin", "pw", bundle=forged)


def test_a_missing_bundle_is_refused(tmp_path):
    with pytest.raises(rds_tls.BundleChecksumError, match="not found"):
        rds_tls.verified_bundle(tmp_path / "absent.pem")


def test_a_bundle_matching_another_pin_is_accepted(tmp_path):
    other = tmp_path / "other.pem"
    other.write_bytes(b"not a real bundle")
    pin = hashlib.sha256(b"not a real bundle").hexdigest()
    assert rds_tls.verified_bundle(other, pin) == other
