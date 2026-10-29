"""Both tools enabled, read-only, credentials passed per request as headers.

No static credential files are mounted, matching the README's
dynamic-credentials deployment.  Known limitation: a `clouds.yaml` in
/app would be auto-selected by openstacksdk and leak its `auth_url` into
the Token plugin, breaking header credentials, so nothing is mounted.
"""

from tests.integration.mcp_container import (
    OPENSHIFT_PATH,
    OPENSTACK_PATH,
    McpIntegrationTest,
)


class DynamicCredentialsTests(McpIntegrationTest):
    openstack_enabled = True
    openshift_enabled = True
    openstack_allow_write = False
    openshift_allow_write = False
    static_creds = False

    async def test_openstack_token_and_url_headers(self):
        credentials = self.dynamic_credentials()
        output = await self.call_tool(
            OPENSTACK_PATH,
            "project list",
            headers={
                "OS_TOKEN": credentials["OS_TOKEN"],
                "OS_URL": credentials["OS_URL"],
            },
        )
        self.assertIn("admin", output)

    async def test_openstack_discovers_versions_with_token_and_url_headers(self):
        # Version discovery needs the service catalog, which the mcp_token
        # plugin fetches by validating the token against OS_URL (Keystone)
        credentials = self.dynamic_credentials()
        output = await self.call_tool(
            OPENSTACK_PATH,
            "versions show --format json",
            headers={
                "OS_TOKEN": credentials["OS_TOKEN"],
                "OS_URL": credentials["OS_URL"],
            },
        )
        self.assertIn("identity", output)
        self.assertIn("block-storage", output)

    async def test_openshift_token_and_url_headers(self):
        credentials = self.dynamic_credentials()
        output = await self.call_tool(
            OPENSHIFT_PATH,
            "get pod -n openstack",
            headers={
                "OCP_TOKEN": credentials["OCP_TOKEN"],
                "OCP_URL": credentials["OCP_URL"],
            },
        )
        self.assertTrue(output.strip())

    async def test_missing_openstack_credentials_fail(self):
        await self.assert_tool_error(
            OPENSTACK_PATH, "project list", pattern="Missing OpenStack credentials"
        )

    async def test_missing_openshift_credentials_fail(self):
        await self.assert_tool_error(OPENSHIFT_PATH, "get pod -n openstack")

    async def test_request_without_bearer_token_is_unauthorized(self):
        response = await self.raw_rpc(OPENSTACK_PATH, "tools/list", auth=False)
        self.assertEqual(response.status_code, 401)
        response = await self.raw_rpc(OPENSHIFT_PATH, "tools/list", auth=False)
        self.assertEqual(response.status_code, 401)
