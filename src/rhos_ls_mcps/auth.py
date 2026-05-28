from dataclasses import dataclass

from pydantic import AnyHttpUrl

from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.provider import (
    AccessToken,
    TokenVerifier,
    OAuthAuthorizationServerProvider,
)
from mcp.server.transport_security import TransportSecuritySettings

from rhos_ls_mcps.settings import Settings


class StaticTokenVerifier(TokenVerifier):
    """Simple static token verifier."""

    def __init__(self, token: str, read_only: bool = True):
        self.token = token
        self.scopes = ["read"] if read_only else ["read", "write"]

    async def verify_token(self, token: str) -> AccessToken | None:
        if self.token != token:
            return None
        return AccessToken(
            token=token,
            client_id="",
            scopes=self.scopes,
            expires_at=None,
            resource=None,
        )


@dataclass
class SecurityConfig:
    auth: AuthSettings | None = None
    token_verifier: TokenVerifier | None = None
    auth_server_provider: OAuthAuthorizationServerProvider | None = None
    transport_security: TransportSecuritySettings | None = None


def get_auth_settings(config: Settings) -> SecurityConfig:
    """Get the security configuration for the MCP server.

    Currently only supports static token verification or nothing.
    """

    auth_server_provider = None
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=config.mcp_transport_security.enable_dns_rebinding_protection,
        allowed_hosts=config.mcp_transport_security.allowed_hosts,
        allowed_origins=config.mcp_transport_security.allowed_origins,
    )
    if config.mcp_transport_security.token:
        auth = AuthSettings(
            issuer_url=AnyHttpUrl("http://localhost:8080"),
            resource_server_url=AnyHttpUrl("http://localhost:8080"),
        )
        token_verifier = StaticTokenVerifier(
            config.mcp_transport_security.token,
            read_only=not config.openstack.allow_write,
        )
    else:
        auth = None
        token_verifier = None

    res = SecurityConfig(
        auth=auth,
        token_verifier=token_verifier,
        auth_server_provider=auth_server_provider,
        transport_security=transport_security,
    )
    return res
