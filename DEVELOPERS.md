# Developer documentation

## MCP Stack

Official MCP documentation is available at [https://modelcontextprotocol.io](https://modelcontextprotocol.io).

The project uses the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk), version 2.x.

We create 2 different MCP routes: `/openstack/` and `/openshift/` and tools should be added to one or the other based on what credentials they need.

## Tests

Run the test suite with the locked dependencies:

```bash
uv run --locked python -m unittest discover -v
```

The HTTP tests cover both MCP endpoints with legacy and current protocol requests,
including authentication, DNS protection, credential forwarding, and tool errors.
CLI execution is mocked, so these tests do not require a cluster or the `oc` binary.

## Configuration

Configuration options for *all* the MCP tools are in the [`settings.py` file](./src/rhos_ls_mcps/settings.py).

Options are defined using the `pydantic` library and the file is loaded from the file defined in `RHOS_MCPS_CONFIG` environmental variable or `./config.yaml` if not defined.

Loader method is `load_config` in the same file, and sets values loaded (or defaulted to) in global variable named `CONFIG`, where other parts of the code can read the values.

## Adding Tools

When creating a new tool in this repository we need to create a new file, like we currently have [`oc.py`](./src/rhos_ls_mcps/oc.py) and [`osc.py`](./src/rhos_ls_mcps/osc.py).

In this file we need to create an `initialize` method responsible for initializing any global variables/instances of the tool and adding the tool/s to the OSP and/or OCP MCPs.

The `initialize` signature is:

```python
from mcp.server.mcpserver import MCPServer

def initialize(mcp_osp: MCPServer, mcp_ocp: MCPServer):
    pass
```

Adding a tool to the MCP servers is straightforward:

```python
from mcp.server.mcpserver import MCPServer

def initialize(mcp_osp: MCPServer, mcp_ocp: MCPServer):
    mcp_osp.add_tool(<tool_method_name>,
                     name="openstack-cli",
                     title="OpenStack Client MCP Tool")
```


Things to remember when coding the tool:

- The method can accept as many arguments as we need, but we must define their type.

- The function's docstring will be sent to the LLM to decide when and how to call it.

- The `ctx: Context` is not an MCP argument for the LLM but an argument we can optionally add that will make the MCP stack pass the request context to the method. Allows the code to access the lifespan variables, headers, send logging messages to the client, etc. Examples:
  * `ctx.request_context.request.headers`
  * `ctx.request_context.lifespan_context`
  * Use Python's `logging` module for logs; MCP context logging is deprecated in SDK v2.

- Method must always be `async` and block as little as possible.

- Tools must use the `tool_logger` decorator to log requests.

- To return an error raise `ToolError` exception with a message.

- On success method should return a string.

- Configuration options are accessible through the `settings.CONFIG` instance, but not before the `initialize` method is called.

- We can use methods `run_command` and `run_function` in `utils.EXECUTOR` to run code in a different process to avoid blocking the MCP server.

Example of a tool:

```python
from mcp.server.mcpserver import Context
from rhos_ls_mcps.logging import tool_logger

@tool_logger
async def <tool_method_name>(<arg_name>: <arg_type>, ctx: Context) -> str:
    """Description for the LLM"""
    pass
```

We can add any setting options the tool needs to the [`settings.py` file](./src/rhos_ls_mcps/settings.py).

Now we need to make sure that our tool's initialization method (`initialize`) is called from the `initialize` method in the [`main.py` file](./src/rhos_ls_mcps/main.py).

## OpenStack tools

MCP tools that require OpenStack credentials are registered on the `mcp_osp` server and are accessible under the `/openstack/` URL path.

Currently the only tool implementation lives in [`osc.py`](./src/rhos_ls_mcps/osc.py).

### `openstack`

The `openstack-cli` tool exposes the full OpenStack CLI to the LLM. The MCP client passes a command string (with or without the leading `openstack` keyword) and the tool returns stdout or raises a `ToolError` on failure.

#### Calling `openstack`

Instead of spawning a new `openstack` subprocess for every call, the tool uses `python-openstackclient` **as a library**. Loading all OpenStack client plugins and command entry points, and initializing things takes time, so doing it once and reusing the same shell object across requests gives a performance improvement.

The `MyOpenStackShell` class subclasses `osc_shell.OpenStackShell` and overrides its `stdin`, `stdout`, and `stderr` to use `io.StringIO` buffers instead of the real terminal streams, which allows the tool to capture command output and return it to the MCP client. Plugin and command loading and initialization is guarded by class-level flags (`loaded_plugins`, `loaded_commands`) so it only happens on the first request.

On the first request the shell also discovers the latest API microversion for every installed OpenStack service by running `openstack versions show --format json`. The reason is that not all openstack clients support setting a "latest" microversion.

These microversions are then set as defaults in the argument parser so that all subsequent commands automatically use the latest available API version — while still allowing the caller to override with the appropriate `--os-XXXX-api-version` flag.

#### Read only mode

Read-only mode is implemented by replacing the `EntryPoint` objects of disallowed commands inside a custom `CommandManager` subclass (`MyCommandManager`).

During `initialize()`, the helper `osp_list_commands()` scans every `openstack.*` entry-point group installed in the Python environment and classifies each command as *allowed* or *not allowed* based on the verb whitelist `ACCEPT_COMMANDS`. The resulting list is stored in the module-level `ALLOWED_COMMANDS`.

At command-load time, `MyCommandManager.load_commands()` iterates over all loaded commands and replaces the `EntryPoint` of every command that is **not** in `ALLOWED_COMMANDS` with a `RejectedEntryPoint` instance. When `cliff` (Command Line Interface Formulation Framework that openstackclient uses) tries to instantiate a blocked command it calls `EntryPoint.load()`, which in our case is `RejectedEntryPoint.load()`, that writes a human-readable message to stderr and raises `SystemError(3)` before the command can run.

Using replacement rather than removal lets `cliff` distinguish between "command blocked" and "command does not exist", giving a clearer error to the caller.

Write mode can be enabled by setting `openstack.allow_write: true` in the configuration file, which causes `_is_command_allowed()` to always return `True` and skip the replacement step entirely.

#### Argument protection

From the code perspective global arguments that the MCP client must not pass can be split in 2 categories:

- **`DELETE_GLOBAL_ARGS`** — arguments that are removed from the parser altogether (e.g. `--os-username`, `--os-password`, `--os-cloud`, `--os-application-credential-secret`). This is done during initialisation by changing the `type` of those parser actions to a custom registered `"fail"` type. Any attempt to pass one of these arguments triggers a `ToolError` before the command even runs.

- **`REJECT_GLOBAL_ARGS`** — arguments that the MCP tool itself needs internally (e.g. `--os-auth-url`, `--os-token`, `--insecure`, `--os-cacert`) and therefore cannot be removed from the parser, but that users are not allowed to provide. These are checked at runtime via `utils.reject_arguments()` just before each command is dispatched to the subprocess. If any of them appear in the user-supplied argv, a `ToolError` is raised immediately.

#### Subprocesses

`python-openstackclient` is inherently synchronous: it blocks the calling thread and its shared parser state is not safe for concurrent use. To avoid blocking the async MCP server, each command is offloaded to a separate process via the `utils.EXECUTOR.run_function` method.

The executor uses a `ProcessPoolExecutor` configured with a **`fork`** multiprocessing context. The `fork` context is intentional: by the time a worker process is forked, `MyOpenStackShell` is already fully initialised (plugins loaded, commands loaded, API versions discovered). The forked child inherits all of that in-memory state and starts executing the command immediately, without repeating the expensive startup work.

`run_function` submits work to a `ProcessPool` with a maximum allowed subprocesses of `processes_pool_size`.

## OpenShift tools

MCP tools that require OpenShift credentials are registered on the `mcp_ocp` server and are accessible under the `/openshift/` URL path.

Currently the only tool implementation lives in [`oc.py`](./src/rhos_ls_mcps/oc.py).

### `oc`

The `openshift-cli` tool exposes the `oc` binary to the LLM. The MCP client passes a command string (with or without the leading `oc` keyword) and the tool returns stdout or raises a `ToolError` on failure.

#### Read only mode

Read-only mode for `oc` is controlled by two lists in the configuration:

- **`allowed_commands`** (default: `DEFAULT_ALLOWED_COMMANDS` in [`oc_defaults.py`](./src/rhos_ls_mcps/oc_defaults.py)): when `openshift.allow_write` is `False` (the default), only commands whose first word(s) match an entry in this list are permitted. Examples of allowed commands include `get`, `describe`, `logs`, `explain`, `adm top`, and `auth can-i`.

- **`blocked_commands`** (default: `DEFAULT_BLOCKED_COMMANDS`): when `openshift.allow_write` is `True`, commands in this list are **always** rejected regardless, to prevent the LLM from accessing sensitive operations like `config`, `logout`, or `get-token`.

The check in `_is_command_allowed()` first strips all leading global flags (arguments starting with `-`) from the user argv so that, for example, `oc --namespace foo get pods` is correctly identified as a `get` command. It then calls `_is_in_command_list()` which tries every prefix of up to `MAX_ALLOW_COMMAND_WORDS` (or `MAX_BLOCK_COMMAND_WORDS`) words against the relevant list. These max-word counts are pre-computed during `initialize()` from the longest entry in each list, so the inner loop never checks more words than necessary. It is necessary to check multiple lengths because some commands have a mix of allowed and forbidden subcommands, for example `oc adm policy scc-review` is allowed but `oc adm policy remove-group` is not.

#### Argument protection

Before the command is tested against the allowed/blocked lists, `utils.reject_arguments()` is called with `REJECT_GLOBAL_ARGS` — a list of `oc` global flags that govern cluster connection parameters (e.g. `--server`, `--token`, `--kubeconfig`, `--certificate-authority`, `--insecure-skip-tls-verify`). If any of those flags appear in the user-provided argv, a `ToolError` is raised immediately.

Cluster credentials are either present on the file system or injected by the MCP server with the `get_ocp_credentials_args()` when it can read the `OCP_TOKEN` and `OCP_URL` HTTP request headers and prepend the corresponding `--token` / `--server` arguments to the command line.

#### Subprocesses

The `oc` external binary is called using the `run_command` helper method which uses `asyncio.create_subprocess_exec` so it does **not** consume a slot in the executor's worker pool.

To make sure that the sum of processes created by `run_command` and `run_funtion` honor the `processes_pool_size` we use a single semaphore for both. The semaphore is therefore the single shared back-pressure mechanism across both OpenStack and OpenShift tool calls.
