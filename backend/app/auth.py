"""Auth. Locally a dev issuer signs RS256 JWTs; the API validates by JWKS, exactly as it will against
Entra ID on Azure (set JWKS_URL + audience; delete the dev-login route)."""
import time
from functools import lru_cache
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.settings import settings

ISSUER = "onboarding-dev-issuer"
bearer = HTTPBearer(auto_error=False)


@lru_cache
def _private_key():
    path = Path(settings().jwt_private_key_path)
    if not path.exists():  # first boot: generate a dev key (gitignored)
        path.parent.mkdir(parents=True, exist_ok=True)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return serialization.load_pem_private_key(path.read_bytes(), None)


def jwks() -> dict:
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(_private_key().public_key(), as_dict=True)  # type: ignore[union-attr]
    return {"keys": [{**jwk, "kid": "dev", "use": "sig", "alg": "RS256"}]}


def issue(sub: str, role: str) -> str:
    now = int(time.time())
    claims = {"sub": sub, "roles": [role], "iss": ISSUER, "aud": settings().jwt_audience, "iat": now, "exp": now + 8 * 3600}
    return jwt.encode(claims, _private_key(), algorithm="RS256", headers={"kid": "dev"})  # type: ignore[arg-type]


@lru_cache
def _jwks_client():
    return jwt.PyJWKClient(settings().jwks_url) if settings().jwks_url else None


def verify(token: str) -> dict:
    client = _jwks_client()
    key = client.get_signing_key_from_jwt(token).key if client else _private_key().public_key()  # type: ignore[union-attr]
    return jwt.decode(token, key, algorithms=["RS256"], audience=settings().jwt_audience)  # type: ignore[arg-type]


def require(*roles: str):
    def dep(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
        if not cred:
            raise HTTPException(401, "missing bearer token")
        try:
            claims = verify(cred.credentials)
        except jwt.PyJWTError as e:
            raise HTTPException(401, f"invalid token: {e}") from e
        if not set(claims.get("roles", [])) & set(roles):
            raise HTTPException(403, f"requires role: {' or '.join(roles)}")
        return claims

    return dep
