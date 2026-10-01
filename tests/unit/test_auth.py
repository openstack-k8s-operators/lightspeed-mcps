import unittest

from rhos_ls_mcps import auth, settings


class StaticTokenVerifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_matching_token_with_read_scope(self):
        token = await auth.StaticTokenVerifier("expected").verify_token("expected")
        self.assertEqual(token.token, "expected")
        self.assertEqual(token.scopes, ["read"])

    async def test_rejects_non_matching_token(self):
        self.assertIsNone(
            await auth.StaticTokenVerifier("expected").verify_token("bad")
        )

    async def test_grants_write_scope_when_configured(self):
        token = await auth.StaticTokenVerifier(
            "expected", read_only=False
        ).verify_token("expected")
        self.assertEqual(token.scopes, ["read", "write"])


class SecuritySettingsTests(unittest.TestCase):
    def test_disables_auth_without_a_token(self):
        security = auth.get_auth_settings(settings.Settings())
        self.assertIsNone(security.auth)
        self.assertIsNone(security.token_verifier)

    def test_configures_static_auth_with_token(self):
        config = settings.Settings(mcp_transport_security={"token": "secret"})
        security = auth.get_auth_settings(config)
        self.assertIsNotNone(security.auth)
        self.assertIsInstance(security.token_verifier, auth.StaticTokenVerifier)

    def test_copies_transport_security_options(self):
        config = settings.Settings(
            mcp_transport_security={
                "enable_dns_rebinding_protection": True,
                "allowed_hosts": ["mcp.example.test"],
                "allowed_origins": ["https://console.example.test"],
            }
        )
        transport = auth.get_transport_security(config)
        self.assertTrue(transport.enable_dns_rebinding_protection)
        self.assertEqual(transport.allowed_hosts, ["mcp.example.test"])
        self.assertEqual(transport.allowed_origins, ["https://console.example.test"])
