import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx2

from rhos_ls_mcps import main, oc, osc, settings, utils
from rhos_ls_mcps import logging as mcp_logging


PROTOCOL_VERSIONS = ("2025-11-25", "2026-07-28")
ENDPOINTS = (
    ("/openstack/", "openstack-cli", "rhoso-tools"),
    ("/openshift/", "openshift-cli", "ocp-tools"),
)


class MCPHTTPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = settings.Settings(mcp_transport_security={"token": None})
        self.enterContext(patch.object(settings, "CONFIG", self.config))
        self.enterContext(
            patch.object(settings, "load_config", return_value=self.config)
        )
        self.enterContext(patch.object(mcp_logging, "init_logging"))
        self.enterContext(patch.object(utils, "init_process_pool"))
        self.command = AsyncMock(return_value=(0, "pods output", ""))
        self.shell = AsyncMock(return_value=(0, "servers output", ""))
        self.enterContext(
            patch.object(utils, "EXECUTOR", SimpleNamespace(run_command=self.command))
        )
        self.enterContext(patch.object(osc, "SHELL", SimpleNamespace(run=self.shell)))
        self.enterContext(patch.object(oc, "OC_PARAMS", ["oc"]))
        self.enterContext(patch.object(osc, "OSC_PARAMS", []))
        self.enterContext(patch.object(osc, "ALLOWED_COMMANDS", []))
        self.enterContext(patch.object(oc, "MAX_ALLOW_COMMAND_WORDS", 3))
        self.enterContext(patch.object(oc, "MAX_BLOCK_COMMAND_WORDS", 3))
        self.osp_token = uuid4().hex
        self.ocp_token = uuid4().hex
        self.credentials = {
            "OS_TOKEN": f"Bearer {self.osp_token}",
            "OS_URL": "https://openstack.example.test",
            "OCP_TOKEN": f"Bearer {self.ocp_token}",
            "OCP_URL": "https://openshift.example.test",
        }

    @asynccontextmanager
    async def client(self):
        app = main.create_app()
        async with app.app.router.lifespan_context(app.app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                base_url="http://mcp.example.test",
                headers={"Accept": "application/json, text/event-stream"},
            ) as client:
                yield client

    async def post(
        self,
        client,
        path,
        method,
        params=None,
        version=PROTOCOL_VERSIONS[0],
        headers=None,
    ):
        params = dict(params or {})
        request_headers = {"MCP-Protocol-Version": version}
        if version == "2026-07-28":
            params["_meta"] = {
                **params.get("_meta", {}),
                "io.modelcontextprotocol/protocolVersion": version,
                "io.modelcontextprotocol/clientCapabilities": {},
            }
            request_headers["Mcp-Method"] = method
            if method == "tools/call":
                request_headers["Mcp-Name"] = params["name"]
        async with asyncio.timeout(5):
            return await client.post(
                path,
                headers={**request_headers, **(headers or {})},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params,
                },
            )

    def result(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("mcp-session-id", response.headers)
        if response.headers["content-type"].startswith("text/event-stream"):
            messages = [
                json.loads(line.removeprefix("data: "))
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]
            payload = next(message for message in messages if message.get("id") == 1)
        else:
            payload = response.json()
        self.assertNotIn("error", payload)
        return payload["result"]

    async def test_discovery_for_legacy_and_current_clients(self):
        async with self.client() as client:
            for version in PROTOCOL_VERSIONS:
                for path, tool_name, server_name in ENDPOINTS:
                    with self.subTest(version=version, path=path):
                        if version == "2025-11-25":
                            response = await self.post(
                                client,
                                path,
                                "initialize",
                                {
                                    "protocolVersion": version,
                                    "capabilities": {},
                                    "clientInfo": {
                                        "name": "migration-test",
                                        "version": "1",
                                    },
                                },
                                version=version,
                            )
                            result = self.result(response)
                            self.assertEqual(result["protocolVersion"], version)
                            self.assertEqual(result["serverInfo"]["name"], server_name)
                        else:
                            response = await self.post(
                                client, path, "server/discover", version=version
                            )
                            self.assertIn(
                                version, self.result(response)["supportedVersions"]
                            )
                        response = await self.post(
                            client, path, "tools/list", version=version
                        )
                        tools = self.result(response)["tools"]
                        self.assertEqual([tool["name"] for tool in tools], [tool_name])
                        self.assertEqual(
                            tools[0]["inputSchema"]["required"], ["command_str"]
                        )
                        self.assertNotIn("ctx", tools[0]["inputSchema"]["properties"])

    async def test_tool_calls_forward_credentials_and_log_metadata(self):
        observed_clients = []

        async def run(*args):
            observed_clients.append(mcp_logging.ctx.get().client_id)
            return 0, "command output", ""

        self.command.side_effect = run
        self.shell.side_effect = run
        async with self.client() as client:
            for version in PROTOCOL_VERSIONS:
                for path, tool_name, _ in ENDPOINTS:
                    for meta in (None, {"client_id": "migration-test"}):
                        with self.subTest(version=version, path=path, meta=meta):
                            params = {
                                "name": tool_name,
                                "arguments": {
                                    "command_str": "server list"
                                    if tool_name == "openstack-cli"
                                    else "get pods"
                                },
                            }
                            if meta is not None:
                                params["_meta"] = meta
                            response = await self.post(
                                client,
                                path,
                                "tools/call",
                                params,
                                version,
                                self.credentials,
                            )
                            result = self.result(response)
                            self.assertFalse(result.get("isError", False), result)
                            self.assertEqual(
                                result["content"][0]["text"], "command output"
                            )
                            self.assertEqual(
                                observed_clients[-1], "migration-test" if meta else "-"
                            )
        self.shell.assert_awaited_with(
            ["--os-token", self.osp_token, "--os-url", self.credentials["OS_URL"]],
            ["server", "list"],
        )
        self.command.assert_awaited_with(
            [
                "oc",
                "--token",
                self.ocp_token,
                "--server",
                self.credentials["OCP_URL"],
                "get",
                "pods",
            ]
        )

    async def test_cli_failures_are_tool_errors(self):
        self.command.return_value = (1, "", "command failed")
        self.shell.return_value = (1, "", "command failed")
        async with self.client() as client:
            for version in PROTOCOL_VERSIONS:
                for path, tool_name, _ in ENDPOINTS:
                    with self.subTest(version=version, path=path):
                        response = await self.post(
                            client,
                            path,
                            "tools/call",
                            {
                                "name": tool_name,
                                "arguments": {"command_str": "get pods"},
                            },
                            version,
                            self.credentials,
                        )
                        result = self.result(response)
                        self.assertTrue(result["isError"])
                        self.assertIn("command failed", result["content"][0]["text"])

    async def test_blocked_commands_are_not_executed(self):
        async with self.client() as client:
            for command in ("delete pod example", "get pods --token=override"):
                with self.subTest(command=command):
                    response = await self.post(
                        client,
                        "/openshift/",
                        "tools/call",
                        {
                            "name": "openshift-cli",
                            "arguments": {"command_str": command},
                        },
                    )
                    self.assertTrue(self.result(response)["isError"])
        self.command.assert_not_awaited()

    async def test_bearer_authentication_and_health(self):
        self.config.mcp_transport_security.token = uuid4().hex
        async with self.client() as client:
            response = await client.get("/health")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "ok"})
            for path, _, _ in ENDPOINTS:
                for token in (
                    None,
                    "invalid",
                    self.config.mcp_transport_security.token,
                ):
                    with self.subTest(
                        path=path,
                        authorized=token == self.config.mcp_transport_security.token,
                    ):
                        headers = {"Authorization": f"Bearer {token}"} if token else {}
                        response = await self.post(
                            client, path, "tools/list", headers=headers
                        )
                        if token == self.config.mcp_transport_security.token:
                            self.assertEqual(len(self.result(response)["tools"]), 1)
                        else:
                            self.assertEqual(response.status_code, 401)

    async def test_dns_rebinding_settings_are_applied(self):
        security = self.config.mcp_transport_security
        security.enable_dns_rebinding_protection = True
        security.allowed_hosts = ["mcp.example.test"]
        security.allowed_origins = ["https://console.example.test"]
        async with self.client() as client:
            for path, _, _ in ENDPOINTS:
                for headers, status in (
                    ({"Host": "unexpected.example.test"}, 421),
                    ({"Origin": "https://unexpected.example.test"}, 403),
                    ({"Origin": "https://console.example.test"}, 200),
                ):
                    with self.subTest(path=path, headers=headers):
                        response = await self.post(
                            client, path, "tools/list", headers=headers
                        )
                        self.assertEqual(response.status_code, status, response.text)

    async def test_disabled_services_are_not_mounted(self):
        for service, disabled, enabled in (
            (self.config.openstack, "/openstack/", "/openshift/"),
            (self.config.openshift, "/openshift/", "/openstack/"),
        ):
            with self.subTest(disabled=disabled):
                service.enabled = False
                async with self.client() as client:
                    response = await self.post(client, disabled, "tools/list")
                    self.assertEqual(response.status_code, 404)
                    response = await self.post(client, enabled, "tools/list")
                    self.assertEqual(len(self.result(response)["tools"]), 1)
                service.enabled = True

    async def test_disabling_both_services_fails_startup(self):
        self.config.openstack.enabled = False
        self.config.openshift.enabled = False
        with self.assertRaisesRegex(
            RuntimeError, "Both OpenStack and OpenShift services are disabled"
        ):
            main.create_app()
