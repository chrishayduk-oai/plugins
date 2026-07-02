"""Credential presence reporting and recursive redaction."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SECRET_KEY_RE = re.compile(
    r"(^|[-_])(authorization|api[-_]?key|token|token[-_]?id|token[-_]?secret|secret|password)($|[-_])",
    re.IGNORECASE,
)
BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(ESM_API_KEY|HF_TOKEN|MODAL_TOKEN_ID|MODAL_TOKEN_SECRET|api[-_]?key|"
    r"token(?:[-_](?:id|secret))?|secret|password|credential|signature)"
    r"\s*[=:]\s*[^\s,;]+"
)
URL_SECRET_PARAM_RE = re.compile(
    r"(?i)([?&](?:x-amz-credential|x-amz-signature|x-amz-security-token|token|credential|signature)=)[^&#\s]+"
)


def _configured(env: Mapping[str, str], key: str) -> bool:
    return bool(env.get(key, "").strip())


def credential_preflight(
    env: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> dict[str, object]:
    values = os.environ if env is None else env
    root = Path.home() if home is None else home
    modal_env = _configured(values, "MODAL_TOKEN_ID") and _configured(values, "MODAL_TOKEN_SECRET")
    modal_profile = (root / ".modal.toml").is_file()
    return {
        "biohub_managed": {
            "status": "configured" if _configured(values, "ESM_API_KEY") else "missing",
            "source": "ESM_API_KEY",
        },
        "atlas": {"status": "not-required", "source": "public v1alpha1 API and anonymous S3"},
        "modal": {
            "status": "configured" if modal_env or modal_profile else "missing",
            "source": "environment" if modal_env else ("profile" if modal_profile else "none"),
        },
        "hugging_face": {
            "status": "configured" if _configured(values, "HF_TOKEN") else "optional-missing",
            "source": "HF_TOKEN (optional for public weights)",
        },
    }


def redact_text(value: str, env: Mapping[str, str] | None = None) -> str:
    result = BEARER_RE.sub("Bearer [REDACTED]", value)
    result = URL_SECRET_PARAM_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", result)
    result = SECRET_ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", result)
    values = os.environ if env is None else env
    for key in ("ESM_API_KEY", "HF_TOKEN", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"):
        secret = values.get(key, "")
        if secret and len(secret) >= 4:
            result = result.replace(secret, "[REDACTED]")
    return result


def redact(value: Any, env: Mapping[str, str] | None = None) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if SECRET_KEY_RE.search(str(key)) else redact(item, env)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, env) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, env) for item in value)
    if isinstance(value, str):
        return redact_text(value, env)
    return value
