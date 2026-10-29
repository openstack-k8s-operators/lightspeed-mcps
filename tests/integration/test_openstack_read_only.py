"""OpenStack-only, read-only scenario with static credentials."""

import json

from tests.integration.mcp_container import OPENSTACK_PATH, McpIntegrationTest


class OpenStackReadOnlyTests(McpIntegrationTest):
    openstack_enabled = True
    openshift_enabled = False
    openstack_allow_write = False
    static_creds = True

    async def test_project_list_with_and_without_prefix(self):
        with_prefix = await self.call_tool(OPENSTACK_PATH, "openstack project list")
        without_prefix = await self.call_tool(OPENSTACK_PATH, "project list")
        self.assertIn("admin", with_prefix)
        self.assertEqual(with_prefix, without_prefix)

    async def test_project_list_json_output_is_parseable(self):
        output = await self.call_tool(OPENSTACK_PATH, "project list --format json")
        projects = json.loads(output)
        self.assertTrue(any(project["Name"] == "admin" for project in projects))

    async def test_write_commands_are_rejected(self):
        await self.assert_tool_error(
            OPENSTACK_PATH, "volume create --size 1 ro_test-volume"
        )
        await self.assert_tool_error(
            OPENSTACK_PATH, "openstack volume create --size 1 ro_test-volume"
        )

    async def test_unknown_command_is_rejected(self):
        await self.assert_tool_error(OPENSTACK_PATH, "cinder list")

    async def test_forbidden_global_arguments_are_rejected(self):
        test_command = "project list "
        reject_params = (
            # This parameter was deleted from the openstack client parser
            "--os-username admin",
            # These parameters exist in the parser but are rejected by the MCP server
            "--os-token some-token",
            "--os-auth-url https://keystone.example",
            "--os-endpoint https://keystone.example/v3",
            "--insecure",
            "--os-cacert /tmp/ca.pem",
        )
        for param in reject_params:
            for command in (test_command + param, param + test_command):
                with self.subTest(command=command):
                    await self.assert_tool_error(OPENSTACK_PATH, command)

    async def test_shell_commands_fail(self):
        await self.assert_tool_error(OPENSTACK_PATH, "curl http://example.com")

    async def test_pipes_fail(self):
        await self.assert_tool_error(OPENSTACK_PATH, "project list | grep admin")

    async def test_bare_openstack_command_is_rejected(self):
        # Rejected because the tool would enter interactive mode and never exit
        await self.assert_tool_error(OPENSTACK_PATH, "openstack")

    async def test_tools_list_only_contains_openstack_cli(self):
        result = await self.rpc(OPENSTACK_PATH, "tools/list")
        self.assertEqual(["openstack-cli"], [tool["name"] for tool in result["tools"]])

    async def test_openshift_endpoint_is_not_mounted(self):
        # We are running with `openshift_enabled = False` so OpenShift CLI tool should not be there`
        response = await self.raw_rpc("/openshift/", "tools/list")
        self.assertEqual(response.status_code, 404)

    async def test_concurrent_calls_beat_serial_execution(self):
        await self.assert_concurrent(OPENSTACK_PATH, "project list")
