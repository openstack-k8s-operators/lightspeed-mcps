"""
# openstack CLI MCP tool

## Features:

- Run `openstack` commands as if they were run in a terminal
- Uses the OpenStackClient (OSC) code as a libray to simulate running commands from the command line
- Supported authentication mechanisms:
  * Config files on standard locations `clouds.yaml` and `secure.yaml`)
  * `OS_TOKEN` and `OS_URL` passed as MCP request headers
- SSL:
  * Support configuring insecure mode (`--osc-insecure`)
  * Explicit local certificates (`--osc-ca-cert`)
- Uses a whitelist mechanism to accept only those commands when running in read only mode
- Allows all commands when running in read/write mode (`--osc-allow-write`)
- Client defaults to the latest microversion for each service, but can be overridden by the caller by passing the appropriate `--os-XXXX-api-version` parameter

## DEV LINKS:
- https://github.com/openstack/python-openstackclient/blob/master/openstackclient/shell.py
- https://github.com/openstack/osc-lib/blob/master/osc_lib/shell.py
"""

import asyncio
from importlib.metadata import entry_points, EntryPoint
import io
import json
import logging
import os
import shlex
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from cliff import interactive

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
import openstackclient.shell as osc_shell

from rhos_ls_mcps import settings
from rhos_ls_mcps.logging import tool_logger
from rhos_ls_mcps import utils


logger = logging.getLogger(__name__)


# fmt: off
ACCEPT_COMMANDS: set[str] = {
    # These are just verbs
    "get", "show", "list", "history", "alarm-history show", "alarm-history search",
    "capabilities list", "alarm show", "alarm quota show", "alarm state get",
    "search", "benchmark metric show", "alarming capabilities list", "simulate",
    "info", "collect", "benchmark measures show", "validate", "ping", "top",
    "stats", "alarm list", "contains", "homedoc", "query", "measures aggregation",
    "tail", "versions", "count",

    # These are full names
    "stack_resource_metadata", "database_configuration_default", "metric_aggregates",
    "optimize_strategy_state", "rca_status", "volume_summary", "stack_hook_poll",
    "database_configuration_instances",  "alarm metrics", "stack_check",
    "cluster_check", "baremetal_introspection_status", "rca_healthcheck",
    "appcontainer_logs", "appcontainer_quota_default", "metric_status",
    "metric_server_version", "messaging_health", "database_cluster_modules",
    "class-schema",
}
# fmt: on

# These are global arguments that the user nor us can pass, so we remove them.
DELETE_GLOBAL_ARGS: list[str] = [
    "--os-cloud",
    "--os-cert",
    "--os-key",
    "--verify",
    "--os-interface",
    "--os-profile",
    "--murano-url",
    "--glare-url",
    "--inspector-url",
    "--os-data-processing-url",
    "--os-username",
    "--os-password",
    "--os-endpoint",
    "--os-trust-id",
    "--os-identity-provider",
    "--os-client-secret",
    "--os-openid-scope",
    "--os-access-token-endpoint",
    "--os-discovery-endpoint",
    "--os-access-token-type",
    "--os-redirect-uri",
    "--os-aodh-endpoint",
    "--os-application-credential-secret",
    "--os-application-credential-id",
    "--os-application-credential-name",
    "--os-code-challenge-method",
    "--os-access-token",
    "--os-consumer-key",
    "--os-consumer-secret",
    "--os-idp-otp-key",
    "--os-realm-name",
    "--os-openid-client-id",
    "--os-auth-type",
    "--os-oauth2-endpoint",
    "--os-oauth2-client-id",
    "--os-oauth2-client-secret",
    "--os-device-authorization-endpoint",
    "--os-auth-methods",
    "--os-user",
    "--os-passcode",
]

# These are global arguments that the user cannot pass but that we cannot remove because
# we use them in the code.
REJECT_GLOBAL_ARGS: list[str] = [
    "--os-auth-url",
    "--os-token",
    "--insecure",
    "--os-cacert",
]

SHELL = None
ALLOWED_COMMANDS: list[str] = []
OSC_PARAMS: list[str] = []


##########
# METHODS AND CLASSES CALLED FROM main.py


def initialize(mcp_osp: FastMCP):
    global ALLOWED_COMMANDS, OSC_PARAMS

    mcp_osp.add_tool(
        openstack_cli_mcp_tool, name="openstack-cli", title="OpenStack Client MCP Tool"
    )

    if settings.CONFIG.openstack.ca_cert:
        OSC_PARAMS.extend(["--os-cacert", settings.CONFIG.openstack.ca_cert])

    if settings.CONFIG.openstack.insecure:
        OSC_PARAMS.append("--insecure")

    ALLOWED_COMMANDS = osp_list_commands(ACCEPT_COMMANDS)[0]


##########
# MCP TOOLS AND SUPPORTING METHODS


def _clean_response(response: str) -> str:
    """Clear the response to remove 0x00 characters at the start."""
    return response.lstrip("\x00")


@tool_logger
async def openstack_cli_mcp_tool(command_str: str, ctx: Context) -> str:
    """Run an OpenStackClient (OSC) CLI command

    Runs the `openstack` command as if it were run in a terminal.
    No need to provide credentials, they are already present.

    The `openstack` command replaces individual commands for example:
    - `cinder volume-list` is now `openstack volume list`
    - `glance image-list` is now `openstack image list`
    - `nova list` is now `openstack server list`

    DON'T EVER USE commands such as cinder, nova, glance. Use `openstack` instead.

    A complete list of commands is available using the help commands:
    - Global options and supported commands: `openstack --help` or `--help`
    - Options for a specific command:
      * `openstack <command> --help`
      * `openstack help <command>`

    Microversions default to latest version, can use older version with
    appropriate `--os-XXXX-api-version`parameter (eg:
    `--os-identity-api-version 3.26`)

    For specific format of the stdout result use
    `--format {table,csv,json,value,yaml}` (default: is table)

    Empty lists output depends on the format:
    - CSV: always have a headers line, when there are no elements that's the only line.
    - JSON: empty array []
    - Table: nothing

    Args:
       command_str: String with the openstack command to run. May start with "openstack"
                    or not, but it will *NEVER* be cinder, nova, glance, etc.
    """
    # TODO: Actually implement our own shell so we don't reload plugins and commands every time?
    #       https://github.com/openstack/python-openstackclient/blob/master/openstackclient/shell.py
    #       Which inherits from: https://github.com/openstack/osc-lib/blob/master/osc_lib/shell.py
    global SHELL

    if not SHELL:
        SHELL = MyOpenStackShell()

    # Build the command arguments list for the openstack command
    mcp_argv = OSC_PARAMS + get_osp_credentials_args(ctx)
    user_argv = split_command(command_str, ctx)

    ret_value, stdout, stderr = await SHELL.run(mcp_argv, user_argv)

    # TODO; Redact values?
    result = {
        "stdout": stdout,
        "stderr": stderr,
    }

    if ret_value:
        raise ToolError(
            "openstack failed with error code {}: {}".format(ret_value, result)
        )

    return stdout or stderr


class MyOpenStackShell(osc_shell.OpenStackShell):
    """OpenStack shell implementation without a subprocess shell.

    Necessary because the osc shell doesn't accept stdin, stdout, and stderr as arguments,
    as it assumes it's always called from a terminal.  We need to capture the command's
    stdout and stderr to return them to the MCP client.

    Also ensures that plugins and commands are loaded only once.
    """

    # Class variables shared by all instances
    initialized: bool = False
    # TODO: Figure out why we need to reload everytime otherwise the commands dissapear and we fail
    loaded_plugins: bool = False
    loaded_commands: bool = False

    def __init__(
        self,
        description: str | None = None,
        version: str | None = None,
        interactive_app_factory: type["interactive.InteractiveApp"] | None = None,
        deferred_help: Optional[bool] = None,
    ) -> None:
        stderr: io.StringIO = io.StringIO()
        stdout: io.StringIO = io.StringIO()

        description = description or osc_shell.__doc__.strip()
        version = version or osc_shell.openstackclient.__version__
        # Our custom command manager blocks commands that are not allowed
        command_manager = MyCommandManager("openstack.cli", stderr=stderr)
        deferred_help = True if deferred_help is None else deferred_help

        super(osc_shell.OpenStackShell, self).__init__(
            description=description,
            version=version,
            command_manager=command_manager,
            stdin=None,
            stdout=stdout,
            stderr=stderr,
            interactive_app_factory=interactive_app_factory,
            deferred_help=deferred_help,
        )

        self.NAME = "openstack"

        self.api_version = {}

        # ?: This doesn't seem to be used
        self.verify = True

        # ignore warnings from openstacksdk since our users can't do anything
        # about them
        osc_shell.warnings.filterwarnings("ignore", module="openstack")

        self.lock = asyncio.Lock()

    def configure_logging(self) -> None:
        """Configure logging for the OpenStack shell and cliff app."""
        super().configure_logging()

        # We need to change the LOG variable to make sure it uses the right stderr instead of sys.stderr
        console = logging.StreamHandler(self.stderr)
        console_level = logging.DEBUG if settings.CONFIG.debug else logging.INFO
        console.setLevel(console_level)
        # We don't use self.CONSOLE_MESSAGE_FORMAT so we don't include the python module in the description:
        formatter = logging.Formatter("%(levelname)s %(message)s")
        console.setFormatter(formatter)
        self.LOG = logging.getLogger("cliff.app")
        self.LOG.addHandler(console)

    # TODO: Figure out why we need to reload everytime otherwise the commands dissapear and we fail
    def _load_plugins(self) -> None:
        """Only load plugins once."""
        if not self.loaded_plugins:
            super()._load_plugins()
            MyOpenStackShell.loaded_plugins = True

    def _load_commands(self) -> None:
        """Only load commands once."""
        if not self.loaded_commands:
            super()._load_commands()
            MyOpenStackShell.loaded_commands = True

    # # From https://specs.openstack.org/openstack/service-types-authority/_downloads/e1997ad174a98e6705a285ae2a24dff8/service-types.yaml
    @staticmethod
    def _get_version_arg_name_from_service_type(service_type: str) -> str:
        API_NAME_MAPPING: dict[str, str] = {
            "block-storage": "volume",
            "volumev3": "volume",
            "volumev2": "volume",
            "metric-storage": "metric",
            "operator-policy": "congressclient",
            "alarm": "alarming",  # alarming is also an alias that doesn't need to be mapped
            "resource-cluster": "clustering",  # Clustering is the real service type and doesn't need to be mapped
            "cluster": "clustering",
            "application-container": "container",
            "message": "messaging",  # messaging is also an alias that doesn't need to be mapped
            "resource-optimization": "infra-optim",
            "root-cause-analysis": "rca",  # rca is also an alias that doesn't need to be mapped
            "workflow": "workflow_engine",
            "workflowv2": "workflow_engine",
        }

        api_name = API_NAME_MAPPING.get(service_type, service_type.replace("-", "_"))
        return f"os_{api_name}_api_version"

    def _clean_stds(self) -> None:
        """Clean the stdout and stderr buffers."""
        self.stdout.seek(0)
        self.stdout.truncate(0)
        self.stderr.seek(0)
        self.stderr.truncate(0)

    async def _initialize_parser(
        self, mcp_argv: list[str], user_argv: list[str]
    ) -> None:
        if self.initialized:
            return

        await self.lock.acquire()
        try:
            await self._initialize_api_versions(mcp_argv)
            await self._initialize_global_args(user_argv)
            MyOpenStackShell.initialized = True
        finally:
            self.lock.release()

    @staticmethod
    def _fail_on_argument(value: str) -> None:
        raise ToolError(f"A forbidden global argument was provided with value: {value}")

    async def _initialize_global_args(self, user_argv: list[str]) -> None:
        self.parser.register("type", "fail", self._fail_on_argument)

        delete_global_args = DELETE_GLOBAL_ARGS.copy()

        for arg in self.parser._actions:
            for option in arg.option_strings:
                if option in delete_global_args:
                    arg.type = "fail"
                    arg.default = None
                    delete_global_args.remove(option)

        if delete_global_args:
            logger.warning(
                f"The following global arguments were not removed: {delete_global_args}"
            )

    async def _initialize_api_versions(self, mcp_argv: list[str]) -> None:
        """Initialize the api_version dictionary with the latest API version for each service.

        This makes a call to OpenStack to get the versions.

        Args:
            mcp_argv: Argumments for credentials and certificates.
        """
        versions_varg = ["versions", "show", "--format", "json"]
        # Run in this process to later on share the loaded plugins and commands with command runs
        response, stdout, stderr = self._do_run(mcp_argv + versions_varg)
        if response:
            raise ToolError(
                f"Failed to get API versions ({response}):\n{stdout}\n{stderr}"
            )
        # For some reason stdout has 0x00 characters at the start, clean it
        api_versions = json.loads(_clean_response(stdout))

        version_defaults = {}

        for version_info in api_versions:
            # We only care about the latest API version
            if version_info["Status"] == "CURRENT":
                # Some services reportt microversions, others only report the version
                arg_name = self._get_version_arg_name_from_service_type(
                    version_info["Service Type"]
                )
                version = version_info["Max Microversion"] or version_info["Version"]
                # Keystone is weird, it reports 3.14 but doesn't accept it :-(
                if arg_name in (
                    "os_identity_api_version",
                    "os_key_manager_api_version",
                ):
                    version = version.split(".")[0]
                version_defaults[arg_name] = version

        # Change the default values for the api versions in the parser, that way
        # the user can override them if needed and we don't need to actually
        # pass them all on the command line.
        self.parser.set_defaults(**version_defaults)

    def _do_run(self, cmd: list[str]) -> tuple[int, str, str]:
        self._clean_stds()
        try:
            return_code = super().run(cmd)
        except (SystemExit, Exception) as e:
            return_code = getattr(e, "code", 1)
            msg = getattr(e, "msg", str(e))
            logger.debug(
                f"Failure running command: {cmd} with code: {return_code} and message: {msg}"
            )
        finally:
            stdout = self.stdout.getvalue()
            stderr = self.stderr.getvalue()
            self._clean_stds()
            return return_code, stdout, stderr

    async def run(
        self, mcp_argv: list[str], user_argv: list[str]
    ) -> tuple[int, str, str]:
        """Run the OpenStack shell.

        Ensures that the API versions are initialized to the latest version for each service.

        Args:
            mcp_argv: Argumments for credentials and certificates.
            user_argv: Arguments for the OpenStack command.
        """
        try:
            await self._initialize_parser(mcp_argv, user_argv)
            utils.reject_arguments(user_argv, REJECT_GLOBAL_ARGS)
            # Run in a separate process to allow concurrency
            return await utils.EXECUTOR.run_function(
                run_shell_cmd, mcp_argv + user_argv
            )
        except SystemExit as e:
            raise ToolError(
                f"OpenStack failed {e.code}: {self.stdout.getvalue() or self.stderr.getvalue()}"
            )


def run_shell_cmd(cmd: list[str]) -> tuple[int, str, str]:
    return SHELL._do_run(cmd)


def get_osp_credentials_args(ctx: Context) -> list[str]:
    """Get arguments for OpenStack credentials.

    Priority from highest to lowest:
    - `OS_TOKEN` and `OS_URL` in request headers
    - `clouds.yaml` and `secure.yaml` in
      * current directory
      * ~/.config/openstack
      * /etc/openstack

    Returns:
       list[str]: The arguments for the OpenStack credentials.
    Raises:
      - ToolError: If no credentials are found
    """

    # Check if we have connection information on the request headers
    headers = ctx.request_context.request.headers
    logger.debug(f"Headers: {headers}")

    token_header = utils.strip_bearer_prefix(headers.get("OS_TOKEN", ""))
    url_header = headers.get("OS_URL")
    if token_header and url_header:
        logger.debug(
            f"Using token and URL from request headers for credentials: {url_header}"
        )
        return ["--os-token", token_header, "--os-url", url_header]

    # Check that we actually have the credential files in a known location
    for config_dir in [
        "./",
        os.path.expanduser("~/.config/openstack"),
        "/etc/openstack",
    ]:
        if os.path.exists(os.path.join(config_dir, "clouds.yaml")) and os.path.exists(
            os.path.join(config_dir, "secure.yaml")
        ):
            logger.debug(
                f"Using clouds.yaml and secure.yaml from {config_dir} for credentials"
            )
            return []

    raise ToolError("Missing OpenStack credentials")


def split_command(command_str: str, ctx: Context) -> list[str]:
    """Basic command validation and return it as a list.

    Args:
       command_str: String with the openstack command to run.
                    May start with "openstack" or not, but cannot just be "openstack"
                    to prevent interactive mode.
       ctx: The MCP request context
    Returns:
       list[str]: The argv list without the initial "openstack".
    """
    command_str = command_str.strip()

    if not command_str:
        raise ToolError("No command provided")

    if command_str == "openstack":
        raise ToolError("openstack interactive mode is not available")

    # Use shlex to do a proper split (honoring quotes and escapes)
    argv = shlex.split(command_str)
    if argv[0] == "openstack":
        argv = argv[1:]
    return argv


def osp_list_commands(verbs: set[str]) -> tuple[list[str], list[str]]:
    """List commands that match and don't match the given verbs"""
    # Use sets because some commands exist in multiple groups
    # eg: dataprocessing_cluster_show exists in openstack.data_processing.v1 and
    # openstack.data_processing.v2
    result_commands: set[str] = set[str]()
    result_other_commands: set[str] = set[str]()

    all_entry_points = entry_points()
    for group in all_entry_points.groups:
        if group.startswith("openstack."):
            for ep in all_entry_points.select(group=group):
                name = ep.name
                cmd = name.split("_")
                if verbs.intersection(cmd) or name in verbs:
                    result_commands.add(name + "_")
                else:
                    result_other_commands.add(name + "_")
    return list(result_commands), list(result_other_commands)


##########
# CLASSES TO CONTROL ALLOWED COMMANDS AT RUNTIME
#
# Approach is to use a custom CommandManager that replaces entry points found
# that don't match our allowed commands with a custom EntryPoint class that
# rejects the request.
#
# This way we can differentiate between blocked and wrong commands efficiently
# since we don't have to check the command on each request.
class RejectedEntryPoint(EntryPoint):
    """Entry point that rejects the request."""

    # Parent is inmutable, so we have to define our additiona slots and then
    # bypass the protections on the immutable base to set additional
    # attributes using the __setattr__ method.
    __slots__ = ("stderr",)

    def __init__(self, name, value, group, stderr: io.StringIO):
        super().__init__(name, value, group)
        # Bypass protections on the immutable base to set additional attributes
        object.__setattr__(self, "stderr", stderr)

    def load(self) -> Any:
        """Load the entrypoint command replacing the action."""
        # Raise it on load instead of take_action to avoid concatenating exceptions (don't know why it happens)
        self.stderr.write(
            f"Command {self.name} is currently blocked for LLM use as it could modify the deployment."
        )
        raise SystemError(3)

    def __repr__(self):
        return (
            f"RejectedEntryPoint(name={self.name!r}, value={self.value!r}, "
            f"group={self.group!r})"
        )


class MyCommandManager(osc_shell.commandmanager.CommandManager):
    """Custom command manager to replace entry points for commands that are not allowed."""

    def __init__(self, *args, **kwargs):
        self.stderr: Optional[io.StringIO] = kwargs.pop("stderr", None)
        if not self.stderr:
            raise ToolError("stderr is required to initialize the command manager")
        super().__init__(*args, **kwargs)

    def load_commands(self, namespace: str) -> None:
        # Don't try to be smart and detect commands that were added since the
        # last load to only act on the new ones because when we do the API
        # discovery we load the commands twice, so they would already exist.
        super().load_commands(namespace)

        # Replace entry points for commands that are not allowed with our custom
        # class that rejects the request.
        for command, ep in self.commands.items():
            # Check agains EntryPoint instead of not being RejectedEntryPoint
            # because there's also EntryPointWrapper for commands such as help
            if isinstance(ep, EntryPoint) and not self._is_command_allowed(
                command.split()
            ):
                # Using `self.commands.pop(command)` would be simpler, but wouldn't let us differentiate
                # between blocked and wrong commands
                entry_point = self.commands[command]
                self.commands[command] = RejectedEntryPoint(
                    name=entry_point.name,
                    value=entry_point.value,
                    group=entry_point.group,
                    stderr=self.stderr,
                )

    def _is_command_allowed(self, argv: list[str]) -> bool:
        if settings.CONFIG.openstack.allow_write:
            return True
        user_cmd = "_".join(argv) + "_"
        return any(user_cmd.startswith(cmd) for cmd in ALLOWED_COMMANDS)
