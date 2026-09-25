import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from mcp.server.mcpserver.exceptions import ToolError

from rhos_ls_mcps import osc, settings, utils


def context(headers=None):
    return SimpleNamespace(
        request_context=SimpleNamespace(request=SimpleNamespace(headers=headers or {}))
    )


class OpenStackClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = settings.Settings()
        self.enterContext(patch.object(settings, "CONFIG", self.config))
        self.enterContext(patch.object(osc, "SHELL", None))
        self.enterContext(patch.object(osc, "OSC_PARAMS", []))
        self.enterContext(patch.object(osc, "ALLOWED_COMMANDS", ["server_list_"]))

    def test_command_and_response_helpers(self):
        self.assertEqual(osc._clean_response("\x00\x00result"), "result")
        self.assertEqual(
            osc.split_command(" openstack server list ", context()), ["server", "list"]
        )
        self.assertEqual(
            osc.split_command("server list --name 'a b'", context()),
            ["server", "list", "--name", "a b"],
        )
        for command, message in (("", "No command"), ("openstack", "interactive")):
            with (
                self.subTest(command=command),
                self.assertRaisesRegex(ToolError, message),
            ):
                osc.split_command(command, context())

    def test_credentials_prefer_headers_then_known_files(self):
        self.assertEqual(
            osc.get_osp_credentials_args(
                context({"OS_TOKEN": "Bearer token", "OS_URL": "https://api"})
            ),
            ["--os-token", "token", "--os-url", "https://api"],
        )
        with patch.object(osc.os.path, "exists", side_effect=[True, True]):
            self.assertEqual(osc.get_osp_credentials_args(context()), [])
        with (
            patch.object(osc.os.path, "exists", return_value=False),
            self.assertRaisesRegex(ToolError, "Missing OpenStack credentials"),
        ):
            osc.get_osp_credentials_args(context())

    async def test_openstack_tool_returns_output_and_reports_errors(self):
        shell = SimpleNamespace(run=AsyncMock(return_value=(0, "output", "warning")))
        with patch.object(osc, "SHELL", shell):
            self.assertEqual(
                await osc.openstack_cli_mcp_tool(
                    "server list", context({"OS_TOKEN": "t", "OS_URL": "u"})
                ),
                "output",
            )
        shell.run.return_value = (2, "", "failure")
        with (
            patch.object(osc, "SHELL", shell),
            self.assertRaisesRegex(ToolError, "error code 2"),
        ):
            await osc.openstack_cli_mcp_tool(
                "server list", context({"OS_TOKEN": "t", "OS_URL": "u"})
            )

    def test_plugins_and_entry_point_filtering(self):
        entries = [SimpleNamespace(name="metric"), SimpleNamespace(name="custom")]
        with patch("importlib.metadata.entry_points", return_value=entries):
            self.assertEqual(
                osc.get_installed_plugins(),
                "\nInstalled extra plugins:\n- AODH (metrics)\n- custom",
            )
        with patch("importlib.metadata.entry_points", return_value=[]):
            self.assertEqual(osc.get_installed_plugins(), "")

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

    def test_rejected_entry_point_and_command_policy(self):
        stderr = io.StringIO()
        entry = osc.RejectedEntryPoint(
            "server_delete", "package:Delete", "openstack.cli", stderr
        )
        with self.assertRaises(SystemError):
            entry.load()
        self.assertIn("currently blocked", stderr.getvalue())
        self.assertIn("server_delete", repr(entry))

        manager = object.__new__(osc.MyCommandManager)
        self.config.openstack.allow_write = False
        self.assertTrue(manager._is_command_allowed(["server", "list"]))
        self.assertFalse(manager._is_command_allowed(["server", "delete"]))
        self.config.openstack.allow_write = True
        self.assertTrue(manager._is_command_allowed(["server", "delete"]))
        with self.assertRaisesRegex(ToolError, "stderr is required"):
            osc.MyCommandManager("openstack.cli")

    def test_shell_initialization_and_helper_methods(self):
        shell = osc.MyOpenStackShell()
        self.assertEqual(shell.NAME, "openstack")
        self.assertEqual(
            shell._get_version_arg_name_from_service_type("block-storage"),
            "os_volume_api_version",
        )
        self.assertEqual(
            shell._get_version_arg_name_from_service_type("my-service"),
            "os_my_service_api_version",
        )
        shell.stdout.write("out")
        shell.stderr.write("err")
        shell._clean_stds()
        self.assertEqual(shell.stdout.getvalue(), "")
        self.assertEqual(shell.stderr.getvalue(), "")
        with self.assertRaisesRegex(ToolError, "forbidden global"):
            shell._fail_on_argument("secret")
        with patch.object(osc.osc_shell.OpenStackShell, "run", return_value=0):
            self.assertEqual(shell._do_run(["server", "list"]), (0, "", ""))
        with patch.object(
            osc.osc_shell.OpenStackShell, "run", side_effect=SystemExit(7)
        ):
            self.assertEqual(shell._do_run(["server", "list"]), (7, "", ""))
        with (
            patch.object(shell, "_do_run", return_value=(0, "out", "")),
            patch.object(osc, "SHELL", shell),
        ):
            self.assertEqual(osc.run_shell_cmd(["server", "list"]), (0, "out", ""))

    async def test_shell_parser_and_api_version_initialization(self):
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

        shell._do_run = Mock(
            return_value=(
                0,
                '\x00[{"Status": "CURRENT", "Service Type": "identity", "Max Microversion": "3.14", "Version": null}, {"Status": "CURRENT", "Service Type": "block-storage", "Max Microversion": null, "Version": "3"}, {"Status": "OLD", "Service Type": "ignored", "Max Microversion": "1", "Version": null}]',
                "",
            )
        )
        shell._get_version_arg_name_from_service_type = (
            osc.MyOpenStackShell._get_version_arg_name_from_service_type
        )
        await osc.MyOpenStackShell._initialize_api_versions(shell, ["--os-token", "t"])
        parser.set_defaults.assert_called_once_with(
            os_identity_api_version="3", os_volume_api_version="3"
        )
        shell._do_run.return_value = (1, "out", "err")
        with self.assertRaisesRegex(ToolError, "Failed to get API versions"):
            await osc.MyOpenStackShell._initialize_api_versions(shell, [])

        initialized_shell = SimpleNamespace(
            initialized=False,
            lock=asyncio.Lock(),
            _initialize_api_versions=AsyncMock(),
            _initialize_global_args=AsyncMock(),
        )
        await osc.MyOpenStackShell._initialize_parser(initialized_shell, ["a"], ["b"])
        initialized_shell._initialize_api_versions.assert_awaited_once_with(["a"])
        initialized_shell._initialize_global_args.assert_awaited_once_with(["b"])

    async def test_shell_runs_with_protected_arguments(self):
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
        with self.assertRaisesRegex(ToolError, "not allowed"):
            await osc.MyOpenStackShell.run(
                shell, [], ["--os-token=override", "server", "list"]
            )
