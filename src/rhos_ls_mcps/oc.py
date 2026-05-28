import logging
import shlex

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from rhos_ls_mcps import settings
from rhos_ls_mcps.logging import tool_logger
from rhos_ls_mcps import utils


logger = logging.getLogger(__name__)

OC_PARAMS: list[str] = ["oc"]

MAX_ALLOW_COMMAND_WORDS = 3
MAX_BLOCK_COMMAND_WORDS = 3

# These are global arguments that the user cannot pass
# You can see the full list of available global arguments by running `oc options`
REJECT_GLOBAL_ARGS: list[str] = [
    "--cache-dir",
    "--certificate-authority",
    "--client-certificate",
    "--client-key",
    "--cluster",
    "--context",
    "--insecure-skip-tls-verify",
    "--kubeconfig",
    "--match-server-version",
    "--profile-output",
    "--profile",
    "-s",
    "--server",
    "--tls-server-name",
    "--token",
    "--user",
]


##########
# METHODS AND CLASSES CALLED FROM main.py


def initialize(mcp_ocp: FastMCP):
    global OC_PARAMS, MAX_ALLOW_COMMAND_WORDS, MAX_BLOCK_COMMAND_WORDS

    mcp_ocp.add_tool(
        openshift_cli_mcp_tool, name="openshift-cli", title="OpenShift Client MCP Tool"
    )

    if settings.CONFIG.openshift.insecure:
        OC_PARAMS.append("--insecure-skip-tls-verify=true")

    MAX_ALLOW_COMMAND_WORDS = max_command_words(
        settings.CONFIG.openshift.allowed_commands
    )
    MAX_BLOCK_COMMAND_WORDS = max_command_words(
        settings.CONFIG.openshift.blocked_commands
    )


def max_command_words(commands: list[str]) -> int:
    return max(len(cmd.split()) for cmd in commands)


##########
# MCP TOOLS AND SUPPORTING METHODS


@tool_logger
async def openshift_cli_mcp_tool(command_str: str, ctx: Context) -> str:
    """Run an OpenShift CLI command

    Runs an `oc` command as if it were run in a terminal.
    No need to provide credentials, they are already passed by the client.

    Args:
       command_str: String with the command to run.
    Returns:
       str: The stdout or stderr of the command.
    """
    # Build the command arguments list for the openstack command
    mcp_argv = OC_PARAMS + get_ocp_credentials_args(ctx)
    user_argv = validate_command(command_str)
    returncode, stdout, stderr = await utils.EXECUTOR.run_command(mcp_argv + user_argv)
    if returncode:
        raise ToolError(f"openshift failed with error code {returncode}: {stderr}")
    return stdout or stderr


def validate_command(command_str: str) -> list[str]:
    """Validate the command and return the argv list without the "oc" prefix."""
    command_str = command_str.strip()

    if not command_str or command_str == "oc":
        raise ToolError("No command provided")

    # Use shlex to do a proper split (honoring quotes and escapes)
    argv = shlex.split(command_str)
    if argv[0] == "oc":
        argv = argv[1:]

    if not _is_command_allowed(argv):
        msg = "Command {command_str} is currently blocked for LLM use as it could modify the deployment."
        raise ToolError(msg)
    return argv


def _is_in_command_list(
    command: list[str], command_list: list[str], max_words: int
) -> bool:
    for i in range(1, max_words + 1):
        if " ".join(command[:i]) in command_list:
            return True
    return False


def _is_command_allowed(argv: list[str]) -> bool:
    utils.reject_arguments(argv, REJECT_GLOBAL_ARGS)

    # To check valid commands we need to remove global arguments
    argv_without_global_args = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        # If the global option doesn't include an =, then the next word is the value, so we skip it
        if arg.startswith("-"):
            if "=" not in arg:
                i += 1
        else:
            argv_without_global_args.append(arg)
        i += 1

    if settings.CONFIG.openshift.allow_write:
        return not _is_in_command_list(
            argv_without_global_args,
            settings.CONFIG.openshift.blocked_commands,
            MAX_BLOCK_COMMAND_WORDS,
        )

    return _is_in_command_list(
        argv_without_global_args,
        settings.CONFIG.openshift.allowed_commands,
        MAX_ALLOW_COMMAND_WORDS,
    )


def get_ocp_credentials_args(ctx: Context) -> list[str]:
    """Get OpenShift credentials arguments."""
    headers = ctx.request_context.request.headers
    logger.debug(f"Headers: {headers}")
    token_header = utils.strip_bearer_prefix(headers.get("OCP_TOKEN", ""))
    result = ["--token", token_header] if token_header else []
    url_header = headers.get("OCP_URL")
    if url_header:
        result.extend(["--server", url_header])
    return result
