"""Agent HQ — optional cross-session control-plane topic.

Feature-flagged by ``CCGRAM_HQ_TOPIC_ID``. Public surface re-exported
here; call sites use subpackage-qualified imports.
"""

from .hq_commands import (
    agents_command,
    brief_command,
    handle_hq_text,
    hq_new_command,
    hq_status_command,
    is_hq_topic,
    needs_you_command,
)
from .tell import tell_command
from .voice_tell import handle_hq_voice

__all__ = [
    "agents_command",
    "brief_command",
    "handle_hq_text",
    "handle_hq_voice",
    "hq_new_command",
    "hq_status_command",
    "is_hq_topic",
    "needs_you_command",
    "tell_command",
]
