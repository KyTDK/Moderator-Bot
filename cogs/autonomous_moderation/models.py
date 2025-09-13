from pydantic import BaseModel

class ViolationEvent(BaseModel):
    rule: str
    reason: str
    actions: list[str]
    message_ids: list[str]

class ModerationReport(BaseModel):
    violations: list[ViolationEvent]
