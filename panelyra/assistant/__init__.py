"""Grounded Panelyra copilot services."""

from .copilot import (
    AssistantAction,
    AssistantEvidence,
    AssistantTurnPlan,
    CopilotContext,
    WorkspaceRun,
    assistant_requests_browser_inspection,
    build_model_prompt,
    classify_intent,
    extract_log_events,
    explicit_capture_review_request,
    needs_external_research,
    plan_assistant_turn,
    parse_configuration,
)
from .local_model import (
    DEFAULT_LOCAL_MODEL,
    PANELYRA_SYSTEM_PROMPT,
    LocalModelClient,
    LocalModelError,
    LocalModelStatus,
)
from .conversation import CONVERSATIONS, ConversationStore
from .knowledge import (
    GroundingBundle,
    KnowledgeDocument,
    PageContext,
    PanelyraKnowledgeService,
    PanelyraSnapshot,
)

__all__ = [
    "AssistantAction",
    "AssistantEvidence",
    "AssistantTurnPlan",
    "CopilotContext",
    "WorkspaceRun",
    "build_model_prompt",
    "assistant_requests_browser_inspection",
    "classify_intent",
    "extract_log_events",
    "explicit_capture_review_request",
    "needs_external_research",
    "plan_assistant_turn",
    "parse_configuration",
    "DEFAULT_LOCAL_MODEL",
    "PANELYRA_SYSTEM_PROMPT",
    "LocalModelClient",
    "LocalModelError",
    "LocalModelStatus",
    "CONVERSATIONS",
    "ConversationStore",
    "GroundingBundle",
    "KnowledgeDocument",
    "PageContext",
    "PanelyraKnowledgeService",
    "PanelyraSnapshot",
]
