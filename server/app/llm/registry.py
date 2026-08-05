"""Per-provider capability declarations driving the LlmChooser UI: the client only
shows the knobs (model / effort / context window) a provider actually supports."""
from .runner import RUNNERS

CAPABILITIES = {
    "claude": {
        "label": "Claude",
        "models": [
            "claude-4-sonnet",
            "claude-4.8-opus",
            "claude-4.7-opus",
            "claude-4.6-opus",
            "claude-3-7-sonnet",
            "claude-3-5-sonnet",
            "claude-3-5-haiku",
            "claude-3-opus",
            "sonnet",
            "opus",
            "haiku",
        ],
        "default_model": "claude-4-sonnet",
        "supports_effort": True,
        "efforts": ["low", "medium", "high"],
        "supports_context_window": False,
        "supports_vision": True,
    },
    "antigravity": {
        "label": "Antigravity",
        "models": [
            "gemini-3.6-flash",
            "gemini-3.6-pro",
            "gemini-3.5-flash",
            "gemini-3.5-pro",
            "gemini-3.1-pro",
            "claude-4.6-sonnet",
            "claude-4.6-opus",
            "claude-3-7-sonnet",
            "claude-3-5-sonnet",
            "claude-3-5-haiku",
            "gpt-4o",
            "gpt-oss-120b",
        ],
        "default_model": "gemini-3.6-flash",
        "supports_effort": True,
        "efforts": ["low", "medium", "high"],
        "supports_context_window": False,
        "supports_vision": True,
    },
}


def providers_info() -> list[dict]:
    return [
        {
            "name": name,
            **caps,
            "available": RUNNERS[name].available(),
        }
        for name, caps in CAPABILITIES.items()
    ]
