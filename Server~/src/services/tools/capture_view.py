from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from fastmcp import Context
from mcp.types import ToolAnnotations

from services.registry import mcp_for_unity_tool
from services.tools import choose_unity_instance
from services.tools.preflight import preflight
from services.tools.utils import coerce_bool, coerce_float, coerce_int, parse_json_payload
from transport.legacy.unity_connection import async_send_command_with_retry
from transport.unity_transport import send_with_unity_instance


@mcp_for_unity_tool(
    description=(
        "Capture one or more offscreen, non-XR camera views in Edit or Play mode without depending on Game View focus. "
        "Targets accept a hierarchy path, unique GameObject/bone name, or instance ID. Angles: front, side, back, top, 45, closeup. "
        "Outputs PNG absolute paths under Library/MCPScreenshots. Includes Screen Space - Overlay canvases by default. "
        "Use list_presets to inspect saved shots; delete_preset requires confirm=true."
    ),
    annotations=ToolAnnotations(title="Capture View", destructiveHint=True),
)
async def capture_view(
    ctx: Context,
    action: Annotated[Literal["capture", "list_presets", "delete_preset"], "Capture views or manage saved camera shot presets."] = "capture",
    target: Annotated[str, "Target hierarchy path, unique GameObject/bone name, or instance ID. Required for angle-based shots."] | None = None,
    angles: Annotated[list[str] | str, "One or more shot angles. A JSON array or comma-separated string is accepted."] | None = None,
    angle: Annotated[str, "Single-angle alias for angles."] | None = None,
    distance: Annotated[float | str, "Camera distance, or 'auto' (default) to fit Renderer bounds or estimated bone-local bounds."] | None = None,
    preset: Annotated[str, "Saved camera shot name. Cannot be combined with target, angles, or distance."] | None = None,
    frames: Annotated[int | str, "Frames per angle, 1-16; angles * frames must not exceed 16."] = 1,
    interval: Annotated[float | str, "Seconds between captures, 0-5; total waiting is capped at 60 seconds."] = 0.1,
    grid: Annotated[bool | str, "Also compose all captures into one PNG. Individual paths are still returned."] = False,
    file_name: Annotated[str, "Optional base PNG file name, without a directory."] | None = None,
    width: Annotated[int | str, "Image width, 64-4096; default 1280."] = 1280,
    height: Annotated[int | str, "Image height, 64-4096; default 720."] = 720,
    fov: Annotated[float | str, "Optional vertical field of view, 5-120 degrees."] | None = None,
    include_overlay: Annotated[bool | str, "Temporarily render Screen Space - Overlay canvases through the shot camera, then restore their state."] = True,
    include_inactive: Annotated[bool | str, "Resolve inactive targets without activating them; default false."] = False,
    confirm: Annotated[bool | str, "Required true only for delete_preset."] = False,
    unity_instance: Annotated[str, "Optional target Unity instance Name@hash. Overrides the session active instance for this call."] | None = None,
) -> dict[str, Any]:
    unity_instance = choose_unity_instance(ctx, unity_instance)
    if angles is not None and angle is not None:
        return {"success": False, "message": "Provide either angle or angles, not both."}
    angles = parse_json_payload(angles)
    if angles is not None and not isinstance(angles, (list, str)):
        return {"success": False, "message": "angles must be a string or an array of strings."}

    numeric_values = {
        "frames": coerce_int(frames, None),
        "width": coerce_int(width, None),
        "height": coerce_int(height, None),
    }
    for name, value in numeric_values.items():
        if value is None:
            return {"success": False, "message": f"{name} must be a valid integer."}
    interval_value = coerce_float(interval, None)
    if interval_value is None:
        return {"success": False, "message": "interval must be a valid number."}
    fov_value = coerce_float(fov, None)
    if fov is not None and fov_value is None:
        return {"success": False, "message": "fov must be a valid number."}

    distance_value: float | str | None = None
    if distance is not None:
        if isinstance(distance, str) and distance.strip().lower() == "auto":
            distance_value = "auto"
        else:
            distance_value = coerce_float(distance, None)
            if distance_value is None:
                return {"success": False, "message": "distance must be 'auto' or a valid number."}

    bool_values = {
        "grid": coerce_bool(grid, None),
        "includeOverlay": coerce_bool(include_overlay, None),
        "includeInactive": coerce_bool(include_inactive, None),
        "confirm": coerce_bool(confirm, None),
    }
    for name, value in bool_values.items():
        if value is None:
            return {"success": False, "message": f"{name} must be a boolean."}

    angle_count = len(angles) if isinstance(angles, list) else 1
    capture_count = angle_count * numeric_values["frames"] if action == "capture" else 1
    wait_seconds = max(0.0, capture_count - 1) * interval_value
    gate = await preflight(ctx, unity_instance=unity_instance, wait_for_no_compile=True, refresh_if_dirty=False)
    if gate is not None:
        return gate.model_dump()

    params: dict[str, Any] = {
        "action": action,
        "frames": numeric_values["frames"],
        "interval": interval_value,
        "width": numeric_values["width"],
        "height": numeric_values["height"],
        **bool_values,
        "timeoutSeconds": min(120, max(30, math.ceil(wait_seconds + 30))),
    }
    optional = {
        "target": target,
        "angles": angles,
        "angle": angle,
        "distance": distance_value,
        "preset": preset,
        "fileName": file_name,
        "fov": fov_value,
    }
    params.update({key: value for key, value in optional.items() if value is not None})

    try:
        response = await send_with_unity_instance(
            async_send_command_with_retry,
            unity_instance,
            "capture_view",
            params,
        )
        return response if isinstance(response, dict) else {"success": False, "message": str(response)}
    except Exception as exc:
        return {"success": False, "message": f"Python error capturing view: {exc}"}
