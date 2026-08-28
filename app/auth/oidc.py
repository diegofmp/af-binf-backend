import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.jose import jwt as jose_jwt
from authlib.oidc.core import CodeIDToken

from app.config import get_settings


class OIDCError(Exception):
    pass


class OIDCClient:
    """Thin wrapper around Authlib for the Authorization Code + PKCE flow.

    Provider details (issuer, client id/secret, redirect URI, scopes) are
    fully driven by Settings/env — nothing here is specific to any one
    company SSO product. Discovery is fetched from
    `{issuer}/.well-known/openid-configuration` at call time so switching
    providers only requires changing env vars.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._discovery_cache: dict | None = None

    async def _discovery(self) -> dict:
        if self._discovery_cache is None:
            url = f"{self._settings.oidc_issuer.rstrip('/')}/.well-known/openid-configuration"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                self._discovery_cache = resp.json()
        return self._discovery_cache

    async def authorization_url(self, *, state: str, code_challenge: str) -> str:
        discovery = await self._discovery()
        client = AsyncOAuth2Client(
            client_id=self._settings.oidc_client_id,
            redirect_uri=self._settings.oidc_redirect_uri,
            scope=" ".join(self._settings.oidc_scopes_list),
        )
        url, _state = client.create_authorization_url(
            discovery["authorization_endpoint"],
            state=state,
            code_challenge=code_challenge,
            code_challenge_method="S256",
        )
        return url

    async def exchange_code(self, *, code: str, code_verifier: str) -> dict:
        """Exchange an authorization code for tokens. Returns the raw token response."""
        discovery = await self._discovery()
        client = AsyncOAuth2Client(
            client_id=self._settings.oidc_client_id,
            client_secret=self._settings.oidc_client_secret,
            redirect_uri=self._settings.oidc_redirect_uri,
        )
        token = await client.fetch_token(
            discovery["token_endpoint"],
            code=code,
            code_verifier=code_verifier,
            grant_type="authorization_code",
        )
        return token

    async def validate_id_token(self, token_response: dict) -> CodeIDToken:
        id_token = token_response.get("id_token")
        if not id_token:
            raise OIDCError("token response did not include an id_token")

        discovery = await self._discovery()
        async with httpx.AsyncClient(timeout=10) as client:
            jwks_resp = await client.get(discovery["jwks_uri"])
            jwks_resp.raise_for_status()
            jwks = jwks_resp.json()

        try:
            claims = jose_jwt.decode(
                id_token,
                jwks,
                claims_options={
                    "iss": {"essential": True, "value": discovery["issuer"]},
                    "aud": {"essential": True, "value": self._settings.oidc_client_id},
                },
            )
            claims.validate()
        except Exception as exc:
            raise OIDCError(f"id_token validation failed: {exc}") from exc

        return claims
