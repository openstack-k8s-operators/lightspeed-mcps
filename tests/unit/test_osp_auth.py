import unittest
from unittest.mock import MagicMock, patch

from keystoneauth1 import access, loading

from rhos_ls_mcps import osp_auth, settings


VALIDATE_BODY = {
    "token": {
        "methods": ["token"],
        "expires_at": "2099-01-01T00:00:00Z",
        "user": {"id": "uid", "name": "admin"},
        "project": {"id": "pid", "name": "admin"},
        "catalog": [
            {
                "type": "identity",
                "name": "keystone",
                "endpoints": [
                    {
                        "interface": "public",
                        "region": "RegionOne",
                        "url": "https://keystone/v3",
                    }
                ],
            },
            {
                "type": "volumev3",
                "name": "cinder",
                "endpoints": [
                    {
                        "interface": "public",
                        "region": "RegionOne",
                        "url": "https://cinder/v3",
                    }
                ],
            },
        ],
    }
}


def mocked_session() -> MagicMock:
    session = MagicMock()
    session.get.return_value.json.return_value = VALIDATE_BODY
    return session


class McpTokenTests(unittest.TestCase):
    def setUp(self):
        self.config = settings.Settings()
        self.enterContext(patch.object(settings, "CONFIG", self.config))
        osp_auth._CATALOG_CACHE.clear()

    def test_fetches_catalog_from_keystone_token_validation(self):
        plugin = osp_auth.McpToken("https://keystone/v3/", "tok")
        session = mocked_session()

        auth_ref = plugin.get_auth_ref(session)

        session.get.assert_called_once_with(
            "https://keystone/v3/auth/tokens",
            headers={"X-Auth-Token": "tok", "X-Subject-Token": "tok"},
            authenticated=False,
        )
        self.assertIsInstance(auth_ref, access.AccessInfoV3)
        self.assertEqual(auth_ref.auth_token, "tok")
        self.assertEqual(
            auth_ref.service_catalog.url_for(
                service_type="volumev3", interface="public"
            ),
            "https://cinder/v3",
        )

    def test_caches_the_catalog_per_endpoint(self):
        session = mocked_session()

        # A new plugin instance is built for every command run
        for _ in range(3):
            auth_ref = osp_auth.McpToken("https://keystone/v3", "t").get_auth_ref(
                session
            )
            self.assertEqual(auth_ref.auth_token, "t")

        session.get.assert_called_once()
        self.assertEqual(osp_auth._CATALOG_CACHE["https://keystone/v3"]["uses"], 3)

    def test_uses_a_separate_cache_entry_per_endpoint(self):
        session = mocked_session()

        osp_auth.McpToken("https://keystone1/v3", "t").get_auth_ref(session)
        osp_auth.McpToken("https://keystone2/v3", "t").get_auth_ref(session)

        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(
            session.get.call_args_list[0].args[0], "https://keystone1/v3/auth/tokens"
        )
        self.assertEqual(
            session.get.call_args_list[1].args[0], "https://keystone2/v3/auth/tokens"
        )

    def test_refreshes_the_catalog_after_the_configured_commands(self):
        self.config.openstack.catalog_refresh_commands = 2
        session = mocked_session()

        for _ in range(4):
            osp_auth.McpToken("https://keystone/v3", "t").get_auth_ref(session)

        # 2 commands per fetch: the second fetch serves commands 3 and 4
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(osp_auth._CATALOG_CACHE["https://keystone/v3"]["uses"], 2)

    def test_rejects_endpoints_that_do_not_return_token_data(self):
        session = MagicMock()
        session.get.return_value.json.return_value = {"versions": {"values": []}}

        with self.assertRaisesRegex(ValueError, "Keystone v3 endpoint"):
            osp_auth.McpToken("https://not-keystone/v3", "t").get_auth_ref(session)

    def test_resolves_endpoints_from_the_cached_catalog(self):
        plugin = osp_auth.McpToken("https://keystone/v3", "tok")
        self.assertIsNone(plugin.auth_ref)
        plugin.get_auth_ref(mocked_session())

        # No session interaction is needed once the catalog is cached
        endpoint = plugin.get_endpoint(
            MagicMock(), service_type="volumev3", interface="public"
        )
        self.assertEqual(endpoint, "https://cinder/v3")


class McpTokenLoaderTests(unittest.TestCase):
    def test_exposes_the_admin_token_option_names(self):
        self.assertEqual(
            [opt.name for opt in osp_auth.McpTokenLoader().get_options()],
            ["endpoint", "token"],
        )

    def test_loads_the_plugin_from_options(self):
        plugin = osp_auth.McpTokenLoader().load_from_options(
            endpoint="https://keystone/v3",
            token="t",  # noqa: S106
        )
        self.assertIsInstance(plugin, osp_auth.McpToken)
        self.assertEqual(plugin.endpoint, "https://keystone/v3")
        self.assertEqual(plugin.token, "t")

    def test_plugin_is_registered_with_keystoneauth(self):
        loader = loading.get_plugin_loader("mcp_token")
        self.assertIsInstance(loader, osp_auth.McpTokenLoader)
