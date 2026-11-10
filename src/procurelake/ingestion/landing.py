"""Immutable object landing for Bronze and manifests.

Writes are create-only: an existing object is never overwritten (ADR 0002).

* ``S3LandingStore`` uses S3 conditional writes (``IfNoneMatch="*"``). S3 rejects the PUT
  with 412 PreconditionFailed if the key already exists. Each PUT also sends
  ``ChecksumAlgorithm=SHA256``, and the SHA-256 that S3 computed is compared with the local one.
* ``LocalLandingStore`` writes under a directory with exclusive-create semantics. It is used
  for local runs without AWS and in tests.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from botocore.exceptions import ClientError


class ObjectAlreadyExistsError(RuntimeError):
    """Raised when a create-only write targets a key that already exists."""


class ChecksumMismatchError(RuntimeError):
    """Raised when the store's checksum disagrees with the locally computed one."""


@dataclass(frozen=True)
class StoredObject:
    uri: str
    key: str
    size_bytes: int
    sha256_hex: str


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LandingStore(Protocol):
    def put_new(self, key: str, data: bytes, content_type: str) -> StoredObject: ...

    def get(self, key: str) -> bytes: ...

    def list_keys(self, prefix: str) -> list[str]: ...


class S3LandingStore:
    def __init__(self, s3_client, bucket: str):
        self._s3 = s3_client
        self.bucket = bucket

    def put_new(self, key: str, data: bytes, content_type: str) -> StoredObject:
        digest = hashlib.sha256(data)
        try:
            response = self._s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
                ChecksumAlgorithm="SHA256",
                ChecksumSHA256=base64.b64encode(digest.digest()).decode(),
                IfNoneMatch="*",
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"PreconditionFailed", "ConditionalRequestConflict"} or status == 412:
                raise ObjectAlreadyExistsError(f"s3://{self.bucket}/{key} already exists") from exc
            raise

        returned = response.get("ChecksumSHA256")
        if returned is not None and base64.b64decode(returned) != digest.digest():
            raise ChecksumMismatchError(f"S3 checksum mismatch for s3://{self.bucket}/{key}")
        return StoredObject(
            uri=f"s3://{self.bucket}/{key}", key=key, size_bytes=len(data), sha256_hex=digest.hexdigest()
        )

    def get(self, key: str) -> bytes:
        return self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
        return sorted(keys)


class LocalLandingStore:
    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"key escapes landing root: {key!r}")
        return path

    def put_new(self, key: str, data: bytes, content_type: str) -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:  # exclusive create: never overwrite
                handle.write(data)
        except FileExistsError as exc:
            raise ObjectAlreadyExistsError(f"{path} already exists") from exc
        return StoredObject(uri=path.as_uri(), key=key, size_bytes=len(data), sha256_hex=sha256_hex(data))

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def list_keys(self, prefix: str) -> list[str]:
        if not self.root.exists():
            return []
        keys = (p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())
        return sorted(k for k in keys if k.startswith(prefix))
