"""OpenShift-only, read-only scenario with static credentials.

`allowed_commands` is the default list minus `whoami`, so the blocked
`whoami` test proves the override reaches the server.
"""

import json

from tests.integration.mcp_container import (
    OPENSHIFT_PATH,
    McpIntegrationTest,
    default_allowed_commands_without,
)


class OpenShiftReadOnlyTests(McpIntegrationTest):
    openstack_enabled = False
    openshift_enabled = True
    openshift_allow_write = False
    openshift_allowed_commands = default_allowed_commands_without("whoami")
    static_creds = True

    async def test_get_pod_with_and_without_prefix(self):
        with_prefix = await self.call_tool(
            OPENSHIFT_PATH, "oc get pod -n openshift-apiserver -o name"
        )
        without_prefix = await self.call_tool(
            OPENSHIFT_PATH, "get pod -n openshift-apiserver -o name"
        )
        self.assertTrue(with_prefix.strip().startswith("pod/"))
        self.assertEqual(with_prefix, without_prefix)

    async def test_get_pod_json_output_is_parseable(self):
        output = await self.call_tool(OPENSHIFT_PATH, "get pod -n openstack -o json")
        # oc wraps multiple objects in a kind "List" with an items array
        pods = json.loads(output)
        self.assertIsInstance(pods["items"], list)

    async def test_whoami_is_blocked_by_the_override(self):
        await self.assert_tool_error(OPENSHIFT_PATH, "whoami")

    async def test_write_commands_are_rejected(self):
        await self.assert_tool_error(
            OPENSHIFT_PATH, "delete pod nova-api-0 -n openstack"
        )
        await self.assert_tool_error(OPENSHIFT_PATH, "create namespace itest")

    async def test_unknown_command_is_rejected(self):
        await self.assert_tool_error(OPENSHIFT_PATH, "kubectl list")

    async def test_forbidden_global_arguments_are_rejected(self):
        test_command = "get pod -n openstack "
        rejected = (
            test_command + "--token some-token",
            test_command + "--server https://api.example:6443",
            test_command + "--kubeconfig /tmp/kubeconfig",
            test_command + "-s https://api.example:6443",
        )
        for command in rejected:
            with self.subTest(command=command):
                await self.assert_tool_error(OPENSHIFT_PATH, command)

    async def test_shell_commands_fail(self):
        await self.assert_tool_error(OPENSHIFT_PATH, "curl http://example.com")

    async def test_pipes_fail(self):
        await self.assert_tool_error(OPENSHIFT_PATH, "get pod -n openstack | grep nova")

    async def test_bare_oc_command_is_rejected(self):
        # Rejected because the tool would just return the help
        await self.assert_tool_error(OPENSHIFT_PATH, "oc")

    async def test_tools_list_only_contains_openshift_cli(self):
        result = await self.rpc(OPENSHIFT_PATH, "tools/list")
        self.assertEqual([tool["name"] for tool in result["tools"]], ["openshift-cli"])

    async def test_openstack_endpoint_is_not_mounted(self):
        # We are running with `openstack_enabled = False` so OpenShift CLI tool should not be there`
        response = await self.raw_rpc("/openstack/", "tools/list")
        self.assertEqual(response.status_code, 404)

    async def test_concurrent_calls_beat_serial_execution(self):
        await self.assert_concurrent(OPENSHIFT_PATH, "get pod -n openshift-apiserver")
