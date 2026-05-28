# OpenStack LightSpeed MCPs

This repository contains MCP tools that have been developed expressly for the OpenStack Lightspeed agent to use on an OpenStack on OpenShift deployment, so they are opinionated and functionality is tailored to our needs.

Current tools are:

- `openstack-cli`: A simple tool to run `openstack` CLI commands.
- `openshift-cli`: A simple tool to run `oc` CLI commands. Requires the `oc` tool to be installed.

## Features

- Exposes the full `openstack` and `oc` CLIs as MCP tools
- Read-only mode by default (configurable allow/block lists per tool)
- Dynamic credentials via HTTP headers — no credentials stored on disk required
- Static credentials from standard filesystem locations (`clouds.yaml`, `kubeconfig`)
- Bearer token authentication for the MCP transport layer
- DNS rebinding protection (mitigates CVE-2025-49596)
- Multi-worker support via stateless HTTP
- Configurable process pool for concurrent command execution
- Container image available at `quay.io/openstack-lightspeed/rhos-mcps`

# Why CLI tools?

Most MCP server implementations out there expose very narrow focused MCP tools following the precept that it is best to have each tool do a single thing, so if that's the recommendation, why are we doing a single MCP tool for the full CLI?

First of all, exposing the CLI in a tool is still a "single thing", even if the tool allows doing many things, it is still implementing just one functionality: Implement the `openstack` or `oc` command.

Even if you think that it is a technicality, which we don't, you must also think about this:

- The `openstack` client has over 1570 commands, making it a herculean effort to implement as tools and the sheer number will make it unmanageable by the LLM.

- The Red Hat OpenStack documentation uses the `openstack` commands in its procedures, and having individual tools adds an abstraction layer, forcing the LLM to make the connection between the `openstack` command that appears in the documentation and the tool. In contrast with our approach the LLM can just send the command from the documentation to the MCP tool.

# Endpoints

Instead of having a single endpoint for the MCP server we have 2 different routes supporting Streamable HTTP MCP:

- `http://<server>/openstack/`
- `http://<server>/openshift/`

The reason for this is to limit the exposure of the tokens, so each endpoint will only get the one it needs instead of both.

# Credentials

The `openstack-cli` and `openshift-cli` tools need to know the endpoint to contact the cluster as well as credentials to access it just like the underlying CLIs they are exposing.

They support 2 mechanism to get the credentials:

- Static: The information is present on the filesystem in default locations.
- Dynamic: The information is passed as HTTP headers when calling the MCP tool.

## Static

For `openstack-cli` the locations must be one of the supported ones by `openstack`:
- `./clouds.yaml` and `./secure.yaml`
- `$HOME/.config/openstack/clouds.yaml` and `$HOME/config/openstack/secure.yaml`
- `/etc/openstack/clouds.yaml` and `/etc/openstack/secure.yaml`
- Location indicated by `OS_CLIENT_CONFIG_FILE` environmental variable

For `openshift-cli` the locations must on the directory supported by `oc`:
- `$HOME/.kube/config`
- Location indicated by `KUBECONFIG` environmental variable

## Dynamic

For `openstack-cli` the headers are:
- `OS_TOKEN`
- `OS_URL`

For `openshift-cli` the headers are:
- `OCP_TOKEN`
- `OCP_URL`

# Configuration

The configuration file is currently hardcoded to be `./config.yaml` (a different location can be defined using `RHOS_MCPS_CONFIG` environmental variable) and if you want to see a valid configuration file the repository includes a [sample configuration](config.yaml.sample).

The configuration file has 4 sections:
- General
- OpenStack
- OpenShift
- MCP Security

## General
- `ip`: IP address the server will bind to. Default `127.0.0.1`.
- `port`: TCP port the server will bind to. Default `8080`.
- `debug`: Default `false`.
- `workers`: Number of different uvicorn workers. Default `1`.
- `processes_pool_size`: Maximum number of processes each worker can launch. Default `10`.
- `log_format`: Format string for the MCP server. Must use YAML's escape syntax, so `\x1b` instead of `\033`.  Default: `%(asctime)s.%(msecs)03d %(process)d \x1b[32m%(levelname)s:\x1b[0m [%(request_id)s|%(client_id)s] %(name)s %(message)s`
- `uvicorn_log_format`: Format string for `uvicorn`. Must use YAML's escape syntax, so `\x1b` instead of `\033`. Default: `%(asctime)s.%(msecs)03d %(process)d \x1b[32m%(levelname)s:\x1b[0m [-|-] %(name)s %(message)s`

## MCP Security

These go under the `mcp_transport_security` key:

- `token`: Token to use for basic authentication. The caller must send this value in request header `Authorization`. For example: `Authorization: Bearer supersecret`. Default `""`.
- `enable_dns_rebinding_protection`: Enable DNS rebinding protection")
- `allowed_hosts`: List of allowed hosts. Default `["*:*"]`
- `allowed_origins`: list of allowed origins. Default `["http://*:*"]`.

With the exception of the `token` field the rest comes from the [Anthropic MCP SDK](https://github.com/modelcontextprotocol/python-sdk).

For example the DNS rebinding protection was introduced by Anthropic to mitigate critical RCE vulnerabilities (e.g., CVE-2025-49596). The protection restricts unauthorized local service access by verifying Origin and Host headers, requiring session tokens, and blocking malicious DNS-to-localhost mappings.

## OpenStack

These go under the `openstack` key:

- `allow_write`: Whether to allow write operations or not. Default `false`.
- `ca_cert`: CA certificate bundle file location. Default `""`.
- `insecure`: Whether to allow insecure SSL connections or not. Default `false`.

## OpenShift

These go under the `openshift` key:

- `allow_write`: Whether to allow write operations or not. Default `false`.
- `insecure`: Whether to allow insecure SSL connections or not. Default `false`.
- `allowed_commands`: List of allowed commands when `allow_write: false`. Example: `["status", "projects", "get", "adm top"]`.
- `blocked_commands`: Explicitly blocked commands when `allow_write: true`. Example: `["get-token", "logout"]`.

# Running the service

We assume the configuration file has already been created, for example `config.yaml` before we run the server using one of the following methods:

## From source code

```bash
uv run rhos-ls-mcps
```

To run with environment variables (e.g. debug mode):

```bash
DEBUG=1 uv run rhos-ls-mcps
```

## Container Image

The container image is available at `quay.io/openstack-lightspeed/rhos-mcps`.

We assume for the following examples that you haven't set the port in our config file or we've set it to the default `8080`.

### Dynamic credentials:

```bash
podman run -p 8080:8080 \
  -v ./config.yaml:/app/config.yaml:Z \
  quay.io/openstack-lightspeed/rhos-mcps:latest
```

### Static credentials:

We assume we are using CRC for the deployment, so we need to get the openstack credentials from the OpenShift cluster:

```bash
scripts/get-crc-creds.sh
```

This creates 3 files in our current directory: `clouds.yaml`, `secure.yaml`, `tls-ca-bundle.pem`.

And we have the OpenShift configuration and credentials in `~/.crc/machines/crc/kubeconfig`.

So we can run the service using all these files and our `config.yaml` file where we define the location of our ca-bundle.

```bash
podman run -p 8080:8080 \
  --user 1001 \
  -v ./config.yaml:/app/config.yaml:Z \
  -v ./clouds.yaml:/app/clouds.yaml:Z \
  -v ./secure.yaml:/app/secure.yaml:Z \
  -v ./tls-ca-bundle.pem:/app/tls-ca-bundle.pem:Z \
  -v ~/.crc/machines/crc/kubeconfig:/opt/app-root/src/.kube/config:Z,U \
  quay.io/openstack-lightspeed/rhos-mcps:latest
```

# Checking the servers

Assuming port is `8080` and the secret token used for the server is `supersecret` we can run a single `curl` command for each endpoint to confirm it's running and responds to requests:

```bash
curl -X POST http://127.0.0.1:8080/openstack/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer supersecret" \
  -d '{"jsonrpc":"2.0","id":"1","method":"tools/list"}'

curl -X POST http://127.0.0.1:8080/openshift/ \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer supersecret" \
  -d '{"jsonrpc":"2.0","id":"1","method":"tools/list"}'
```

Additionally we can use the [MCP inspector tool](https://modelcontextprotocol.io/docs/tools/inspector) against the server to run commands.
