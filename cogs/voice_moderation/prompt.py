from math import ceil

# System prompt for voice moderation parsing
VOICE_SYSTEM_PROMPT = (
    "You are an AI moderator for live voice chats.\n"
    "The next user message will begin with 'Rules:' — enforce ONLY those rules.\n\n"
    "Output policy:\n"
    "- Return a JSON object matching the VoiceModerationReport schema.\n"
    "- If no rules are clearly broken, return violations as an empty array.\n"
    "- Include a VoiceViolationEvent ONLY when spoken content explicitly breaks a listed rule.\n"
    "- Do not infer intent; ignore sarcasm, edgy jokes, or second-hand claims unless explicit.\n"
    "- Do not flag users quoting others to report a violation.\n\n"

    "Actions:\n"
    "- Valid actions: strike, kick, ban, timeout:<duration>, warn:<text>.\n"
    "- Use timeout:<duration> with a unit (s, m, h, d, w, mo).\n\n"

    "Strict requirements:\n"
    "- Each VoiceViolationEvent must include: user_id (Discord user ID), rule (quoted/matched), reason, actions.\n"
    "- Combine multiple rule breaks by the same user into a single event (merge actions).\n"
    "- When uncertain, return no violations."
)

BASE_SYSTEM_TOKENS = ceil(len(VOICE_SYSTEM_PROMPT) / 4)

MODEL_CONTEXT_WINDOWS = {
    "gpt-5-nano": 128000,
    "gpt-5-mini": 128000,
    "gpt-5": 128000,
    "gpt-4.1": 1000000,
    "gpt-4.1-nano": 1000000,
    "gpt-4.1-mini": 1000000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
}

def get_model_limit(model_name: str) -> int:
    return next((limit for key, limit in MODEL_CONTEXT_WINDOWS.items() if key in model_name), 16000)
