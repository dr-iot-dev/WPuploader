"""Versioned, immutable file transport. Standard library only."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid5

VERSION = "1.0"
NAMESPACE = UUID("c2e54cd2-1813-5e04-8e37-fd1179071529")
MAX_ZIP = 25 * 1024 * 1024
MAX_TOTAL = 40 * 1024 * 1024
MAX_METADATA = 256 * 1024
MAX_ENTRIES = 64


class ProtocolError(ValueError):
    """A bounded, public error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def object_hash(value: Any) -> str:
    return digest(canonical(value))


def load_json(data: bytes) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise ProtocolError("JSON_DUPLICATE_KEY")
            result[key] = value
        return result

    def bad_constant(_: str) -> None:
        raise ProtocolError("JSON_NONFINITE_NUMBER")

    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=bad_constant)
        if not isinstance(value, dict):
            raise ProtocolError("JSON_OBJECT_REQUIRED")

        def finite(item: Any) -> None:
            if isinstance(item, float) and not math.isfinite(item):
                raise ProtocolError("JSON_NONFINITE_NUMBER")
            if isinstance(item, dict):
                for child in item.values():
                    finite(child)
            elif isinstance(item, list):
                for child in item:
                    finite(child)

        finite(value)
        return value
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("JSON_INVALID") from exc


def relative_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ProtocolError("BUNDLE_PATH_INVALID")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in name.split("/")):
        raise ProtocolError("BUNDLE_PATH_INVALID")
    return name


def normalize_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or any(ord(c) <= 32 for c in value)
    ):
        raise ProtocolError("SOURCE_URL_INVALID")
    host = parsed.hostname.lower().encode("idna").decode()
    port = parsed.port
    authority = host if port in (None, 443) else f"{host}:{port}"
    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "fbclid"}
    ]
    return urlunsplit((parsed.scheme.lower(), authority, parsed.path or "/", urlencode(query), ""))


def source_hash(urls: list[str]) -> str:
    return digest(
        json.dumps(
            sorted({normalize_url(url) for url in urls}), ensure_ascii=False, separators=(",", ":")
        ).encode()
    )


def package_identity(meta: dict[str, Any]) -> str:
    return str(
        uuid5(
            NAMESPACE,
            canonical(
                [
                    str(UUID(meta["producer_id"])),
                    str(UUID(meta["article_id"])),
                    meta["revision"],
                    meta["artifact_revision"],
                ]
            ).decode(),
        )
    )


def bundle_bytes(meta: dict[str, Any], files: dict[str, bytes], *, draft: bool = False) -> bytes:
    manifest_name = "package.json" if draft else "manifest.json"
    if manifest_name in files or not files:
        raise ProtocolError("BUNDLE_FILES_INVALID")
    if len(files) >= MAX_ENTRIES or sum(map(len, files.values())) > MAX_TOTAL:
        raise ProtocolError("BUNDLE_TOO_LARGE")
    manifest = dict(meta)
    manifest["schema_version"] = VERSION
    manifest["files"] = {
        relative_name(name): {"size": len(data), "sha256": digest(data)}
        for name, data in sorted(files.items())
    }
    manifest.pop("manifest_hash", None)
    manifest["manifest_hash"] = object_hash(manifest)
    metadata = canonical(manifest)
    if len(metadata) > MAX_METADATA:
        raise ProtocolError("METADATA_TOO_LARGE")
    from io import BytesIO

    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted({manifest_name: metadata, **files}.items()):
            entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_STORED
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            entry.create_system = 3
            archive.writestr(entry, data)
    result = output.getvalue()
    if len(result) > MAX_ZIP:
        raise ProtocolError("BUNDLE_TOO_LARGE")
    return result


def read_bundle(
    source: bytes | Path, *, draft: bool = False
) -> tuple[dict[str, Any], dict[str, bytes]]:
    from io import BytesIO

    if isinstance(source, Path):
        if source.stat().st_size > MAX_ZIP:
            raise ProtocolError("BUNDLE_TOO_LARGE")
        data = source.read_bytes()
    else:
        data = source
    if len(data) > MAX_ZIP:
        raise ProtocolError("BUNDLE_TOO_LARGE")
    manifest_name = "package.json" if draft else "manifest.json"
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            names = [relative_name(entry.filename) for entry in entries]
            if len(names) != len(set(names)) or len(names) > MAX_ENTRIES:
                raise ProtocolError("BUNDLE_DUPLICATE_OR_TOO_MANY_FILES")
            total = 0
            for entry in entries:
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if entry.is_dir() or kind not in {0, stat.S_IFREG} or entry.flag_bits & 1:
                    raise ProtocolError("BUNDLE_ENTRY_INVALID")
                total += entry.file_size
                if total > MAX_TOTAL or entry.file_size > 100 * max(entry.compress_size, 1):
                    raise ProtocolError("BUNDLE_TOO_LARGE")
            if manifest_name not in names:
                raise ProtocolError("BUNDLE_MANIFEST_REQUIRED")
            if archive.getinfo(manifest_name).file_size > MAX_METADATA:
                raise ProtocolError("METADATA_TOO_LARGE")
            manifest = load_json(archive.read(manifest_name))
            if manifest.get("schema_version") != VERSION:
                raise ProtocolError("SCHEMA_UNSUPPORTED")
            expected = manifest.get("manifest_hash")
            actual = object_hash({k: v for k, v in manifest.items() if k != "manifest_hash"})
            if expected != actual:
                raise ProtocolError("MANIFEST_HASH_MISMATCH")
            listed = manifest.get("files")
            if not isinstance(listed, dict) or set(listed) != set(names) - {manifest_name}:
                raise ProtocolError("BUNDLE_FILE_MANIFEST_MISMATCH")
            files: dict[str, bytes] = {}
            for name, record in listed.items():
                value = archive.read(name)
                if (
                    not isinstance(record, dict)
                    or type(record.get("size")) is not int
                    or record["size"] != len(value)
                    or record.get("sha256") != digest(value)
                ):
                    raise ProtocolError("BUNDLE_FILE_HASH_MISMATCH")
                files[name] = value
            return manifest, files
    except (zipfile.BadZipFile, RuntimeError, KeyError, OSError) as exc:
        raise ProtocolError("BUNDLE_INVALID") from exc


def atomic_write(path: Path, data: bytes, *, immutable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if immutable and path.exists():
        if path.read_bytes() != data:
            raise ProtocolError("IMMUTABLE_FILE_CONFLICT")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".part-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        # Content-addressed publications may race, but all winners must have identical bytes.
        if immutable and path.exists():
            if path.read_bytes() != data:
                raise ProtocolError("IMMUTABLE_FILE_CONFLICT")
        else:
            os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def process_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ProtocolError("WORKER_BUSY") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def require_local(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if os.name == "nt":
        import ctypes

        if (
            str(resolved).startswith("\\\\")
            or ctypes.windll.kernel32.GetDriveTypeW(resolved.anchor) == 4
        ):
            raise ProtocolError("STATE_DIRECTORY_MUST_BE_LOCAL")
    elif Path("/proc/mounts").exists():
        mounts = []
        for line in Path("/proc/mounts").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3:
                mount = Path(parts[1].replace("\\040", " "))
                if resolved == mount or mount in resolved.parents:
                    mounts.append((len(str(mount)), parts[2]))
        if mounts and max(mounts)[1] in {"cifs", "smb3", "nfs", "nfs4", "fuse.sshfs"}:
            raise ProtocolError("STATE_DIRECTORY_MUST_BE_LOCAL")
    return resolved


def read_stage(
    path: bytes | Path, expected: str | None = None
) -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes]]:
    meta, files = read_bundle(path)
    if expected and meta.get("contract_type") != expected:
        raise ProtocolError("STAGE_CONTRACT_MISMATCH")
    payload = load_json(files.get("payload.json", b""))
    return meta, payload, {k: v for k, v in files.items() if k != "payload.json"}


def write_stage(
    path: Path,
    kind: str,
    payload: dict[str, Any],
    assets: dict[str, bytes] | None = None,
    provenance: dict[str, Any] | None = None,
) -> str:
    data = bundle_bytes(
        {"contract_type": kind, "provenance": provenance or {}},
        {"payload.json": canonical(payload), **(assets or {})},
    )
    atomic_write(path, data, immutable=True)
    return digest(data)
