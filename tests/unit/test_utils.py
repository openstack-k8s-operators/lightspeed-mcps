import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from mcp.server.mcpserver.exceptions import ToolError

from rhos_ls_mcps import utils


class ArgumentRejectionTests(unittest.TestCase):
    def test_rejects_protected_argument(self):
        with self.assertRaisesRegex(ToolError, "--token=secret"):
            utils.reject_arguments(["get", "pods", "--token=secret"], ["--token"])

    def test_allows_unrelated_arguments(self):
        utils.reject_arguments(["get", "pods"], ["--token"])


class BearerPrefixTests(unittest.TestCase):
    def test_removes_case_insensitive_bearer_prefix(self):
        self.assertEqual(utils.strip_bearer_prefix("bearer token"), "token")

    def test_leaves_non_bearer_value_unchanged(self):
        self.assertEqual(utils.strip_bearer_prefix("token"), "token")


class ProcessPoolInitializationTests(unittest.TestCase):
    def test_creates_global_process_pool_with_requested_size(self):
        pool = object()
        with (
            patch.object(utils, "EXECUTOR", None),
            patch.object(utils, "ProcessPool", return_value=pool) as process_pool,
        ):
            utils.init_process_pool(4)
            process_pool.assert_called_once_with(4)
            self.assertIs(utils.EXECUTOR, pool)

    def test_configures_executor_loop_and_shared_semaphore(self):
        executor = MagicMock()
        loop = MagicMock()
        semaphore = MagicMock()
        with (
            patch.object(utils, "ProcessPoolExecutor", return_value=executor),
            patch.object(utils.multiprocessing, "get_context", return_value="fork"),
            patch.object(utils.asyncio, "get_running_loop", return_value=loop),
            patch.object(utils.asyncio, "Semaphore", return_value=semaphore),
        ):
            pool = utils.ProcessPool(3)
        self.assertIs(pool.pool, executor)
        self.assertIs(pool.loop, loop)
        self.assertIs(pool.semaphore, semaphore)


class ProcessPoolExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_function_in_process_executor(self):
        result = asyncio.get_running_loop().create_future()
        result.set_result("done")
        loop = MagicMock()
        loop.run_in_executor.return_value = result
        pool = SimpleNamespace(pool="pool", loop=loop, semaphore=_Semaphore())
        self.assertEqual(await utils.ProcessPool.run_function(pool, str, 1), "done")
        loop.run_in_executor.assert_called_once_with("pool", str, 1)

    async def test_returns_decoded_subprocess_output(self):
        process = SimpleNamespace(
            communicate=AsyncMock(return_value=(b"stdout", b"stderr")), returncode=7
        )
        pool = SimpleNamespace(semaphore=_Semaphore())
        with patch.object(
            utils.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            self.assertEqual(
                await utils.ProcessPool.run_command(pool, ["oc", "get", "pods"]),
                (7, "stdout", "stderr"),
            )


class _Semaphore:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False
