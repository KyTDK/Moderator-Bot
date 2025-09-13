from pydantic import BaseModel

class VoiceViolationEvent(BaseModel):
    user_id: int
    rule: str
    reason: str
    actions: list[str]

class VoiceModerationReport(BaseModel):
    violations: list[VoiceViolationEvent]

