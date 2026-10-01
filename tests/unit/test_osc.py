import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from mcp.server.mcpserver.exceptions import ToolError

from rhos_ls_mcps import osc, settings, utils


def context(headers=None, meta=None):
    return SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers=headers or {}), meta=meta
        )
    )


class RejectedEntryPointTests(unittest.TestCase):
    def test_writes_an_explanation_and_stops_execution(self):
        stderr = io.StringIO()
        entry = osc.RejectedEntryPoint(
            "server_delete", "package:Delete", "openstack.cli", stderr
        )
        with self.assertRaises(SystemError):
            entry.load()
        self.assertIn("currently blocked", stderr.getvalue())

    def test_repr_identifies_the_rejected_entry_point(self):
        entry = osc.RejectedEntryPoint(
            "server_delete", "package:Delete", "openstack.cli", io.StringIO()
        )
        self.assertIn("server_delete", repr(entry))


class CommandManagerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = settings.Settings()
        self.enterContext(patch.object(settings, "CONFIG", self.config))
        self.enterContext(patch.object(osc, "ALLOWED_COMMANDS", ["server_list_"]))
        self.manager = object.__new__(osc.MyCommandManager)

    def test_allows_allow_listed_commands_in_read_only_mode(self):
        self.assertTrue(self.manager._is_command_allowed(["server", "list"]))

    def test_blocks_unlisted_commands_in_read_only_mode(self):
        self.assertFalse(self.manager._is_command_allowed(["server", "delete"]))

    def test_allows_commands_when_write_mode_is_enabled(self):
        self.config.openstack.allow_write = True
        self.assertTrue(self.manager._is_command_allowed(["server", "delete"]))

    def test_requires_stderr_buffer(self):
        with self.assertRaisesRegex(ToolError, "stderr is required"):
            osc.MyCommandManager("openstack.cli")


class SplitCommandTests(unittest.TestCase):
    def test_removes_openstack_prefix(self):
        self.assertEqual(
            osc.split_command(" openstack server list ", context()),
            ["server", "list"],
        )

    def test_honors_quoted_arguments(self):
        self.assertEqual(
            osc.split_command("server list --name 'a b'", context()),
            ["server", "list", "--name", "a b"],
        )

    def test_rejects_empty_command(self):
        with self.assertRaisesRegex(ToolError, "No command"):
            osc.split_command("", context())

    def test_rejects_interactive_mode(self):
        with self.assertRaisesRegex(ToolError, "interactive"):
            osc.split_command("openstack", context())


class ResponseCleaningTests(unittest.TestCase):
    def test_removes_leading_null_bytes(self):
        self.assertEqual(osc._clean_response("\x00\x00result"), "result")


class OpenStackCredentialTests(unittest.TestCase):
    def test_uses_token_and_url_headers(self):
        self.assertEqual(
            osc.get_osp_credentials_args(
                context({"OS_TOKEN": "Bearer token", "OS_URL": "https://api"})
            ),
            ["--os-token", "token", "--os-url", "https://api"],
        )

    def test_uses_credential_files_when_headers_are_absent(self):
        with patch.object(osc.os.path, "exists", side_effect=[True, True]):
            self.assertEqual(osc.get_osp_credentials_args(context()), [])

    def test_rejects_missing_headers_and_credential_files(self):
        with (
            patch.object(osc.os.path, "exists", return_value=False),
            self.assertRaisesRegex(ToolError, "Missing OpenStack credentials"),
        ):
            osc.get_osp_credentials_args(context())


class PluginDiscoveryTests(unittest.TestCase):
    def test_formats_known_and_unknown_plugins(self):
        entries = [SimpleNamespace(name="metric"), SimpleNamespace(name="custom")]
        with patch("importlib.metadata.entry_points", return_value=entries):
            self.assertEqual(
                osc.get_installed_plugins(),
                "\nInstalled extra plugins:\n- AODH (metrics)\n- custom",
            )

    def test_returns_empty_string_without_plugins(self):
        with patch("importlib.metadata.entry_points", return_value=[]):
            self.assertEqual(osc.get_installed_plugins(), "")

    def test_classifies_allowed_and_disallowed_commands(self):
        allowed = SimpleNamespace(name="server_list")
        blocked = SimpleNamespace(name="server_delete")
        with patch.object(
            osc,
            "entry_points",
            return_value=SimpleNamespace(
                groups=["openstack.cli", "other"],
                select=Mock(return_value=[allowed, blocked]),
            ),
        ):
            commands, rejected = osc.osp_list_commands({"list"})
        self.assertEqual(commands, ["server_list_"])
        self.assertEqual(rejected, ["server_delete_"])


class MetricStorageErrorTests(unittest.TestCase):
    def test_returns_none_for_unrelated_error(self):
        self.assertIsNone(osc._metric_storage_error("some unrelated failure"))

    def test_detects_prometheus_client_failure(self):
        stderr = (
            "ERROR Failed to configure Prometheus client. Aetos discovery from "
            "keystone failed: 'public endpoint for metric-storage service ...'"
        )
        message = osc._metric_storage_error(stderr)
        self.assertIsNotNone(message)
        self.assertIn("metric-storage", message)
        self.assertIn("openstack.prometheus", message)

    def test_detects_metric_storage_mention(self):
        self.assertIsNotNone(
            osc._metric_storage_error("public endpoint for metric-storage not found")
        )


class ConfigurePrometheusEnvTests(unittest.TestCase):
    def _configure(self, prometheus, environ):
        config = settings.Settings(openstack={"prometheus": prometheus})
        with (
            patch.object(settings, "CONFIG", config),
            patch.dict(osc.os.environ, environ, clear=True),
        ):
            osc._configure_prometheus_env()
            return dict(osc.os.environ)

    def test_exports_config_values(self):
        env = self._configure(
            {"host": "h", "port": 9090, "ca_cert": "/ca", "root_path": "/p"}, {}
        )
        self.assertEqual(env["PROMETHEUS_HOST"], "h")
        self.assertEqual(env["PROMETHEUS_PORT"], "9090")
        self.assertEqual(env["PROMETHEUS_CA_CERT"], "/ca")
        self.assertEqual(env["PROMETHEUS_ROOT_PATH"], "/p")

    def test_existing_env_is_not_overridden(self):
        env = self._configure({"host": "cfg"}, {"PROMETHEUS_HOST": "preexisting"})
        self.assertEqual(env["PROMETHEUS_HOST"], "preexisting")

    def test_unset_values_are_absent(self):
        env = self._configure({"host": "h"}, {})
        self.assertNotIn("PROMETHEUS_PORT", env)
        self.assertNotIn("PROMETHEUS_ROOT_PATH", env)


class OpenStackShellHelperTests(unittest.TestCase):
    def test_maps_service_type_to_api_argument_name(self):
        self.assertEqual(
            osc.MyOpenStackShell._get_version_arg_name_from_service_type(
                "block-storage"
            ),
            "os_volume_api_version",
        )
        self.assertEqual(
            osc.MyOpenStackShell._get_version_arg_name_from_service_type("my-service"),
            "os_my_service_api_version",
        )

    def test_clears_standard_stream_buffers(self):
        shell = SimpleNamespace(stdout=io.StringIO("out"), stderr=io.StringIO("err"))
        osc.MyOpenStackShell._clean_stds(shell)
        self.assertEqual(shell.stdout.getvalue(), "")
        self.assertEqual(shell.stderr.getvalue(), "")

    def test_forbidden_parser_argument_raises_tool_error(self):
        with self.assertRaisesRegex(ToolError, "forbidden global"):
            osc.MyOpenStackShell._fail_on_argument("secret")


class OpenStackShellInitializationTests(unittest.IsolatedAsyncioTestCase):
    async def test_marks_forbidden_global_options_with_fail_type(self):
        parser = MagicMock()
        parser._actions = [
            SimpleNamespace(option_strings=["--os-cloud"], type=None, default="x")
        ]
        shell = SimpleNamespace(
            parser=parser, _fail_on_argument=osc.MyOpenStackShell._fail_on_argument
        )
        await osc.MyOpenStackShell._initialize_global_args(shell, [])
        self.assertEqual(parser._actions[0].type, "fail")
        self.assertIsNone(parser._actions[0].default)

    async def test_sets_current_service_versions_as_parser_defaults(self):
        parser = MagicMock()
        shell = SimpleNamespace(
            parser=parser,
            _do_run=Mock(
                return_value=(
                    0,
                    '\x00[{"Status":"CURRENT","Service Type":"identity","Max Microversion":"3.14","Version":null},{"Status":"CURRENT","Service Type":"block-storage","Max Microversion":null,"Version":"3"}]',
                    "",
                )
            ),
            _get_version_arg_name_from_service_type=(
                osc.MyOpenStackShell._get_version_arg_name_from_service_type
            ),
        )
        await osc.MyOpenStackShell._initialize_api_versions(shell, ["--os-token", "t"])
        parser.set_defaults.assert_called_once_with(
            os_identity_api_version="3", os_volume_api_version="3"
        )

    async def test_reports_api_version_discovery_failure(self):
        shell = SimpleNamespace(
            _do_run=Mock(return_value=(1, "out", "err")), parser=MagicMock()
        )
        with self.assertRaisesRegex(ToolError, "Failed to get API versions"):
            await osc.MyOpenStackShell._initialize_api_versions(shell, [])

    async def test_initializes_api_versions_and_global_args_once(self):
        shell = SimpleNamespace(
            initialized=False,
            lock=asyncio.Lock(),
            _initialize_api_versions=AsyncMock(),
            _initialize_global_args=AsyncMock(),
        )
        await osc.MyOpenStackShell._initialize_parser(shell, ["a"], ["b"])
        shell._initialize_api_versions.assert_awaited_once_with(["a"])
        shell._initialize_global_args.assert_awaited_once_with(["b"])


class OpenStackShellAsyncExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_command_in_executor_after_initialization(self):
        executor = SimpleNamespace(run_function=AsyncMock(return_value=(0, "out", "")))
        shell = SimpleNamespace(
            _initialize_parser=AsyncMock(), stdout=io.StringIO(), stderr=io.StringIO()
        )
        with patch.object(utils, "EXECUTOR", executor):
            self.assertEqual(
                await osc.MyOpenStackShell.run(
                    shell, ["--os-token", "t"], ["server", "list"]
                ),
                (0, "out", ""),
            )

    async def test_rejects_protected_runtime_arguments(self):
        shell = SimpleNamespace(
            _initialize_parser=AsyncMock(), stdout=io.StringIO(), stderr=io.StringIO()
        )
        with self.assertRaisesRegex(ToolError, "not allowed"):
            await osc.MyOpenStackShell.run(
                shell, [], ["--os-token=override", "server", "list"]
            )


class OpenStackInitializationTests(unittest.TestCase):
    def test_registers_tool_and_configures_tls_options(self):
        config = settings.Settings(openstack={"ca_cert": "ca.pem", "insecure": True})
        server = MagicMock()
        params = []
        with (
            patch.object(settings, "CONFIG", config),
            patch.object(osc, "OSC_PARAMS", params),
            patch.object(osc, "ALLOWED_COMMANDS", []),
            patch.object(osc, "osp_list_commands", return_value=(["server_list_"], [])),
        ):
            osc.initialize(server)
        server.add_tool.assert_called_once()
        self.assertEqual(params, ["--os-cacert", "ca.pem", "--insecure"])


class ShellExecutionTests(unittest.TestCase):
    def setUp(self):
        self.shell = object.__new__(osc.MyOpenStackShell)
        self.shell.stdout = io.StringIO()
        self.shell.stderr = io.StringIO()

    def test_captures_successful_shell_output(self):
        with patch.object(osc.osc_shell.OpenStackShell, "run", return_value=0):
            self.assertEqual(self.shell._do_run(["server", "list"]), (0, "", ""))

    def test_converts_system_exit_to_return_code(self):
        with patch.object(
            osc.osc_shell.OpenStackShell, "run", side_effect=SystemExit(7)
        ):
            self.assertEqual(self.shell._do_run(["server", "list"]), (7, "", ""))

    def test_run_shell_command_delegates_to_global_shell(self):
        shell = SimpleNamespace(_do_run=lambda command: (0, "out", ""))
        with patch.object(osc, "SHELL", shell):
            self.assertEqual(osc.run_shell_cmd(["server", "list"]), (0, "out", ""))


class OpenStackToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_stdout_from_successful_command(self):
        shell = SimpleNamespace(run=AsyncMock(return_value=(0, "output", "warning")))
        with patch.object(osc, "SHELL", shell), patch.object(osc, "OSC_PARAMS", []):
            self.assertEqual(
                await osc.openstack_cli_mcp_tool(
                    "server list", context({"OS_TOKEN": "t", "OS_URL": "u"})
                ),
                "output",
            )

    async def test_reports_command_failure(self):
        shell = SimpleNamespace(run=AsyncMock(return_value=(2, "", "failure")))
        with (
            patch.object(osc, "SHELL", shell),
            patch.object(osc, "OSC_PARAMS", []),
            self.assertLogs("rhos_ls_mcps.logging", level="ERROR") as logs,
            self.assertRaisesRegex(ToolError, "error code 2"),
        ):
            await osc.openstack_cli_mcp_tool(
                "server list", context({"OS_TOKEN": "t", "OS_URL": "u"})
            )
        self.assertIn("error code 2", logs.output[0])
