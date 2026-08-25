from __future__ import annotations

import hashlib
import hmac
import re
import time
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import Settings


ACCOUNT_PATTERN = re.compile(r"(?<!\d)\d{12}(?!\d)")
ARN_PATTERN = re.compile(r"arn:aws-cn:[^\s,\]\}\"]+")
RESOURCE_PATTERN = re.compile(
    r"\b(?:i|vol|snap|eni|eipalloc|nat|igw|subnet|vpc|sg)-[0-9a-f]{8,17}\b",
    re.IGNORECASE,
)


class SecurityService:
    cookie_name = "finops_session"

    def __init__(self, settings: Settings):
        if not settings.session_secret or not settings.admin_password_hash:
            raise RuntimeError("Authentication secrets are not configured")
        self.settings = settings
        self.serializer = URLSafeTimedSerializer(settings.session_secret, salt="finops-session-v1")
        self.password_hasher = PasswordHasher()
        self.pseudonym_key = (settings.pseudonym_secret or settings.session_secret).encode()

    def verify_password(self, password: str) -> bool:
        try:
            return self.password_hasher.verify(self.settings.admin_password_hash, password)
        except VerificationError:
            return False

    def issue_session(self, username: str) -> str:
        return self.serializer.dumps({"sub": username, "iat": int(time.time())})

    def session_user(self, request: Request) -> str | None:
        token = request.cookies.get(self.cookie_name)
        if not token:
            return None
        try:
            payload = self.serializer.loads(token, max_age=self.settings.session_ttl_seconds)
            return str(payload.get("sub")) if payload.get("sub") else None
        except (BadSignature, SignatureExpired):
            return None

    def require_user(self, request: Request) -> str:
        user = self.session_user(request)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
        return user

    def pseudonym(self, value: str, prefix: str = "res") -> str:
        digest = hmac.new(self.pseudonym_key, value.encode(), hashlib.sha256).hexdigest()[:12]
        return f"{prefix}-{digest}"

    def redact_text(self, value: str) -> str:
        value = ACCOUNT_PATTERN.sub("[ACCOUNT]", value)
        value = ARN_PATTERN.sub(lambda match: self.pseudonym(match.group(), "arn"), value)
        value = RESOURCE_PATTERN.sub(lambda match: self.pseudonym(match.group(), "res"), value)
        return value

    def sanitize(self, value: Any, key: str = "") -> Any:
        sensitive_keys = {
            "accountid",
            "account_id",
            "resourcearn",
            "resource_arn",
            "instancearn",
            "volumeArn",
        }
        resource_keys = {
            "resourceid",
            "resource_id",
            "instanceid",
            "instance_id",
            "volumeid",
            "dbinstanceidentifier",
            "dbclusteridentifier",
            "loadbalancerarn",
            "natgatewayid",
            "allocationid",
        }
        normalized = key.replace("-", "").replace("_", "").lower()
        if normalized in {item.replace("_", "").lower() for item in sensitive_keys}:
            return self.pseudonym(str(value), "arn" if "arn" in normalized else "account")
        if normalized in {item.replace("_", "").lower() for item in resource_keys}:
            return self.pseudonym(str(value))
        if isinstance(value, dict):
            return {str(k): self.sanitize(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self.sanitize(item, key) for item in value]
        if isinstance(value, str):
            return self.redact_text(value)
        return value
