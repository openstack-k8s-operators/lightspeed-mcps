import logging
import unittest
from types import SimpleNamespace

from rhos_ls_mcps import logging as mcp_logging


class InjectFilterTests(unittest.TestCase):
    def test_adds_request_and_client_ids_to_log_record(self):
        context_token = mcp_logging.ctx.set(
            mcp_logging.LoggerContext("request", "client")
        )
        self.addCleanup(mcp_logging.ctx.reset, context_token)
        record = logging.LogRecord("test", logging.INFO, "", 0, "message", (), None)
        self.assertTrue(mcp_logging.InjectFilter().filter(record))
        self.assertEqual(record.request_id, "request")
        self.assertEqual(record.client_id, "client")


class ToolLoggerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        context_token = mcp_logging.ctx.set(mcp_logging.ctx.get())
        self.addCleanup(mcp_logging.ctx.reset, context_token)

    async def test_records_client_id_and_returns_tool_result(self):
        @mcp_logging.tool_logger
        async def tool(ctx):
            return "result"

        context = SimpleNamespace(
            request_context=SimpleNamespace(meta={"client_id": "c"})
        )
        self.assertEqual(await tool(ctx=context), "result")
        self.assertEqual(mcp_logging.ctx.get().client_id, "c")

    async def test_uses_placeholder_client_id_without_context(self):
        @mcp_logging.tool_logger
        async def tool():
            return "result"

        await tool()
        self.assertEqual(mcp_logging.ctx.get().client_id, "-")
