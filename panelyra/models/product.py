"""Commercial product identity and workspace metadata."""

from __future__ import annotations

from dataclasses import dataclass


PRODUCT_NAME = "Panelyra"
PRODUCT_TAGLINE = "Your chapters, in one place."
PRODUCT_VERSION = "0.16.0"
PRODUCT_MODE = "Local-only"


@dataclass(frozen=True)
class Workspace:
    key: str
    name: str
    description: str
    accent: str


WORKSPACES = {
    "engage": Workspace(
        key="engage",
        name="Panelyra Engage",
        description="Generate, submit, verify, and review contextual chapter comments.",
        accent="cyan",
    ),
    "capture": Workspace(
        key="capture",
        name="Panelyra Capture",
        description="Collect chapter images, CBZ archives, and OCR transcripts.",
        accent="green",
    ),
    "control": Workspace(
        key="control",
        name="Panelyra Control",
        description="Monitor health, history, storage, settings, and local operations.",
        accent="neutral",
    ),
}


NAVIGATION_GROUPS = {
    "Library & downloads": ["Library", "Downloads", "Capture"],
    "Tools": ["Engage", "Activity", "Settings"],
}
PAGE_RUNTIME_TYPES = {"Engage": "comment", "Capture": "download"}


def runtime_type_for_page(page: str) -> str:
    for prefix, runtime_type in PAGE_RUNTIME_TYPES.items():
        if page.startswith(prefix):
            return runtime_type
    return ""
