"""Small host/runtime contract; filesystem policy belongs to host adapters."""
from dataclasses import dataclass
from typing import Callable


class WorkspaceError(RuntimeError):
    STATUS = {"workspace_disabled": 403, "invalid_workspace": 400, "protected_workspace": 400,
              "workspace_overlap": 409, "workspace_changed": 409, "workspace_conflict": 409,
              "workspace_unavailable": 409, "workspace_not_selected": 409,
              "workspace_not_configured": 409, "workspace_identity_unavailable": 409,
              "workspace_session_mismatch": 409}

    def __init__(self, code):
        if code not in self.STATUS:
            code = "workspace_unavailable"
        self.code, self.status = code, self.STATUS[code]
        super().__init__(code)


@dataclass(frozen=True)
class WorkspaceBinding:
    id: str
    path: str
    identity: str
    tools: tuple
    permissions: tuple
    file_plugin: object = None
    validate: Callable[[], None] | None = None
