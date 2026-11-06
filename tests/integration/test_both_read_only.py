"""Both tools enabled, read-only, static credentials."""

from tests.integration.mcp_container import (
    OPENSHIFT_PATH,
    OPENSTACK_PATH,
    McpIntegrationTest,
)


class BothReadOnlyTests(McpIntegrationTest):
    openstack_enabled = True
    openshift_enabled = True
    openstack_allow_write = False
    openshift_allow_write = False
    static_creds = True

    async def test_openstack_read(self):
        output = await self.call_tool(OPENSTACK_PATH, "project list")
        self.assertIn("admin", output)

    async def test_openshift_read(self):
        output = await self.call_tool(
            OPENSHIFT_PATH, "get pod -n openshift-apiserver -o name"
        )
        self.assertTrue(output.strip().startswith("pod/"))

    async def test_openstack_write_is_rejected(self):
        await self.assert_tool_error(
            OPENSTACK_PATH, "volume create --size 1 ro_test-volume"
        )

    async def test_openshift_write_is_rejected(self):
        await self.assert_tool_error(OPENSHIFT_PATH, "create namespace itest")
