from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from fastmcp import Context
from mcp.types import ToolAnnotations

from services.registry import mcp_for_unity_tool
from services.state.instance_locks import get_instance_write_locks, resolve_lock_instance
from services.tools import choose_unity_instance
from services.tools.utils import coerce_float, coerce_int
from transport.unity_instance_middleware import get_unity_instance_middleware


@mcp_for_unity_tool(
    description=(
        "Coordinate Unity edits across MCP sessions. claim reserves this project's write lease; "
        "renew extends it; release relinquishes it after active calls finish; status is read-only. "
        "Owner identity comes from the MCP session, never from the display name. Read tools need no lease."
    ),
    annotations=ToolAnnotations(title="Claim Unity Instance", destructiveHint=True),
)
async def claim_instance(
    ctx: Context,
    action: Annotated[Literal["claim", "renew", "status", "release"], "Lease operation; default claim."] = "claim",
    owner: Annotated[str, "Human-readable task or session name; not an authorization identity."] = "MCP session",
    ttl_seconds: Annotated[float | int | str, "Idle lease TTL, 10-3600 seconds; default 300. Active write calls do not expire."] = 300,
    unity_instance: Annotated[str, "Target Name@hash or hash; defaults to this session's selected instance."] | None = None,
) -> dict[str, Any]:
    instance = choose_unity_instance(ctx, unity_instance)
    owner_key = get_unity_instance_middleware().get_session_key(ctx)
    if not owner_key:
        return {
            "success": False, "code": "mcp_session_identity_required",
            "error": "A stable MCP client_id, Codex thread, or FastMCP session_id is required for write coordination.",
        }
    ttl = coerce_float(ttl_seconds, None)
    if ttl is None or not math.isfinite(ttl) or not 10 <= ttl <= 3600:
        return {"success": False, "error": "ttl_seconds must be a finite number between 10 and 3600."}
    owner = owner.strip()[:160] if isinstance(owner, str) else "MCP session"
    if not owner:
        return {"success": False, "error": "owner must not be empty."}
    try:
        if action == "status":
            project_hash = None
            if instance:
                project_hash, _ = await resolve_lock_instance(instance)
            return {"success": True, "data": {"locks": await get_instance_write_locks().status(project_hash)}}
        project_hash, label = await resolve_lock_instance(instance)
        if action == "release":
            result = await get_instance_write_locks().release(project_hash, owner_key)
        else:
            result = await get_instance_write_locks().claim(project_hash, label, owner_key, owner, ttl)
        if result.get("success"):
            result["message"] = f"Unity write lease {action} completed for {label}."
        return result
    except Exception as exc:
        return {"success": False, "code": "unity_instance_unreachable", "error": str(exc), "retryable": True}
