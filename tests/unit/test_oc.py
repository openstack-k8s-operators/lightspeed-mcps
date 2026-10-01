import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp.server.mcpserver.exceptions import ToolError

from rhos_ls_mcps import oc, settings, utils


class OpenShiftCommandValidationTests(unittest.TestCase):
    def setUp(self):
        self.config = settings.Settings(
            openshift={
                "allowed_commands": ["get", "adm top"],
                "blocked_commands": ["delete", "adm policy remove-group"],
            }
        )
        self.enterContext(patch.object(settings, "CONFIG", self.config))
        self.enterContext(patch.object(oc, "MAX_ALLOW_COMMAND_WORDS", 2))
        self.enterContext(patch.object(oc, "MAX_BLOCK_COMMAND_WORDS", 3))

    def test_counts_words_in_longest_command(self):
        self.assertEqual(oc.max_command_words(["get", "adm policy remove-group"]), 3)

    def test_accepts_allowed_command_and_removes_oc_prefix(self):
        self.assertEqual(oc.validate_command("oc get pods"), ["get", "pods"])

    def test_rejects_empty_command(self):
        with self.assertRaisesRegex(ToolError, "No command"):
            oc.validate_command("oc")

    def test_rejects_write_command_in_read_only_mode(self):
        with self.assertRaisesRegex(ToolError, "currently blocked"):
            oc.validate_command("delete pod example")

    def test_rejects_protected_connection_argument(self):
        with self.assertRaisesRegex(ToolError, "not allowed"):
            oc.validate_command("get pods --token=override")

    def test_allows_non_blocked_commands_in_write_mode(self):
        self.config.openshift.allow_write = True
        self.assertTrue(oc._is_command_allowed(["create", "configmap", "example"]))

    def test_blocks_explicitly_blocked_commands_in_write_mode(self):
        self.config.openshift.allow_write = True
        self.assertFalse(oc._is_command_allowed(["adm", "policy", "remove-group"]))


def context(headers):
    return SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers=headers), meta=None
        )
    )


class OpenShiftCredentialTests(unittest.TestCase):
    def test_returns_token_and_server_from_headers(self):
        self.assertEqual(
            oc.get_ocp_credentials_args(
                context({"OCP_TOKEN": "Bearer token", "OCP_URL": "https://api"})
            ),
            ["--token", "token", "--server", "https://api"],
        )

    def test_omits_missing_credentials(self):
        self.assertEqual(oc.get_ocp_credentials_args(context({})), [])


class OpenShiftToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = settings.Settings(openshift={"allowed_commands": ["get"]})
        self.enterContext(patch.object(settings, "CONFIG", self.config))
        self.enterContext(patch.object(oc, "OC_PARAMS", ["oc"]))
        self.enterContext(patch.object(oc, "MAX_ALLOW_COMMAND_WORDS", 1))

    async def test_returns_stdout_from_successful_command(self):
        executor = SimpleNamespace(run_command=AsyncMock(return_value=(0, "pods", "")))
        with patch.object(utils, "EXECUTOR", executor):
            self.assertEqual(
                await oc.openshift_cli_mcp_tool("get pods", context({})), "pods"
            )

    async def test_raises_tool_error_for_failed_command(self):
        executor = SimpleNamespace(
            run_command=AsyncMock(return_value=(1, "", "failed"))
        )
        with (
            patch.object(utils, "EXECUTOR", executor),
            self.assertLogs("rhos_ls_mcps.logging", level="ERROR") as logs,
            self.assertRaisesRegex(ToolError, "error code 1"),
        ):
            await oc.openshift_cli_mcp_tool("get pods", context({}))
        self.assertIn("error code 1", logs.output[0])
