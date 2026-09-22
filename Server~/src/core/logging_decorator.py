import functools
import inspect
import logging
from typing import Callable, Any

logger = logging.getLogger("mcp-for-unity-server")

# Hard cap for a single logged payload. Hierarchy dumps and screenshots can be
# MBs of JSON/base64; previously they were str()-ed in full and synchronously
# written (with RotatingFileHandler rollover churn) on the event loop for every
# call. The formatter below never stringifies large values in full.
_MAX_LOG_PAYLOAD_CHARS = 2048


def _bounded_str(value: Any, budget: int) -> tuple[str, bool]:
    """Render ``value`` within ``budget`` chars; returns (text, truncated).

    Never str()-ifies a large payload: strings are sliced (O(budget)), bytes
    are summarized by length, containers recurse with a shrinking budget,
    pydantic models go through model_dump() (field strings are shared, not
    copied) and take the dict path.
    """
    if budget <= 0:
        return "...", True
    if isinstance(value, str):
        if len(value) <= budget:
            return value, False
        return f"{value[:budget]}...<+{len(value) - budget} chars>", True
    if isinstance(value, bytes):
        return f"<bytes len={len(value)}>", False
    if value is None or isinstance(value, (bool, int, float)):
        return str(value), False
    if hasattr(value, "model_dump") and callable(value.model_dump):
        try:
            value = value.model_dump()
        except Exception:
            pass
    if isinstance(value, dict):
        parts: list[str] = []
        truncated = False
        for key, item in value.items():
            k = key if isinstance(key, str) else repr(key)
            entry_budget = budget - sum(len(p) for p in parts) - len(k) - 4
            if entry_budget <= 0:
                truncated = True
                break
            item_text, item_truncated = _bounded_str(item, entry_budget)
            truncated = truncated or item_truncated
            parts.append(f"{k!r}: {item_text}" if not isinstance(key, str) else f"{k}: {item_text}")
        body = ", ".join(parts)
        return ("{" + body + ", ...}", True) if truncated else ("{" + body + "}", False)
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        parts = []
        truncated = False
        open_c, close_c = ("[", "]") if isinstance(value, list) else ("(", ")")
        for item in items:
            entry_budget = budget - sum(len(p) for p in parts) - 4
            if entry_budget <= 0:
                truncated = True
                break
            item_text, item_truncated = _bounded_str(item, entry_budget)
            truncated = truncated or item_truncated
            parts.append(item_text)
        if truncated or len(parts) < len(items):
            truncated = True
        body = ", ".join(parts)
        suffix = ", ..." if truncated else ""
        return (open_c + body + suffix + close_c), truncated
    text = str(value)
    if len(text) <= budget:
        return text, False
    return f"{text[:budget]}...<+{len(text) - budget} chars>", True


def _format_payload(value: Any) -> str:
    """Stringify a payload with a hard length cap (see _bounded_str)."""
    try:
        text, _ = _bounded_str(value, _MAX_LOG_PAYLOAD_CHARS)
    except Exception:
        return "<unrepresentable>"
    return text


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
                logger.info("%s '%s' failed: %s",
                            type_label, name, _format_payload(e))
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
                logger.info("%s '%s' failed: %s",
                            type_label, name, _format_payload(e))
                raise

        return _async_wrapper if inspect.iscoroutinefunction(func) else _sync_wrapper
    return decorator
