import functools
import inspect
import logging
from typing import Callable, Any

logger = logging.getLogger("mcp-for-unity-server")

# Hard cap for a single logged payload. Hierarchy dumps and screenshots can be
# MBs of JSON/base64; formatting and synchronously writing them (with
# RotatingFileHandler rollover churn) on the event loop made busy sessions
# progressively sluggish.
_MAX_LOG_PAYLOAD_CHARS = 2048


def _format_payload(value: Any) -> str:
    """Stringify a payload with a hard length cap."""
    try:
        text = str(value)
    except Exception:
        return "<unrepresentable>"
    if len(text) <= _MAX_LOG_PAYLOAD_CHARS:
        return text
    return f"{text[:_MAX_LOG_PAYLOAD_CHARS]}...<truncated {len(text) - _MAX_LOG_PAYLOAD_CHARS} chars>"


def log_execution(name: str, type_label: str):
    """Decorator to log input arguments and return value of a function."""
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def _sync_wrapper(*args, **kwargs) -> Any:
            if logger.isEnabledFor(logging.INFO):
                logger.info(
                    "%s '%s' called with args=%s kwargs=%s",
                    type_label, name, _format_payload(args), _format_payload(kwargs))
            try:
                result = func(*args, **kwargs)
                if logger.isEnabledFor(logging.INFO):
                    logger.info("%s '%s' returned: %s",
                                type_label, name, _format_payload(result))
                return result
            except Exception as e:
                logger.info("%s '%s' failed: %s", type_label, name, e)
                raise

        @functools.wraps(func)
        async def _async_wrapper(*args, **kwargs) -> Any:
            if logger.isEnabledFor(logging.INFO):
                logger.info(
                    "%s '%s' called with args=%s kwargs=%s",
                    type_label, name, _format_payload(args), _format_payload(kwargs))
            try:
                result = await func(*args, **kwargs)
                if logger.isEnabledFor(logging.INFO):
                    logger.info("%s '%s' returned: %s",
                                type_label, name, _format_payload(result))
                return result
            except Exception as e:
                logger.info("%s '%s' failed: %s", type_label, name, e)
                raise

        return _async_wrapper if inspect.iscoroutinefunction(func) else _sync_wrapper
    return decorator
