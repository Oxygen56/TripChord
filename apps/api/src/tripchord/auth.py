from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from tripchord.config import Settings, get_settings
from tripchord.domain.common import DomainModel


class Principal(DomainModel):
    tenant_id: str
    auth_mode: str


def authenticate(settings: Settings, credential: str | None) -> Principal:
    if not settings.auth_tokens:
        if settings.auth_required:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="authentication is required but no server tokens are configured",
            )
        return Principal(tenant_id="anonymous", auth_mode="development-anonymous")
    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing TripChord API credential",
            headers={"WWW-Authenticate": "Bearer"},
        )
    matched_tenant: str | None = None
    for token, tenant_id in settings.auth_tokens.items():
        if secrets.compare_digest(credential, token):
            matched_tenant = tenant_id
    if matched_tenant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid TripChord API credential",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Principal(tenant_id=matched_tenant, auth_mode="static-token")


async def get_principal(
    authorization: str | None = Header(default=None),
    x_tripchord_key: str | None = Header(default=None),
) -> Principal:
    bearer = None
    if authorization is not None and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    return authenticate(get_settings(), bearer or x_tripchord_key)
