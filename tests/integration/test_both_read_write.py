"""Both tools enabled, read-write, static credentials.

`blocked_commands` is the default list minus `cluster-info`, so the
successful `cluster-info` call proves the override reaches the server.
Writes use uuid-suffixed names.  Every write is verified on the cluster
itself and cleaned up with the raw `oc`/`openstack` CLIs exec'd in the
server container (bypassing the MCP under test), so a broken MCP cannot
fake a success or leave resources behind.
"""

import json
from uuid import uuid4

from tests.integration.mcp_container import (
    OPENSHIFT_PATH,
    OPENSTACK_PATH,
    McpIntegrationTest,
    default_blocked_commands_without,
)


class BothReadWriteTests(McpIntegrationTest):
    openstack_enabled = True
    openshift_enabled = True
    openstack_allow_write = True
    openshift_allow_write = True
    openshift_blocked_commands = default_blocked_commands_without("cluster-info")
    static_creds = True

    async def test_volume_create_with_and_without_prefix(self):
        name = f"itest-{uuid4().hex[:8]}"
        with_prefix = await self.call_tool(
            OPENSTACK_PATH, f"openstack volume create --size 1 {name}-1"
        )
        without_prefix = await self.call_tool(
            OPENSTACK_PATH, f"volume create --size 1 {name}-2"
        )
        self.assertIn(f"{name}-1", with_prefix)
        self.assertIn(f"{name}-2", without_prefix)
        # The writes must have reached the cluster, not just the MCP output
        for suffix in ("-1", "-2"):
            volume_name = f"{name}{suffix}"
            output = (
                await self.exec_openstack("volume", "show", volume_name, "-f", "json")
            ).stdout
            self.assertEqual(json.loads(output)["name"], volume_name)
            # Cleanup never goes through the MCP under test
            self.addCleanup(self.delete_volume, volume_name)

    async def delete_volume(self, name):
        """Best-effort direct cleanup; safe when the volume is gone."""
        result = await self.exec_openstack("volume", "show", name, check=False)
        if result.returncode == 0:
            await self.exec_openstack("volume", "delete", name)

    async def test_namespace_create_and_delete(self):
        name = f"itest-{uuid4().hex[:8]}"
        output = await self.call_tool(OPENSHIFT_PATH, f"create namespace {name}")
        self.assertIn(name, output)
        # The write must have reached the cluster, not just the MCP output
        result = await self.exec_in_container(
            "oc", "get", "namespace", name, "-o", "name"
        )
        self.assertEqual(result.stdout.strip(), f"namespace/{name}")
        # Safety net for the delete below; never through the MCP under test
        self.addCleanup(
            self.exec_in_container,
            "oc",
            "delete",
            "namespace",
            name,
            "--ignore-not-found",
        )
        output = await self.call_tool(OPENSHIFT_PATH, f"delete namespace {name}")
        self.assertIn(name, output)
        # oc delete waits for the deletion, so the namespace must be gone
        result = await self.exec_in_container(
            "oc", "get", "namespace", name, check=False
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NotFound", result.stderr)

    async def test_cluster_info_is_allowed_by_the_override(self):
        output = await self.call_tool(OPENSHIFT_PATH, "cluster-info")
        self.assertIn("Kubernetes control plane", output)

    async def test_still_blocked_commands_are_rejected(self):
        await self.assert_tool_error(OPENSHIFT_PATH, "config view")

    async def test_forbidden_arguments_are_still_rejected(self):
        await self.assert_tool_error(
            OPENSTACK_PATH, "project list --os-token some-token"
        )
        await self.assert_tool_error(
            OPENSHIFT_PATH, "get pod -n openstack --token some-token"
        )

    async def test_pipes_still_fail(self):
        await self.assert_tool_error(OPENSTACK_PATH, "project list | grep admin")
        await self.assert_tool_error(OPENSHIFT_PATH, "get pod -n openstack | grep nova")
