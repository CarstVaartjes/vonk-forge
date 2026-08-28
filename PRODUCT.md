# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Vonk Forge is for an operator or AI builder managing one or more NVIDIA DGX Spark systems from a local control plane. The primary job is to understand the fleet in seconds, make any suitable model available through an exact recipe, and safely change what one Spark or the whole fleet has installed and running.

## Product Purpose

Make it easy to run any model on one or more Sparks and make the resulting endpoint accessible. Success means an operator can answer three questions without translating backend concepts:

1. Which Sparks are in the fleet, are they healthy, and what is installed or running on each?
2. Which models and exact recipes are available, compatible, installed, and running—and where?
3. What will change if the operator manages one Spark or applies a saved fleet profile?

## Positioning

Vonk Forge joins a curated model-and-recipe library to authenticated live DGX Spark capacity, atomic multi-Spark placement, and reusable desired-state fleet profiles. The browser shows the same model, recipe, placement, installation, run, and health truth from either the fleet or model perspective.

## Operating Context

The control plane runs on a local NAS. Sparks enroll through an outbound native agent; PostgreSQL owns fleet and operational state. Routine operation does not require SSH, Git, direct browser-to-Spark access, or manually edited agent configuration. Models can have multiple immutable recipe revisions and topologies. A recipe may be installed on several complete Spark groups and may be running on zero or more groups.

The approved source briefs remain authoritative product history and requirements:

- `docs/superpowers/specs/2026-08-14-control-plane-experience-design.md`
- `docs/superpowers/specs/2026-08-17-fleet-library-product-simplification-design.md`

## Capabilities and Constraints

- Fleet membership comes from enrolled, non-revoked PostgreSQL `AgentNode` records.
- Human names and outcomes lead; identifiers, digests, raw evidence, and exact timestamps use progressive disclosure.
- “Installed” means the exact recipe image and artifacts exist on every required Spark. “Running” means every assigned rank is ready with fresh authenticated evidence.
- Multi-Spark recipes are one atomic placement and one operator action, never a collection of independent healthy-looking ranks.
- Capacity-sensitive actions use fresh inventory and telemetry, explain blockers, and show a server-authored impact preview before mutation.
- Saved fleet profiles describe complete desired installed/running recipe placements across selected Sparks. Applying a profile shows live-versus-desired differences and executes one confirmed, observable operation.
- Individual Spark management and whole-fleet profiles use the same primitives and terminology.
- This is a clean-slate product. There are no production installations to migrate and no requirement to retain legacy pages, schemas, adapters, routes, or data.
- Repository work must not mutate the live NAS or Sparks.

## Brand Commitments

The product is named Vonk Forge. Its voice is direct, calm, technically exact, and optimistic about making advanced local AI approachable. NVIDIA DGX Spark hardware colors and material character may inform accents and texture, but legibility and WCAG contrast take precedence; visually noisy metal or gold fields never sit behind operational text.

## Evidence on Hand

The repository contains real Fleet telemetry, inventory, installation, run, placement, recipe, catalog, enrollment, update, operation, and audit contracts plus representative test fixtures. No customer claims, production usage metrics, or live fleet data may be fabricated. Demonstration fixtures must be recognizable as illustrative test data.

## Product Principles

1. One system, two perspectives: Fleet answers “where”; Library answers “what”; every object cross-links.
2. State before controls: show health, current workload, installed inventory, capacity, and drift before asking for a decision.
3. Desired versus live: every change begins with an understandable diff and ends with observable progress.
4. Model-first language: recipes make a model runnable; internal pipelines never become the user’s navigation.
5. Safe speed: common individual and fleet-wide changes are quick, while exact evidence and consequential impact remain available.

## Accessibility & Inclusion

Target WCAG 2.2 AA. The complete experience must work with keyboard and assistive technology, never encode state through color alone, preserve visible unobscured focus, honor reduced motion, provide useful touch targets, and avoid document-level horizontal scrolling at 360, 768, 1280, and 1920 CSS pixels.
