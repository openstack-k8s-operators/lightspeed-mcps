"""Base class for the MCP integration tests.

Each subclass describes a scenario as class attributes (which tools are
enabled, write permissions, command-list overrides, whether static
credentials are mounted); ``setUpClass`` starts one MCP server container
for the scenario, and ``tearDownClass`` removes it.

The suite is strictly a consumer: the container image and the backend
cluster (a real CRC deployment or the fake cluster from
``tests/integration/fake_cluster.py``) must already exist.  Missing
prerequisites fail fast with instructions instead of being provisioned.

The MCP client is raw JSON-RPC over ``httpx2`` (the wire format proven by
the component tests): stateless ``MCP-Protocol-Version`` requests with
Bearer transport authentication, ``initialize`` -> ``tools/list`` ->
``tools/call``, and per-call extra headers for the dynamic credentials
(``OS_TOKEN``/``OS_URL``/``OCP_TOKEN``/``OCP_URL``).

Configuration (environment variables, the suite reads but never sets):

===========================  ==========================  =========================================
Variable                     Default                    Meaning
===========================  ==========================  =========================================
RHOS_MCPS_TEST_BACKEND       ``real``                    ``real`` or ``fake``
RHOS_MCPS_TEST_CREDS_DIR     repo root                  Dir with ``clouds.yaml`` + ``secure.yaml``
                                                        (+ optional ``tls-ca-bundle.pem``): CRC
                                                        outputs for real, the fake's ``--out-dir``
                                                        for fake
RHOS_MCPS_TEST_KUBECONFIG    see resolution order       Resolution order: env var ->
                                                        ``<creds_dir>/kubeconfig`` ->
                                                        ``~/.crc/machines/crc/kubeconfig``
RHOS_MCPS_TEST_CLOUD         unset                      Optional cloud name, forwarded as
                                                        ``OS_CLOUD`` into the container
RHOS_MCPS_TEST_IMAGE         ``localhost/rhos-mcps-itest``  Must already exist
===========================  ==========================  =========================================
"""

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.request
import uuid
from pathlib import Path

import httpx2
import yaml

from rhos_ls_mcps.oc_defaults import (
    DEFAULT_ALLOWED_COMMANDS,
    DEFAULT_BLOCKED_COMMANDS,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
OPENSTACK_PATH = "/openstack/"
OPENSHIFT_PATH = "/openshift/"
TOOL_NAMES = {OPENSTACK_PATH: "openstack-cli", OPENSHIFT_PATH: "openshift-cli"}
SERVER_NAMES = {OPENSTACK_PATH: "rhoso-tools", OPENSHIFT_PATH: "ocp-tools"}
PROTOCOL_VERSION = "2025-11-25"
# A real-cluster first call does real version discovery, be patient
CALL_TIMEOUT = 120
HEALTH_TIMEOUT = 30
# Concurrency check: N parallel calls must beat 60% of the serial estimate
CONCURRENCY_FACTOR = 0.6
# Namespace of the service account the dynamic-credentials token is minted
# for (the tests only read it); must match the namespaces they query
DYNAMIC_SA_NAMESPACE = "openstack"


class McpIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """One container per test class, configured from the scenario attributes."""

    # Scenario attributes, overridden by subclasses
    openstack_enabled: bool = True
    openshift_enabled: bool = True
    openstack_allow_write: bool = False
    openshift_allow_write: bool = False
    openshift_allowed_commands: list[str] | None = None  # None: default
    openshift_blocked_commands: list[str] | None = None  # None: default
    static_creds: bool = True

    _handshake_done = False

    # OpenShift token minting for the dynamic-credentials tests (see
    # _mint_ocp_token): minted once per class, account deleted on teardown
    _ocp_token: str | None = None
    _ocp_sa: str | None = None

    ##########
    # Container lifecycle

    @classmethod
    def setUpClass(cls):
        cls.backend = os.environ.get("RHOS_MCPS_TEST_BACKEND", "real").lower()
        if cls.backend not in ("real", "fake"):
            raise AssertionError(
                f"Invalid RHOS_MCPS_TEST_BACKEND {cls.backend!r}, use 'real' or 'fake'"
            )
        cls.image = os.environ.get("RHOS_MCPS_TEST_IMAGE", "localhost/rhos-mcps-itest")
        creds_dir = os.environ.get("RHOS_MCPS_TEST_CREDS_DIR", REPO_ROOT)
        cls.creds_dir = Path(creds_dir).resolve()
        cls.os_cloud = os.environ.get("RHOS_MCPS_TEST_CLOUD")
        cls._fail_fast()
        cls.tmp_dir = Path(tempfile.mkdtemp(prefix=f"rhos-mcps-itest-{cls.__name__}-"))
        cls.transport_token = secrets.token_hex(16)
        cls._write_config()
        cls._start_container()
        cls._wait_for_health()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "_ocp_sa", None):
            subprocess.run(
                [
                    "oc",
                    "--kubeconfig",
                    str(cls.kubeconfig_path()),
                    "delete",
                    "serviceaccount",
                    cls._ocp_sa,
                    "-n",
                    DYNAMIC_SA_NAMESPACE,
                ],
                capture_output=True,
            )
        if getattr(cls, "container", None):
            subprocess.run(["podman", "rm", "-f", cls.container], capture_output=True)
        if getattr(cls, "tmp_dir", None):
            shutil.rmtree(cls.tmp_dir, ignore_errors=True)

    @classmethod
    def kubeconfig_path(cls) -> Path:
        env = os.environ.get("RHOS_MCPS_TEST_KUBECONFIG")
        if env:
            return Path(env)
        candidate = cls.creds_dir / "kubeconfig"
        if candidate.exists():
            return candidate
        return Path.home() / ".crc/machines/crc/kubeconfig"

    @classmethod
    def _fail_fast(cls):
        """Fail with instructions, never fix up the environment."""
        if not shutil.which("podman"):
            raise AssertionError("podman is required to run the integration tests")
        if (
            subprocess.run(["podman", "image", "exists", cls.image]).returncode
            and subprocess.run(["podman", "pull", cls.image]).returncode
        ):
            raise AssertionError(
                f"Container image {cls.image} does not exist. Build it first:\n"
                f"  ./.s2i/get_artifacts.sh ./.s2i/artifacts.txt\n"
                f"  podman build -t {cls.image} .\n"
                f"Or set RHOS_MCPS_TEST_IMAGE to an existing image."
            )
        missing = [
            name
            for name in ("clouds.yaml", "secure.yaml")
            if not (cls.creds_dir / name).exists()
        ]
        kubeconfig = cls.kubeconfig_path()
        if not kubeconfig.exists():
            missing.append(f"kubeconfig ({kubeconfig})")
        if missing:
            if cls.backend == "real":
                raise AssertionError(
                    f"Missing real-cluster credentials: {', '.join(missing)}\n"
                    f"Get them from the CRC deployment with:\n"
                    f"  scripts/get-crc-creds.sh\n"
                    f"Or run the suite against the fake cluster:\n"
                    f"  RHOS_MCPS_TEST_BACKEND=fake "
                    f"RHOS_MCPS_TEST_CREDS_DIR=<fake-cluster-out-dir>"
                )
            raise AssertionError(
                f"Missing fake cluster credentials: {', '.join(missing)}\n"
                f"Start the fake cluster first:\n"
                f"  uv run python -m tests.integration.fake_cluster --host 0.0.0.0"
                f" --port 0 --out-dir <dir> --advertise-host"
                f" host.containers.internal\n"
                f"and pass its --out-dir as RHOS_MCPS_TEST_CREDS_DIR."
            )

    @classmethod
    def _write_config(cls):
        openstack = {
            "enabled": cls.openstack_enabled,
            "allow_write": cls.openstack_allow_write,
        }
        openshift = {
            "enabled": cls.openshift_enabled,
            "allow_write": cls.openshift_allow_write,
        }
        ca_bundle = cls.creds_dir / "tls-ca-bundle.pem"
        if cls.backend == "real":
            # With static credentials oc trusts the kubeconfig's own CA
            # data; the dynamic-credentials scenario mounts no kubeconfig,
            # so oc cannot verify the cluster certificate and must run
            # insecure, exactly like openstack in the same situation
            openshift["insecure"] = not cls.static_creds
            if cls.static_creds and ca_bundle.exists():
                openstack["ca_cert"] = "/app/tls-ca-bundle.pem"
                openstack["insecure"] = False
            else:
                openstack["insecure"] = True
        else:  # fake: both tools talk to the fake's self-signed certificate
            openstack["insecure"] = True
            openshift["insecure"] = True
        if cls.openshift_allowed_commands is not None:
            openshift["allowed_commands"] = cls.openshift_allowed_commands
        if cls.openshift_blocked_commands is not None:
            openshift["blocked_commands"] = cls.openshift_blocked_commands
        config = {
            "ip": "0.0.0.0",
            "port": 8080,
            "processes_pool_size": 10,
            "mcp_transport_security": {"token": cls.transport_token},
            "openstack": openstack,
            "openshift": openshift,
        }
        cls.config_file = cls.tmp_dir / "config.yaml"
        cls.config_file.write_text(yaml.safe_dump(config))

    @classmethod
    def _start_container(cls):
        cls.container = f"rhos-mcps-itest-{uuid.uuid4().hex[:12]}"
        cmd = [
            "podman",
            "run",
            "-d",
            "--name",
            cls.container,
            # Random host port so parallel suite runs don't collide
            "-p",
            "127.0.0.1::8080",
            "-v",
            f"{cls.config_file}:/app/config.yaml:Z",
        ]
        if cls.static_creds:
            # The kubeconfig is copied (and made world readable) instead of
            # mounting the original: the container runs as UID 1001 and we
            # must not relabel or chown the developer's file.
            kubeconfig = cls.tmp_dir / "kubeconfig"
            shutil.copyfile(cls.kubeconfig_path(), kubeconfig)
            kubeconfig.chmod(0o644)
            ca_bundle = cls.creds_dir / "tls-ca-bundle.pem"
            cmd += [
                "-v",
                f"{cls.creds_dir}/clouds.yaml:/app/clouds.yaml:Z",
                "-v",
                f"{cls.creds_dir}/secure.yaml:/app/secure.yaml:Z",
                "-v",
                f"{kubeconfig}:/app/kubeconfig:Z",
                "-e",
                "KUBECONFIG=/app/kubeconfig",
            ]
            if cls.backend == "real" and ca_bundle.exists():
                cmd += ["-v", f"{ca_bundle}:/app/tls-ca-bundle.pem:Z"]
            if cls.os_cloud:
                cmd += ["-e", f"OS_CLOUD={cls.os_cloud}"]
        cmd.append(cls.image)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode:
            raise AssertionError(f"podman run failed:\n{result.stderr}")

    @classmethod
    def _dump_logs(cls):
        result = subprocess.run(
            ["podman", "logs", cls.container], capture_output=True, text=True
        )
        print(f"--- podman logs {cls.container} ---\n{result.stdout}{result.stderr}")

    @classmethod
    def _wait_for_health(cls):
        result = subprocess.run(
            ["podman", "port", cls.container, "8080"], capture_output=True, text=True
        )
        if result.returncode or not result.stdout.strip():
            cls._dump_logs()
            raise AssertionError(f"Could not resolve container port: {result.stderr}")
        # Output looks like "127.0.0.1:34567"
        cls.base_url = "http://" + result.stdout.strip().splitlines()[0]
        deadline = time.monotonic() + HEALTH_TIMEOUT
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"{cls.base_url}/health", timeout=2
                ) as response:
                    if response.status == 200:
                        return
            except OSError:
                pass
            time.sleep(0.5)
        cls._dump_logs()
        raise AssertionError(
            f"MCP server did not answer /health in {HEALTH_TIMEOUT}s (logs above)"
        )

    ##########
    # MCP client (raw JSON-RPC over the component tests' wire format)

    @property
    def enabled_paths(self) -> list[str]:
        return [
            path
            for path, enabled in (
                (OPENSTACK_PATH, self.openstack_enabled),
                (OPENSHIFT_PATH, self.openshift_enabled),
            )
            if enabled
        ]

    async def asyncSetUp(self):
        # One initialize handshake per class validates the protocol against
        # the real server; stateless requests don't require it afterwards.
        if type(self)._handshake_done:
            return
        type(self)._handshake_done = True
        for path in self.enabled_paths:
            result = await self.rpc(
                path,
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "rhos-mcps-itest", "version": "1"},
                },
            )
            self.assertEqual(result["serverInfo"]["name"], SERVER_NAMES[path])

    async def raw_rpc(self, path, method, params=None, headers=None, auth=True):
        request_headers = {"MCP-Protocol-Version": PROTOCOL_VERSION}
        if auth:
            request_headers["Authorization"] = f"Bearer {self.transport_token}"
        request_headers.update(headers or {})
        async with httpx2.AsyncClient(
            base_url=type(self).base_url,
            headers={"Accept": "application/json, text/event-stream"},
            timeout=CALL_TIMEOUT,
        ) as client:
            return await client.post(
                path,
                headers=request_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params or {},
                },
            )

    async def rpc(self, path, method, params=None, headers=None):
        response = await self.raw_rpc(path, method, params, headers)
        if response.status_code != 200:
            self.fail(
                f"HTTP {response.status_code} for {method} {path}: {response.text}"
            )
        if response.headers["content-type"].startswith("text/event-stream"):
            messages = [
                json.loads(line.removeprefix("data: "))
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            payload = next(message for message in messages if message.get("id") == 1)
        else:
            payload = response.json()
        self.assertNotIn("error", payload, payload)
        return payload["result"]

    async def call_tool(self, path, command, headers=None) -> str:
        result = await self.rpc(
            path,
            "tools/call",
            {"name": TOOL_NAMES[path], "arguments": {"command_str": command}},
            headers,
        )
        text = result["content"][0]["text"]
        self.assertFalse(result.get("isError", False), text)
        return text

    async def assert_tool_error(self, path, command, headers=None, pattern=None) -> str:
        result = await self.rpc(
            path,
            "tools/call",
            {"name": TOOL_NAMES[path], "arguments": {"command_str": command}},
            headers,
        )
        self.assertTrue(result.get("isError", False), result)
        text = result["content"][0]["text"]
        if pattern:
            self.assertRegex(text, pattern)
        return text

    async def assert_concurrent(self, path, command, count=5):
        """Count parallel calls must beat the serial estimate by a wide margin."""
        # Warm up: the first call initializes the shell (version discovery)
        await self.call_tool(path, command)
        start = time.monotonic()
        await self.call_tool(path, command)
        baseline = time.monotonic() - start
        start = time.monotonic()
        await asyncio.gather(*(self.call_tool(path, command) for _ in range(count)))
        parallel = time.monotonic() - start
        self.assertLess(
            parallel,
            baseline * count * CONCURRENCY_FACTOR,
            f"{count} parallel calls took {parallel:.2f}s, "
            f"which is not faster than {CONCURRENCY_FACTOR:.0%} of the "
            f"{baseline * count:.2f}s serial estimate",
        )

    ##########
    # Direct cluster access: verification and cleanup must bypass the MCP
    # under test, so they run the raw CLIs in the server container

    @classmethod
    async def exec_in_container(cls, *args, check=True) -> subprocess.CompletedProcess:
        """Run a command in the MCP server container, not through the MCP."""
        command = ("podman", "exec", cls.container, *args)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if check and process.returncode:
            raise AssertionError(
                f"podman exec {' '.join(args)} failed:\n{stderr.decode().strip()}"
            )
        return subprocess.CompletedProcess(
            command, process.returncode, stdout.decode(), stderr.decode()
        )

    @classmethod
    async def exec_openstack(cls, *args, check=True) -> subprocess.CompletedProcess:
        """Direct `openstack` CLI call in the server container."""
        # Same TLS decision as _write_config: the CA bundle is only mounted
        # for the real backend when it exists; the fake's clouds.yaml
        # already carries verify: false
        ca_bundle = cls.creds_dir / "tls-ca-bundle.pem"
        if cls.backend == "real" and ca_bundle.exists():
            args = (*args, "--os-cacert", "/app/tls-ca-bundle.pem")
        elif cls.backend == "real":
            args = (*args, "--insecure")
        return await cls.exec_in_container("openstack", *args, check=check)

    ##########
    # Dynamic credentials sourcing (for the scenarios without static creds)

    @classmethod
    def dynamic_credentials(cls) -> dict[str, str]:
        """OS_TOKEN/OS_URL/OCP_TOKEN/OCP_URL headers for the current backend."""
        if cls.backend == "fake":
            # The fake accepts any OpenStack token
            clouds = yaml.safe_load((cls.creds_dir / "clouds.yaml").read_text())
            cloud = next(iter(clouds["clouds"].values()))
            kubeconfig = yaml.safe_load(cls.kubeconfig_path().read_text())
            server, token = _kubeconfig_credentials(kubeconfig)
            return {
                "OS_TOKEN": "itest-dynamic-token",
                "OS_URL": cloud["auth"]["auth_url"],
                "OCP_TOKEN": token,
                "OCP_URL": server,
            }
        # Real backend: get a token through openstacksdk from the creds files
        import openstack

        ca_bundle = cls.creds_dir / "tls-ca-bundle.pem"
        config = openstack.config.loader.OpenStackConfig(
            config_files=[str(cls.creds_dir / "clouds.yaml")],
            secure_files=[str(cls.creds_dir / "secure.yaml")],
        )
        verify = str(ca_bundle) if ca_bundle.exists() else False
        cloud = config.get_one_cloud(cloud=cls.os_cloud, verify=verify)
        connection = openstack.connection.Connection(config=cloud)
        kubeconfig = yaml.safe_load(cls.kubeconfig_path().read_text())
        server, token = _kubeconfig_credentials(kubeconfig)
        auth_url = cloud.config["auth"]["auth_url"].rstrip("/")
        # Token+endpoint credentials use the URL verbatim (no discovery), so
        # it must point at the v3 API even when auth_url doesn't include it
        if not auth_url.endswith("/v3"):
            auth_url += "/v3"
        return {
            "OS_TOKEN": connection.session.get_token(),
            "OS_URL": auth_url,
            "OCP_TOKEN": token or cls._mint_ocp_token(),
            "OCP_URL": server,
        }

    @classmethod
    def _mint_ocp_token(cls) -> str:
        """Bearer token for the dynamic-credentials tests.

        Needed when the kubeconfig has no token (client-certificate auth,
        like CRC's admin kubeconfig): oc only accepts bearer tokens on its
        command line.  A short-lived token is minted for a per-run
        read-only service account, mirroring the OpenStack side, which
        requests its token through openstacksdk.  tearDownClass removes
        the account.
        """
        if cls._ocp_token:
            return cls._ocp_token
        kubeconfig = str(cls.kubeconfig_path())
        cls._ocp_sa = f"rhos-mcps-itest-{uuid.uuid4().hex[:12]}"
        for args in (
            ("create", "serviceaccount", cls._ocp_sa, "-n", DYNAMIC_SA_NAMESPACE),
            (
                "adm",
                "policy",
                "add-role-to-user",
                "view",
                "-z",
                cls._ocp_sa,
                "-n",
                DYNAMIC_SA_NAMESPACE,
            ),
            (
                "create",
                "token",
                cls._ocp_sa,
                "-n",
                DYNAMIC_SA_NAMESPACE,
                "--duration=1h",
            ),
        ):
            result = subprocess.run(
                ["oc", "--kubeconfig", kubeconfig, *args],
                capture_output=True,
                text=True,
            )
            if result.returncode:
                raise AssertionError(
                    f"Could not mint the dynamic-credentials token "
                    f"(oc {' '.join(args)}): {result.stderr.strip()}"
                )
        cls._ocp_token = result.stdout.strip()
        return cls._ocp_token


def _kubeconfig_credentials(kubeconfig: dict) -> tuple[str, str | None]:
    """Current context's cluster server URL and user token.

    The token is None when the user authenticates with something else,
    e.g. the client certificates of CRC's admin kubeconfig; the caller
    must then mint one (see ``McpIntegrationTest._mint_ocp_token``).
    """
    current = kubeconfig["current-context"]
    context = next(c for c in kubeconfig["contexts"] if c["name"] == current)["context"]
    cluster = next(
        c for c in kubeconfig["clusters"] if c["name"] == context["cluster"]
    )["cluster"]
    user = next(c for c in kubeconfig["users"] if c["name"] == context["user"])["user"]
    return cluster["server"], user.get("token")


def default_allowed_commands_without(*removed: str) -> list[str]:
    return [cmd for cmd in DEFAULT_ALLOWED_COMMANDS if cmd not in removed]


def default_blocked_commands_without(*removed: str) -> list[str]:
    return [cmd for cmd in DEFAULT_BLOCKED_COMMANDS if cmd not in removed]
