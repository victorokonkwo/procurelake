"""Immutable object landing for Bronze and manifests.

Writes are create-only: an existing object is never overwritten (ADR 0002).

* ``S3LandingStore`` uses S3 conditional writes (``IfNoneMatch="*"``). S3 rejects the PUT
  with 412 PreconditionFailed if the key already exists. Each PUT also sends
  ``ChecksumAlgorithm=SHA256``, and the SHA-256 that S3 computed is compared with the local one.
* ``LocalLandingStore`` writes under a directory with exclusive-create semantics. It is used
  for local runs without AWS and in tests.

``put_new_file`` lands a file from disk without reading it into memory (large public CSVs).
On S3, files above ``MULTIPART_THRESHOLD`` go up as a multipart upload:
``CreateMultipartUpload`` with SHA-256, each part sent with its own SHA-256 (S3 validates it and
returns it), and ``CompleteMultipartUpload`` with ``IfNoneMatch="*"``, so the object is still
create-only. A multipart SHA-256 is a checksum of the part checksums, not of the file, so the
whole-file SHA-256 is computed locally and returned in ``StoredObject``. On any failure the
upload is aborted; the buckets' lifecycle rule removes any upload left incomplete after 7 days.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import shutil
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


MIB = 1024 * 1024
MULTIPART_THRESHOLD = 64 * MIB
PART_SIZE = 64 * MIB  # S3 allows 5 MiB to 5 GiB per part and at most 10,000 parts.


def file_sha256_hex(path: Path, chunk: int = 8 * MIB) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk):
            digest.update(block)
    return digest.hexdigest()


class LandingStore(Protocol):
    def put_new(self, key: str, data: bytes, content_type: str) -> StoredObject: ...

    def put_new_file(self, key: str, path: Path, content_type: str) -> StoredObject: ...

    def get(self, key: str) -> bytes: ...

    def list_keys(self, prefix: str) -> list[str]: ...

    def uri_for(self, key: str) -> str: ...


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

    def put_new_file(
        self, key: str, path: Path, content_type: str, part_size: int = PART_SIZE
    ) -> StoredObject:
        size = path.stat().st_size
        if size <= MULTIPART_THRESHOLD:
            return self.put_new(key, path.read_bytes(), content_type)
        if not 5 * MIB <= part_size <= 5 * 1024 * MIB or -(-size // part_size) > 10_000:
            raise ValueError(f"part size {part_size} is outside S3's limits for a {size}-byte object")

        upload_id = self._s3.create_multipart_upload(
            Bucket=self.bucket, Key=key, ContentType=content_type, ChecksumAlgorithm="SHA256"
        )["UploadId"]
        whole = hashlib.sha256()
        parts: list[dict] = []
        try:
            with path.open("rb") as handle:
                number = 1
                while block := handle.read(part_size):
                    whole.update(block)
                    part_digest = base64.b64encode(hashlib.sha256(block).digest()).decode()
                    response = self._s3.upload_part(
                        Bucket=self.bucket, Key=key, UploadId=upload_id, PartNumber=number,
                        Body=block, ChecksumSHA256=part_digest,
                    )  # fmt: skip
                    if response.get("ChecksumSHA256") not in (None, part_digest):
                        raise ChecksumMismatchError(f"S3 checksum mismatch for part {number} of {key}")
                    parts.append(
                        {"PartNumber": number, "ETag": response["ETag"], "ChecksumSHA256": part_digest}
                    )
                    number += 1
            self._s3.complete_multipart_upload(
                Bucket=self.bucket, Key=key, UploadId=upload_id,
                MultipartUpload={"Parts": parts}, IfNoneMatch="*",
            )  # fmt: skip
        except ClientError as exc:
            self._abort(key, upload_id)
            code = exc.response.get("Error", {}).get("Code", "")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"PreconditionFailed", "ConditionalRequestConflict"} or status == 412:
                raise ObjectAlreadyExistsError(f"s3://{self.bucket}/{key} already exists") from exc
            raise
        except BaseException:
            self._abort(key, upload_id)
            raise
        return StoredObject(
            uri=f"s3://{self.bucket}/{key}", key=key, size_bytes=size, sha256_hex=whole.hexdigest()
        )

    def _abort(self, key: str, upload_id: str) -> None:
        # If the abort itself fails, the lifecycle rule removes the upload after 7 days.
        with contextlib.suppress(ClientError):
            self._s3.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)

    def get(self, key: str) -> bytes:
        return self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def uri_for(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

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
        # Plain file:// URI: Path.as_uri() would percent-encode '=' in partition-style
        # directories (ingestion_date=...), which Spark and Hadoop paths do not expect.
        return StoredObject(uri=f"file://{path}", key=key, size_bytes=len(data), sha256_hex=sha256_hex(data))

    def put_new_file(self, key: str, path: Path, content_type: str) -> StoredObject:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("rb") as source, target.open("xb") as handle:
                shutil.copyfileobj(source, handle, 8 * MIB)
        except FileExistsError as exc:
            raise ObjectAlreadyExistsError(f"{target} already exists") from exc
        return StoredObject(
            uri=f"file://{target}",
            key=key,
            size_bytes=target.stat().st_size,
            sha256_hex=file_sha256_hex(target),
        )

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def uri_for(self, key: str) -> str:
        return f"file://{self._path(key)}"

    def list_keys(self, prefix: str) -> list[str]:
        if not self.root.exists():
            return []
        keys = (p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file())
        return sorted(k for k in keys if k.startswith(prefix))
