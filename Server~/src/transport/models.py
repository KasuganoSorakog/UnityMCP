from typing import Any
from pydantic import BaseModel, Field
from models.models import ToolDefinitionModel

# Outgoing (Server -> Plugin)


class WelcomeMessage(BaseModel):
    type: str = "welcome"
    serverTimeout: int
    keepAliveInterval: int


class RegisteredMessage(BaseModel):
    type: str = "registered"
    session_id: str


class ExecuteCommandMessage(BaseModel):
    type: str = "execute"
    id: str
    name: str
    params: dict[str, Any]
    timeout: float


class CancelCommandMessage(BaseModel):
    """Ask the plugin to cancel a command the server has timed out on.

    Older plugins ignore unknown message types, so this is safe to send to
    clients that do not implement cancellation.
    """

    type: str = "cancel"
    id: str

# Incoming (Plugin -> Server)


class RegisterMessage(BaseModel):
    type: str = "register"
    project_name: str = "Unknown Project"
    project_hash: str
    unity_version: str = "Unknown"
    project_path: str | None = None
    package_version: str | None = None
    current_scene: str | None = None
    capabilities_version: str | None = None


class RegisterToolsMessage(BaseModel):
    type: str = "register_tools"
    tools: list[ToolDefinitionModel]


class PongMessage(BaseModel):
    type: str = "pong"
    session_id: str | None = None


class CommandResultMessage(BaseModel):
    type: str = "command_result"
    id: str
    result: dict[str, Any] = Field(default_factory=dict)

# Session Info (API response)


class SessionDetails(BaseModel):
    project: str
    hash: str
    unity_version: str
    connected_at: str
    project_path: str | None = None
    package_version: str | None = None
    current_scene: str | None = None
    capabilities_version: str | None = None


class SessionList(BaseModel):
    sessions: dict[str, SessionDetails]
