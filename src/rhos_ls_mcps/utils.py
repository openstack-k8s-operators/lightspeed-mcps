import asyncio
from concurrent.futures import ProcessPoolExecutor
import logging
import multiprocessing
import os
from typing import Any, Callable

from mcp.server.fastmcp.exceptions import ToolError


logger = logging.getLogger(__name__)


class ProcessPool:
    def __init__(self, pool_size: int):
        self.pool_size = pool_size
        self.pool = ProcessPoolExecutor(
            max_workers=pool_size, mp_context=multiprocessing.get_context("fork")
        )
        self.loop = asyncio.get_running_loop()
        # To limit total number of concurrent commands: run_function + run_command
        self.semaphore = asyncio.Semaphore(pool_size)

    async def run_function(self, func: Callable[..., Any], *args: Any) -> Any:
        async with self.semaphore:
            result = await self.loop.run_in_executor(self.pool, func, *args)
            return result

    async def run_command(self, cmd: list[str]) -> tuple[int, str, str]:
        async with self.semaphore:
            logger.debug(f"Running command: {cmd}")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=os.getcwd(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await proc.communicate()
            return proc.returncode, stdout.decode(), stderr.decode()


EXECUTOR: ProcessPool | None = None


def init_process_pool(pool_size: int) -> None:
    global EXECUTOR
    EXECUTOR = ProcessPool(pool_size)


def reject_arguments(user_argv: list[str], reject_args: list[str]) -> None:
    # This is a very rudimentary implementation, since we could be getting false positives
    for reject_arg in reject_args:
        for user_arg in user_argv:
            if user_arg.strip().startswith(reject_arg):
                raise ToolError(f"Global argument {user_arg} is not allowed")


def strip_bearer_prefix(header: str) -> str:
    """Auxiliary function that removes the 'Bearer' prefix from OAuth header"""
    bearer, _, token = header.partition(" ")
    if bearer.lower() != "bearer":
        return header

    return token
