from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import logging
import sys
import traceback
from typing import Any, Callable
from uuid import uuid4


logger = logging.getLogger(__name__)


@dataclass
class LoggerContext:
    request_id: str = "-"
    client_id: str = "-"


ctx: ContextVar[str] = ContextVar("ctx", default=LoggerContext())


class InjectFilter(logging.Filter):
    def filter(self, record):
        ctx_value = ctx.get()
        record.request_id = ctx_value.request_id
        record.client_id = ctx_value.client_id
        return True


def init_logging(config) -> None:
    # Initialize root logger
    log_level = logging.DEBUG if config.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format=config.log_format,
        stream=sys.stderr,
        force=True,
    )

    # Add the filter to inject the request_id and client_id to log records
    f = InjectFilter()
    logging.getLogger().handlers[0].addFilter(f)


def tool_logger(func: Callable[..., Any]) -> Callable[..., Any]:
    """Logger for MCP tools."""

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        # ctx.request_id is always 2, so it's useless
        request_id = str(uuid4())
        if "ctx" in kwargs:
            client_id = kwargs["ctx"].client_id or "-"
        else:
            client_id = "-"
        ctx.set(LoggerContext(request_id=request_id, client_id=client_id))

        logger.debug(f"Running {func.__name__} with args: {args} and kwargs: {kwargs}")
        try:
            result = await func(*args, **kwargs)
            logger.debug(f"Result: {result}")
        except Exception as exc:
            # Show full traceback
            logger.error(f"Error running {func.__name__}: {exc}")
            logger.error(traceback.format_exc())
            raise
        return result

    return wrapper
