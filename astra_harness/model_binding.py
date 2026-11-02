"""Provider-neutral identity exchanged by the host catalog and runtime."""
from dataclasses import dataclass, field


class ModelProfileError(RuntimeError):
    STATUS = {
        "model_profiles_disabled": 403, "invalid_model_profile": 400,
        "model_profile_conflict": 409, "model_profile_not_found": 404,
        "model_profile_in_use": 409, "model_profile_unavailable": 409,
        "model_profile_changed": 409, "model_credentials_changed": 409,
        "model_credentials_missing": 409, "model_session_mismatch": 409,
        "model_profile_not_selected": 409,
        "model_credentials_invalid": 400, "model_key_storage_unavailable": 409,
    }

    def __init__(self, code):
        self.code = code if code in self.STATUS else "model_profile_unavailable"
        self.status = self.STATUS[self.code]
        super().__init__(self.code)


@dataclass(frozen=True)
class ModelBinding:
    snapshot: dict
    identity: str
    credential_seal: str = field(repr=False)
    model: object = field(repr=False)
