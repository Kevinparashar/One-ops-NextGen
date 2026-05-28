"""Platform-owned conversational control plane.

Currently holds the Stage-1 conversation-control gate that runs BEFORE
routing — handles greetings, thanks, acks, farewells, help inquiries,
and structural noise with canned replies, never reaching the router /
disambiguator / planner.

See `control_gate.py` for the gate itself.
"""
from oneops.conversation.control_gate import (
    ControlType,
    ConversationControlResult,
    DragonflyControlCache,
    LlmControlClassifier,
    detect_conversation_control,
    get_control_classifier,
    set_control_classifier,
)

__all__ = [
    "ControlType",
    "ConversationControlResult",
    "DragonflyControlCache",
    "LlmControlClassifier",
    "detect_conversation_control",
    "get_control_classifier",
    "set_control_classifier",
]
