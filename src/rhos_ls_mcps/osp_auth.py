"""keystoneauth plugin for dynamic credentials: token + endpoint, with the
service catalog fetched from the endpoint so version discovery and per-service
endpoints work (admin_token has no catalog and routes everything to one URL).
"""

from typing import Any

from keystoneauth1 import access, loading
from keystoneauth1.identity import base as identity_base
from keystoneauth1 import session as ks_session

from rhos_ls_mcps import settings


# Catalog cache indexed by endpoint. Command workers are forked once and then
# serve many commands, so this dict lives for the worker's lifetime.
# shortcut: counters are per-worker, not global; a shared file cache would add
# I/O and races for little gain, upgrade if catalogs must refresh in lockstep.
_CATALOG_CACHE: dict[str, dict] = {}


class McpToken(identity_base.BaseIdentityPlugin):
    """Static token + endpoint auth that behaves like a catalog-based plugin.

    The admin_token plugin OSC would otherwise pick for `--os-token` +
    `--os-endpoint` has no service catalog, so `openstack versions show` fails
    and every service is routed to the endpoint URL. This plugin keeps the
    token and endpoint as given, and adds the catalog that Keystone returns
    when validating the token, so the inherited BaseIdentityPlugin methods
    resolve real per-service endpoints and versions.
    """

    def __init__(self, endpoint: str, token: str):
        # reauthenticate=False: the token is static, re-validating can't renew it
        super().__init__(auth_url=endpoint, reauthenticate=False)
        self.endpoint = endpoint
        self.token = token

    def get_token(self, session: ks_session.Session) -> str | None:
        return self.token

    def get_auth_ref(self, session: ks_session.Session) -> access.AccessInfo:
        """Fabricate an AccessInfo with a service catalog for this token.

        admin_token has no catalog, so `versions show` (microversion defaults)
        fails and every service gets routed to the endpoint URL. Validating the
        token against Keystone (GET /auth/tokens, self-validation allowed by
        default policy) returns its service catalog, which the inherited
        get_endpoint/get_endpoint_data/get_all_version_data then use to resolve
        real per-service endpoints and versions. The catalog is cached per
        endpoint and re-fetched after `catalog_refresh_commands` commands have
        used it, instead of querying Keystone on every command. Only the
        catalog is kept: no OSC command sets required_scope, so the token's
        own scope is not needed here.
        """
        endpoint = self.endpoint.rstrip("/")
        cached = _CATALOG_CACHE.get(endpoint)
        refresh_after = settings.CONFIG.openstack.catalog_refresh_commands
        if cached and cached["uses"] < refresh_after:
            cached["uses"] += 1
        else:
            resp = session.get(
                f"{endpoint}/auth/tokens",
                headers={"X-Auth-Token": self.token, "X-Subject-Token": self.token},
                authenticated=False,  # we fetch our own auth data: no recursion
            )
            token_data: dict[str, Any] | None = resp.json().get("token")
            if token_data is None:
                raise ValueError(
                    f"No token data from {endpoint}/auth/tokens: "
                    f"OS_URL must be the Keystone v3 endpoint"
                )
            catalog: list[dict[str, Any]] = token_data.get("catalog", [])
            # Count the fetching command as the first use, so a catalog serves
            # exactly `catalog_refresh_commands` commands before refreshing
            cached = {"catalog": catalog, "uses": 1}
            _CATALOG_CACHE[endpoint] = cached
        return access.create(
            body={"token": {"catalog": cached["catalog"]}}, auth_token=self.token
        )


class McpTokenLoader(loading.BaseLoader[McpToken]):
    @property
    def plugin_class(self) -> type[McpToken]:
        return McpToken

    def get_options(self) -> list[loading.Opt]:
        # Same option names as admin_token, so --os-token/--os-endpoint keep
        # working for the argv we build in osc.get_osp_credentials_args
        return [
            loading.Opt("endpoint", help="Keystone v3 endpoint"),
            loading.Opt("token", secret=True, help="The token"),
        ]
