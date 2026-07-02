"""Error types with secret-safe messages."""

from __future__ import annotations

from typing import Any


class BiohubESMError(RuntimeError):
    """Base error for deterministic plugin helpers."""


class ValidationError(BiohubESMError, ValueError):
    """Input failed local validation before provider submission."""


class SchemaDriftError(BiohubESMError):
    """A public alpha response no longer matches the contract we validated."""

    def __init__(self, message: str, *, raw: Any = None) -> None:
        super().__init__(message)
        self.raw = raw
        self.diagnostic_path: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": "schema-drift",
            "message": str(self),
            "raw_response_available": self.raw is not None,
        }
        if self.diagnostic_path:
            result["diagnostic_path"] = self.diagnostic_path
        return result


class APIError(BiohubESMError):
    """Normalized provider failure without response secrets."""

    def __init__(
        self,
        *,
        status: int | None,
        kind: str,
        message: str,
        retry_after: float | None = None,
        partial: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.retry_after = retry_after
        self.partial = partial

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "kind": self.kind,
            "message": str(self),
            "retry_after": self.retry_after,
            "partial": self.partial,
        }
