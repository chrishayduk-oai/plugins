"""Small, mockable HTTP layer for Biohub managed and Atlas APIs."""

from __future__ import annotations

import json
import math
import os
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
from pathlib import Path
from typing import Any, Protocol
from urllib import error, request

from .constants import (
    BIOHUB_BASE_URL,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ESMC_MANAGED_MODELS,
    ESMFOLD2_MANAGED_MODELS,
    MANAGED_ENDPOINTS,
)
from .errors import APIError, SchemaDriftError, ValidationError
from .security import redact


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def encode_json_body(value: Any) -> bytes:
    try:
        return json.dumps(
            value, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "outbound request must be interoperable JSON without non-finite numbers"
        ) from exc


def validate_timeout(value: float, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValidationError(f"{context} must be a finite positive number")
    return float(value)


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> HTTPResponse: ...


class _RejectRedirectHandler(request.HTTPRedirectHandler):
    """Return every redirect as a response; never forward sensitive headers."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class UrllibTransport:
    def __init__(self) -> None:
        self._opener = request.build_opener(_RejectRedirectHandler())

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> HTTPResponse:
        validate_timeout(timeout, "request timeout")
        req = request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with self._opener.open(req, timeout=timeout) as response:
                return HTTPResponse(
                    status=response.status,
                    headers={key.lower(): value for key, value in response.headers.items()},
                    body=response.read(),
                )
        except error.HTTPError as exc:
            return HTTPResponse(
                status=exc.code,
                headers={key.lower(): value for key, value in exc.headers.items()},
                body=exc.read(),
            )
        except IncompleteRead:
            raise APIError(
                status=None,
                kind="network",
                message="provider response was incomplete",
                partial=True,
            ) from None
        except (error.URLError, TimeoutError, socket.timeout, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            kind = "timeout" if isinstance(reason, (TimeoutError, socket.timeout)) else "network"
            raise APIError(status=None, kind=kind, message=f"provider request failed: {kind}") from None

    def stream_to(
        self,
        method: str,
        url: str,
        destination: Path,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        append_on_partial: bool = False,
        expected_offset: int = 0,
    ) -> HTTPResponse:
        """Stream a successful response to disk and retain partial bytes on interruption."""

        validate_timeout(timeout, "stream timeout")
        req = request.Request(url, headers=headers or {}, method=method)
        try:
            with self._opener.open(req, timeout=timeout) as response:
                response_headers = {
                    key.lower(): value for key, value in response.headers.items()
                }
                if response.status == 206:
                    validate_content_range(response_headers, expected_offset)
                mode = "ab" if append_on_partial and response.status == 206 else "wb"
                destination.parent.mkdir(parents=True, exist_ok=True)
                with destination.open(mode) as handle:
                    while chunk := response.read(1024 * 1024):
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                if response.status == 206:
                    validate_content_range(
                        response_headers,
                        expected_offset,
                        final_size=destination.stat().st_size,
                    )
                return HTTPResponse(response.status, response_headers, b"")
        except error.HTTPError as exc:
            return HTTPResponse(
                status=exc.code,
                headers={key.lower(): value for key, value in exc.headers.items()},
                body=exc.read(),
            )
        except (error.URLError, TimeoutError, socket.timeout, IncompleteRead, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            kind = "timeout" if isinstance(reason, (TimeoutError, socket.timeout)) else "network"
            raise APIError(status=None, kind=kind, message=f"provider stream failed: {kind}") from None


CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+|\*)$", re.IGNORECASE)


def validate_content_range(
    headers: dict[str, str], expected_offset: int, *, final_size: int | None = None
) -> None:
    value = headers.get("content-range", "")
    matched = CONTENT_RANGE_RE.fullmatch(value.strip())
    if not matched or int(matched.group(1)) != expected_offset:
        raise SchemaDriftError(
            "provider returned a mismatched Content-Range for resumed download",
            raw={"content-range": value, "expected_offset": expected_offset},
        )
    start = int(matched.group(1))
    end = int(matched.group(2))
    total = None if matched.group(3) == "*" else int(matched.group(3))
    if end < start or (total is not None and (end >= total or start >= total)):
        raise SchemaDriftError(
            "provider returned an invalid Content-Range",
            raw={"content-range": value, "expected_offset": expected_offset},
        )
    if final_size is not None and (
        final_size != end + 1 or (total is not None and final_size != total)
    ):
        raise SchemaDriftError(
            "provider returned an incomplete ranged download",
            raw={
                "content-range": value,
                "expected_offset": expected_offset,
                "final_size": final_size,
            },
        )


def _retry_after(
    headers: dict[str, str], *, now: datetime | None = None
) -> float | None:
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
        return max(0.0, seconds) if math.isfinite(seconds) else None
    except ValueError:
        try:
            target = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return max(0.0, (target.astimezone(timezone.utc) - current).total_seconds())


def _safe_error_message(response: HTTPResponse) -> str:
    generic = f"provider returned HTTP {response.status}"
    if not response.body:
        return generic
    try:
        payload = json.loads(
            response.body.decode("utf-8"), parse_constant=_reject_json_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return generic
    safe = redact(payload)
    if isinstance(safe, dict):
        for key in ("message", "detail", "error"):
            value = safe.get(key)
            if isinstance(value, str) and value:
                return f"{generic}: {value[:500]}"
    return generic


def raise_for_status(response: HTTPResponse, *, expected: set[int]) -> None:
    if response.status in expected:
        return
    kind = {
        400: "malformed-input",
        401: "authentication",
        403: "authorization-or-safety",
        404: "not-found",
        408: "timeout",
        409: "conflict",
        410: "expired",
        422: "validation",
        429: "rate-limit",
    }.get(response.status)
    if response.status == 402:
        kind = "credits"
    elif response.status >= 500:
        kind = "provider"
    elif kind is None:
        kind = "http"
    raise APIError(
        status=response.status,
        kind=kind,
        message=_safe_error_message(response),
        retry_after=_retry_after(response.headers),
    )


def decode_json(response: HTTPResponse) -> Any:
    try:
        return json.loads(
            response.body.decode("utf-8"), parse_constant=_reject_json_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise SchemaDriftError("provider returned malformed JSON", raw=response.body) from exc


class BiohubClient:
    """Minimal allowlisted managed-API transport.

    The official pinned ``esm`` SDK remains the preferred semantic client. This
    layer exists to make auth, status, timeout, and redaction behavior testable.
    """

    def __init__(
        self,
        *,
        token: str,
        base_url: str = BIOHUB_BASE_URL,
        transport: Transport | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.token = token
        self.timeout = validate_timeout(timeout, "Biohub request timeout")
        self.base_url = base_url.rstrip("/")
        if self.base_url != BIOHUB_BASE_URL:
            raise ValidationError("managed Biohub credentials may only be sent to https://biohub.ai")
        self.transport = transport or UrllibTransport()

    def post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if endpoint not in MANAGED_ENDPOINTS:
            raise ValidationError(f"managed endpoint is not allowlisted: {endpoint}")
        if not self.token:
            raise APIError(
                status=None,
                kind="missing-credentials",
                message="ESM_API_KEY is missing; configure it through the host secret manager",
            )
        model = payload.get("model")
        allowed_models = (
            ESMC_MANAGED_MODELS if endpoint in {"encode", "logits"} else ESMFOLD2_MANAGED_MODELS
        )
        if model not in allowed_models:
            raise ValidationError(
                f"{endpoint} requires an exact supported managed model ID"
            )
        body = encode_json_body(payload)
        response = self.transport.request(
            "POST",
            f"{self.base_url}/api/v1/{endpoint}",
            headers={
                "accept": "application/json",
                "authorization": f"Bearer {self.token}",
                "content-type": "application/json",
            },
            body=body,
            timeout=self.timeout,
        )
        raise_for_status(response, expected={200})
        result = decode_json(response)
        if not isinstance(result, dict):
            raise SchemaDriftError("managed API response must be a JSON object", raw=result)
        return result
