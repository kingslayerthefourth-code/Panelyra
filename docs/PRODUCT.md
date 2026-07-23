# Panelyra Product

**Tagline:** Automation for every chapter.

## Product Modules

- **Engage:** contextual comment workflow QA.
- **Capture:** authorized chapter image, CBZ, and OCR collection.
- **Studio:** Word Art and editor upload workflow QA.
- **Copilot:** grounded help, run diagnosis, workflow configuration, browser inspection, and support-ticket assistance.
- **Control:** overview, run history, storage, settings, health, support, and help.

## Copilot Contract

Copilot combines a local product knowledge base with structured run records, log events, browser state, saved output, and support tickets. Ask, Diagnose, Configure, and Support modes can work locally; an optional model integration improves language quality and streams responses when configured.

Actions are deliberately constrained. Copilot may navigate, inspect Chrome, validate or fill configuration, prepare a retry draft, and create a local support ticket. Starting or stopping automation, submitting content, publishing, deleting, and uploading remain explicit user actions.

## Reader Compatibility

Engage and Capture use one shared routing contract with distinct selectors for internal pages, chapter transitions, counters, and chapter lists. The supported adapter families are continuous long-strip readers, one-chapter-per-URL readers, button or URL paginated readers, and mixed paginated-plus-next-chapter readers.

Auto detection is the default. Named selector profiles are the extension point for provider-specific DOM structures.

## Operating Boundary

Panelyra is local-first and has no required cloud account, telemetry, or license service. Account and licensing integration can be added behind the Control service boundary without coupling it to Selenium engines or local data.

## Design Direction

The dashboard uses a charcoal shell, restrained neutral surfaces, white text, muted gray secondary text, cyan action emphasis, and green/amber/red only for state. Space Grotesk provides display hierarchy and Manrope provides compact interface text.
