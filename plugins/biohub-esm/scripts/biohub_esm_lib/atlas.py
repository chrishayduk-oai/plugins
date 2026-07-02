"""Public ESM Atlas v1alpha1 client with schema-drift checks."""

from __future__ import annotations

import binascii
import io
import math
import re
import struct
import time
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from .constants import (
    ATLAS_API_PREFIX,
    BIOHUB_BASE_URL,
    DEFAULT_POLL_TIMEOUT_SECONDS,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
)
from .errors import APIError, SchemaDriftError, ValidationError
from .http import (
    HTTPResponse,
    Transport,
    UrllibTransport,
    decode_json,
    encode_json_body,
    raise_for_status,
    validate_content_range,
    validate_timeout,
)
from .provenance import sha256_file
from .validation import (
    sequence_md5,
    validate_atlas_fold_sequence,
    validate_atlas_search_sequence,
    validate_batch_hashes,
    validate_feature_index,
    validate_md5,
)

JOB_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
DOWNLOAD_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
    re.IGNORECASE,
)


def validate_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise ValidationError(
            "job_id must be 1-128 path-safe characters beginning and ending alphanumeric"
        )
    return job_id


def safe_download_endpoint(url: str) -> str:
    """Validate an ephemeral Atlas download URL and omit its signed query."""

    if not isinstance(url, str) or any(ord(character) < 32 for character in url):
        raise ValidationError("download URL must be a valid HTTPS URL")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValidationError("download URL must be a valid HTTPS URL") from exc
    hostname = parts.hostname
    if (
        parts.scheme != "https"
        or hostname is None
        or not DOWNLOAD_HOST_RE.fullmatch(hostname)
        or parts.username is not None
        or parts.password is not None
        or (port is not None and port != 443)
        or not parts.path.startswith("/")
        or parts.fragment
    ):
        raise ValidationError(
            "download URL requires HTTPS, a public DNS host, no userinfo/fragment, and port 443"
        )
    return f"https://{hostname.lower()}{parts.path}"


def _require_object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaDriftError(
            f"Atlas {context} response must be a JSON object", raw=value
        )
    return value


def _require_keys(value: dict[str, Any], context: str, keys: set[str]) -> None:
    missing = sorted(keys - set(value))
    if missing:
        raise SchemaDriftError(
            f"Atlas {context} response is missing: {', '.join(missing)}", raw=value
        )


def _integer_in_range(name: str, value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValidationError(f"{name} must be an integer between {low} and {high}")
    return value


def _boolean(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValidationError(f"{name} must be boolean")
    return value


def _validate_zip(value: bytes | Path, context: str) -> None:
    source: io.BytesIO | Path = io.BytesIO(value) if isinstance(value, bytes) else value
    try:
        with zipfile.ZipFile(source) as archive:
            bad_member = archive.testzip()
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise SchemaDriftError(f"{context} is not a valid zip archive") from exc
    if bad_member is not None:
        raise SchemaDriftError(f"{context} has a corrupt zip member: {bad_member}")


def _validate_png(value: bytes, context: str) -> None:
    diagnostic = {"size_bytes": len(value), "prefix_hex": value[:16].hex()}
    if not value.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SchemaDriftError(f"{context} is not a PNG", raw=diagnostic)
    offset = 8
    chunks: list[tuple[bytes, bytes]] = []
    while offset < len(value):
        if len(value) - offset < 12:
            raise SchemaDriftError(f"{context} PNG is truncated", raw=diagnostic)
        length = struct.unpack(">I", value[offset : offset + 4])[0]
        chunk_end = offset + 12 + length
        if chunk_end > len(value):
            raise SchemaDriftError(f"{context} PNG is truncated", raw=diagnostic)
        kind = value[offset + 4 : offset + 8]
        payload = value[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", value[offset + 8 + length : chunk_end])[0]
        if binascii.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            raise SchemaDriftError(f"{context} PNG checksum is invalid", raw=diagnostic)
        chunks.append((kind, payload))
        offset = chunk_end
        if kind == b"IEND":
            break
    if offset != len(value) or not chunks or chunks[0][0] != b"IHDR":
        raise SchemaDriftError(f"{context} PNG structure is invalid", raw=diagnostic)
    ihdr = chunks[0][1]
    if len(ihdr) != 13:
        raise SchemaDriftError(f"{context} PNG IHDR is invalid", raw=diagnostic)
    width, height, _, _, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", ihdr
    )
    if (
        width <= 0
        or height <= 0
        or compression != 0
        or filtering != 0
        or interlace not in {0, 1}
        or not any(kind == b"IDAT" and payload for kind, payload in chunks)
        or chunks[-1] != (b"IEND", b"")
    ):
        raise SchemaDriftError(f"{context} PNG structure is invalid", raw=diagnostic)


class AtlasClient:
    def __init__(
        self,
        *,
        base_url: str = BIOHUB_BASE_URL,
        transport: Transport | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if self.base_url != BIOHUB_BASE_URL:
            raise ValidationError(
                "Atlas biological inputs can only be sent to the canonical Biohub host"
            )
        self.transport = transport or UrllibTransport()
        self.timeout = validate_timeout(timeout, "Atlas request timeout")

    def _url(self, path: str, query: dict[str, Any] | None = None) -> str:
        url = f"{self.base_url}{ATLAS_API_PREFIX}{path}"
        if query:
            values: list[tuple[str, str]] = []
            for key, item in query.items():
                if item is None:
                    continue
                if isinstance(item, bool):
                    values.append((key, "true" if item else "false"))
                elif isinstance(item, list):
                    values.extend((key, str(element)) for element in item)
                else:
                    values.append((key, str(item)))
            url += "?" + urlencode(values)
        return url

    def _json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        expected: set[int] = {200},
    ) -> tuple[int, Any, dict[str, str]]:
        body = None if payload is None else encode_json_body(payload)
        headers = {"accept": "application/json"}
        if body is not None:
            headers["content-type"] = "application/json"
        response = self.transport.request(
            method,
            self._url(path, query),
            headers=headers,
            body=body,
            timeout=self.timeout,
        )
        raise_for_status(response, expected=expected)
        if response.status == 204 or not response.body:
            return response.status, None, response.headers
        return response.status, decode_json(response), response.headers

    def search(
        self,
        sequence: str,
        *,
        topk_results: int = 10,
        topk_features: int = 20,
        min_similarity: float = 0.5,
        cluster_pct_characterized_max: int | None = None,
        include_cluster_info: bool = False,
    ) -> dict[str, Any]:
        normalized = validate_atlas_search_sequence(sequence)
        _integer_in_range("topk_results", topk_results, 1, 100)
        _integer_in_range("topk_features", topk_features, 1, 100)
        if (
            isinstance(min_similarity, bool)
            or not isinstance(min_similarity, (int, float))
            or not math.isfinite(min_similarity)
            or not 0 <= min_similarity <= 1
        ):
            raise ValidationError("Atlas min_similarity must be between 0 and 1")
        if cluster_pct_characterized_max is not None:
            _integer_in_range(
                "cluster_pct_characterized_max",
                cluster_pct_characterized_max,
                0,
                100,
            )
        _boolean("include_cluster_info", include_cluster_info)
        _, raw, _ = self._json(
            "GET",
            "/similarity-search",
            query={
                "sequence": normalized,
                "topk_results": topk_results,
                "topk_features": topk_features,
                "min_similarity": min_similarity,
                "cluster_pct_characterized_max": cluster_pct_characterized_max,
                "include_cluster_info": include_cluster_info,
            },
        )
        result = _require_object(raw, "similarity-search")
        _require_keys(result, "similarity-search", {"query_sequence", "similar_proteins"})
        if result["query_sequence"] != normalized:
            raise SchemaDriftError(
                "Atlas similarity-search query_sequence does not match the request",
                raw=result,
            )
        if not isinstance(result["similar_proteins"], list):
            raise SchemaDriftError(
                "Atlas similarity-search similar_proteins must be a list", raw=result
            )
        return result

    def protein(
        self,
        protein_hash: str,
        *,
        topk_features: int = 10,
        fold_on_miss: bool = False,
        normalize_features: bool = True,
        feature_indices: list[int] | None = None,
        sequence_for_fold: str | None = None,
    ) -> dict[str, Any]:
        digest = validate_md5(protein_hash)
        if fold_on_miss:
            if sequence_for_fold is None:
                raise ValidationError(
                    "fold_on_miss requires --sequence or --sequence-file for the <=699 residue gate"
                )
            fold_sequence = validate_atlas_fold_sequence(sequence_for_fold)
            if sequence_md5(fold_sequence) != digest:
                raise ValidationError("fold_on_miss sequence does not match protein_hash MD5")
        _integer_in_range("topk_features", topk_features, 1, 100)
        _boolean("fold_on_miss", fold_on_miss)
        _boolean("normalize_features", normalize_features)
        indices = None
        if feature_indices is not None:
            if len(feature_indices) > 100:
                raise ValidationError("feature_indices is capped at 100 entries")
            indices = [validate_feature_index(index) for index in feature_indices]
        _, raw, _ = self._json(
            "GET",
            f"/proteins/{digest}",
            query={
                "topk_features": topk_features,
                "fold_on_miss": fold_on_miss,
                "normalize_features": normalize_features,
                "feature_indices": indices,
            },
        )
        result = _require_object(raw, "protein")
        _require_keys(result, "protein", {"protein_hash"})
        if result["protein_hash"] != digest:
            raise SchemaDriftError(
                "Atlas protein response hash does not match the request", raw=result
            )
        return result

    def cluster(self, protein_hash: str, *, topk_features: int = 10) -> dict[str, Any]:
        digest = validate_md5(protein_hash)
        _integer_in_range("topk_features", topk_features, 1, 100)
        _, raw, _ = self._json(
            "GET", f"/clusters/{digest}", query={"topk_features": topk_features}
        )
        result = _require_object(raw, "cluster")
        _require_keys(result, "cluster", {"protein_hash", "cluster_size", "member_protein_hashes"})
        if result["protein_hash"] != digest:
            raise SchemaDriftError(
                "Atlas cluster response hash does not match the request", raw=result
            )
        cluster_size = result["cluster_size"]
        if isinstance(cluster_size, bool) or not isinstance(cluster_size, int) or cluster_size < 0:
            raise SchemaDriftError(
                "Atlas cluster_size must be a nonnegative integer", raw=result
            )
        if not isinstance(result["member_protein_hashes"], list):
            raise SchemaDriftError(
                "Atlas cluster member_protein_hashes must be a list", raw=result
            )
        if any(
            not isinstance(member, str)
            or member != member.lower()
            or not re.fullmatch(r"[0-9a-f]{32}", member)
            for member in result["member_protein_hashes"]
        ):
            raise SchemaDriftError(
                "Atlas cluster member hashes are invalid", raw=result
            )
        return result

    def features(self) -> dict[str, Any]:
        _, raw, _ = self._json("GET", "/features")
        result = _require_object(raw, "features")
        _require_keys(result, "features", {"data"})
        if not isinstance(result["data"], list):
            raise SchemaDriftError("Atlas features data must be a list", raw=result)
        return result

    def feature(self, feature_index: int) -> dict[str, Any]:
        index = validate_feature_index(feature_index)
        _, raw, _ = self._json("GET", f"/features/{index}")
        result = _require_object(raw, "feature")
        _require_keys(result, "feature", {"feature_index", "label", "description"})
        if result["feature_index"] != index:
            raise SchemaDriftError(
                "Atlas feature response index does not match the request", raw=result
            )
        return result

    def thumbnail(self, protein_hash: str, thumbnail_type: str) -> HTTPResponse:
        digest = validate_md5(protein_hash)
        if thumbnail_type not in {"pct-characterized", "plddt"}:
            raise ValidationError("thumbnail_type must be pct-characterized or plddt")
        response = self.transport.request(
            "GET",
            self._url(f"/proteins/{digest}/thumbnail/{thumbnail_type}"),
            headers={"accept": "image/png"},
            timeout=self.timeout,
        )
        raise_for_status(response, expected={200})
        _validate_png(response.body, "Atlas thumbnail response")
        return response

    def submit_batch(
        self,
        protein_hashes: list[str],
        *,
        topk_features: int = 10,
        include_structure: bool = True,
        include_cluster_info: bool = True,
        include_sequence: bool = True,
        include_features: dict[str, bool] | None = None,
    ) -> tuple[int, Any, dict[str, str]]:
        hashes = validate_batch_hashes(protein_hashes)
        _integer_in_range("topk_features", topk_features, 1, 100)
        _boolean("include_structure", include_structure)
        _boolean("include_cluster_info", include_cluster_info)
        _boolean("include_sequence", include_sequence)
        feature_options = (
            {"protein_level": True, "per_residue": True}
            if include_features is None
            else include_features
        )
        unknown = set(feature_options) - {"protein_level", "per_residue"}
        if (
            unknown
            or set(feature_options) != {"protein_level", "per_residue"}
            or any(not isinstance(value, bool) for value in feature_options.values())
        ):
            raise ValidationError(
                "include_features requires boolean protein_level and per_residue fields"
            )
        payload = {
            "protein_hashes": hashes,
            "topk_features": topk_features,
            "include_structure": include_structure,
            "include_cluster_info": include_cluster_info,
            "include_sequence": include_sequence,
            "include_features": feature_options,
        }
        response = self.transport.request(
            "POST",
            self._url("/proteins/batch"),
            headers={"accept": "application/json, application/zip", "content-type": "application/json"},
            body=encode_json_body(payload),
            timeout=self.timeout,
        )
        raise_for_status(response, expected={200, 202})
        status = response.status
        raw: Any = response.body if status == 200 else decode_json(response)
        if status == 200:
            _validate_zip(response.body, "Atlas synchronous batch response")
        if status == 202:
            result = _require_object(raw, "batch submit")
            _require_keys(result, "batch submit", {"status", "job_id"})
            try:
                validate_job_id(result["job_id"])
            except ValidationError as exc:
                raise SchemaDriftError(
                    "Atlas batch submit job_id is invalid", raw=result
                ) from exc
            if result["status"] != "pending":
                raise SchemaDriftError(
                    "Atlas batch submit status must be pending", raw=result
                )
        return status, raw, response.headers

    def batch_status(self, job_id: str) -> tuple[int, dict[str, Any]]:
        job_id = validate_job_id(job_id)
        status, raw, _ = self._json(
            "GET", f"/proteins/batch/jobs/{job_id}", expected={200, 202, 410}
        )
        if status == 410:
            expired = dict(raw) if isinstance(raw, dict) else {}
            expired.update({"status": "expired", "job_id": expired.get("job_id", job_id)})
            return status, expired
        result = _require_object(raw, "batch status")
        _require_keys(result, "batch status", {"status", "job_id"})
        returned_job_id = result["job_id"]
        if not isinstance(returned_job_id, str) or not JOB_ID_RE.fullmatch(
            returned_job_id
        ):
            raise SchemaDriftError("Atlas batch status job_id is invalid", raw=result)
        if returned_job_id != job_id:
            raise SchemaDriftError(
                "Atlas batch status returned a different job_id", raw=result
            )
        if result["status"] not in {"pending", "completed", "cancelled", "failed", "expired"}:
            raise SchemaDriftError(
                f"Atlas batch status is unknown: {result['status']!r}", raw=result
            )
        for field in ("completed_count", "total_count"):
            value = result.get(field)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise SchemaDriftError(
                    f"Atlas batch status {field} must be a nonnegative integer",
                    raw=result,
                )
        if (
            isinstance(result.get("completed_count"), int)
            and isinstance(result.get("total_count"), int)
            and result["completed_count"] > result["total_count"]
        ):
            raise SchemaDriftError(
                "Atlas batch completed_count exceeds total_count", raw=result
            )
        download_url = result.get("download_url")
        if result["status"] == "completed" and download_url is None:
            raise SchemaDriftError(
                "Atlas completed batch is missing a valid HTTPS download_url", raw=result
            )
        if download_url is not None:
            try:
                safe_download_endpoint(download_url)
            except ValidationError as exc:
                raise SchemaDriftError(
                    "Atlas batch download_url is invalid", raw=result
                ) from exc
        return status, result

    def cancel_batch(self, job_id: str) -> None:
        job_id = validate_job_id(job_id)
        self._json("DELETE", f"/proteins/batch/jobs/{job_id}", expected={204})

    def wait_for_batch(
        self,
        job_id: str,
        *,
        poll_interval: float = 5.0,
        timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        if (
            not math.isfinite(poll_interval)
            or not math.isfinite(timeout)
            or poll_interval <= 0
            or timeout <= 0
        ):
            raise ValidationError("poll interval and timeout must be finite and positive")
        deadline = time.monotonic() + timeout
        while True:
            _, result = self.batch_status(job_id)
            if result["status"] != "pending":
                return result
            if time.monotonic() >= deadline:
                raise APIError(status=None, kind="timeout", message="Atlas batch polling timed out")
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

    def download(self, url: str, destination: Path, *, resume: bool = True) -> dict[str, Any]:
        safe_download_endpoint(url)
        partial = destination.with_suffix(destination.suffix + ".partial")
        headers: dict[str, str] = {}
        offset = partial.stat().st_size if resume and partial.exists() else 0
        if offset:
            headers["range"] = f"bytes={offset}-"
        stream_to = getattr(self.transport, "stream_to", None)
        if callable(stream_to):
            response = stream_to(
                "GET",
                url,
                partial,
                headers=headers,
                timeout=self.timeout,
                append_on_partial=bool(offset),
                expected_offset=offset,
            )
        else:
            response = self.transport.request("GET", url, headers=headers, timeout=self.timeout)
        raise_for_status(response, expected={200, 206})
        if not callable(stream_to):
            mode = "ab" if offset and response.status == 206 else "wb"
            partial.parent.mkdir(parents=True, exist_ok=True)
            with partial.open(mode) as handle:
                handle.write(response.body)
                handle.flush()
            if response.status == 206:
                validate_content_range(
                    response.headers, offset, final_size=partial.stat().st_size
                )
        _validate_zip(partial, "Atlas batch download")
        partial.replace(destination)
        return {
            "path": str(destination),
            "size_bytes": destination.stat().st_size,
            "sha256": sha256_file(destination),
            "resumed": bool(offset and response.status == 206),
            "http_status": response.status,
        }
