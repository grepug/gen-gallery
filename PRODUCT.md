# Product

## Register

product

## Users

Primary users are the operator of the local image generation service, usually working alone on a desktop machine. They monitor generations, review outputs, retry failures, and clean up bad or stale jobs while other scripts continue submitting work in the background.

## Product Purpose

This product is an internal operations surface for a queue-backed image generation service. It should make it fast to understand what the system is doing, inspect results, navigate across nearby outputs, and take corrective action on failed jobs without dropping to the terminal.

## Brand Personality

Calm, precise, work-focused.

## Anti-references

- Marketing-style galleries with oversized hero treatments
- Neon-dark dashboards that bury status inside decoration
- Dense admin tables that make image review feel secondary

## Design Principles

- Images first, operations second: the output should stay visually central while controls remain close at hand.
- Quiet familiarity: use standard product UI patterns so the operator can stay in flow.
- Status should be glanceable: active, failed, and succeeded jobs must read instantly.
- Recovery should be local: retry and delete actions belong next to the selected job, not hidden behind navigation.

## Accessibility & Inclusion

Target solid keyboard access, visible focus states, clear contrast, and reduced-motion-safe transitions. Core navigation should work with arrow keys and buttons, not hover-only affordances.
