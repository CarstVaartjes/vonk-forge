# Fleet operating system design

**Date:** 2026-08-28
**Status:** implementation authority for the clean-slate controller redesign
**Extends:** the approved control-plane experience and Fleet/Library simplification designs

## North star

Vonk Forge makes any suitable model easy to run on one or more Sparks and easy to access afterward. The interface must answer, without UUID translation:

1. What Sparks do I have, are they healthy, and what is installed or running on each?
2. What models can I run, through which exact recipes, and where is each recipe installed or running?
3. What will change if I manage one Spark or apply a complete fleet profile?

This is a direct replacement. No production installation exists, so legacy navigation, data shapes, compatibility paths, and migration concerns cannot weaken the result.

## Research synthesis

The interaction model adopts these proven patterns while keeping Vonk Forge's stronger safety boundary:

- Kubernetes labels and selectors demonstrate that fleet groupings should be cross-cutting metadata, not a rigid hierarchy. Spark labels power filters, saved scopes, and profile targeting.
- Tailscale's Machines experience demonstrates that a device inventory needs free-form search, explicit health/last-seen filters, tags, and contextual device actions in one surface.
- Grafana's variables and repeated panels demonstrate that fleet-wide filters must affect every summary and repeated Spark panel consistently, and that URL state makes an operational view shareable.
- Argo CD's live-versus-desired diff makes a dry-run preview the natural language for profile application. Vonk Forge previews exact model, recipe, Spark, capacity, stop, install, and run effects rather than raw manifests.
- Nomad node pools and placement constraints separate reusable Spark groups from hard scheduling requirements. Vonk Forge profiles use exact placements while labels accelerate selection and composition.
- Nomad and Argo progressive rollout patterns support an explicit one-at-a-time strategy for consequential fleet-wide platform changes; recipe profile changes remain topology-atomic.
- Hugging Face and NVIDIA NGC show that discovery begins with a model or task, then exposes deployable providers/artifacts. Vonk Forge begins with the model and treats recipes as exact, comparable ways to make it runnable on this fleet.

Primary references are the official Kubernetes, Tailscale, Grafana, Argo CD, HashiCorp Nomad, Hugging Face, and NVIDIA NGC documentation.

## One operating model

```text
Model version
  -> one or more immutable recipes
    -> one fixed topology per selected recipe revision
      -> one or more exact Spark placements
        -> installed state
          -> running state and accessible endpoint

Fleet profile
  -> a named complete set of desired recipe placements
    -> live-versus-desired preview
      -> one durable, observable application
```

Fleet and Library are different lenses over the same graph:

- **Fleet** starts with Sparks and answers where capacity and workloads live.
- **Library** starts with models and answers what can run and which recipe is best.
- **Profiles** are a first-class Fleet workspace, not a third disconnected inventory. They compose Library recipes onto Fleet Sparks.
- **Activity and Audit** are contextual timelines reached from the user menu or an affected object, not a primary workspace.

## Fleet workspace

The first viewport is an operational board, not a page header. It contains:

- live connection state and evidence age;
- online Sparks, running models, complete installations, available unified memory, and warnings;
- a prominent current-profile/live-drift control;
- one search/filter row for name, status, label, installed model, and update state;
- a compact per-Spark strip that shows status, running workload, memory headroom, installation count, and the next relevant action.

Fleet has three task-preserving views:

1. **Overview** — responsive Spark workcells for fast health and capacity scanning.
2. **Workload matrix** — Sparks by model/recipe state, making installed/running topology visible across the fleet.
3. **Topology** — complete multi-Spark placements and routes, with incomplete groups called out rather than visually repaired.

Selecting a Spark opens an in-page inspector with Overview, Models, Performance, and Identity sections. The Models section can install/run another recipe, stop a run, or uninstall an exact installation without leaving Fleet. It reuses Library placement and preview contracts.

## Library workspace

Library begins with model discovery rather than a recipe inventory. Search and filters cover capability, publisher, qualification, Spark count, installed/running state, and present fleet fit.

Each model result shows:

- model and exact version identity;
- capabilities and intended task;
- number of local and public recipes;
- best current recipe for this fleet and why;
- whether any recipe can install now;
- installed and running coverage across named Sparks.

A model detail compares its recipes on topology, runtime, download, memory, qualification, installed coverage, running endpoints, and blockers. Selecting a recipe opens a single action area: choose recommended placement, install, run, add to profile, or inspect exact evidence. Public import and custom creation are empty-state and secondary actions, not competing top-level workflows.

## Saved fleet profiles

A fleet profile is server-owned desired state with:

- name, description, favorite status, and optional labels;
- an installation policy: **keep cached recipes** or **exact installed set**;
- one or more assignments pinned to an immutable recipe revision and exact ordered Spark ranks;
- desired state per assignment: installed or running;
- stable alias for a running endpoint when required.

The profile composer is a Spark-by-assignment board. Operators can start from the live fleet, an empty profile, or a previous profile; add recipes from Library; use recommended complete placements; and see capacity before saving.

Preview compares live and desired state and groups work into:

- already correct;
- stop conflicting run;
- retain or uninstall cached recipe;
- create placement;
- distribute image/artifacts;
- install recipe;
- start recipe and publish endpoint;
- blocked, with the smallest next action.

Applying a profile creates one durable application with topology-atomic child operations. Closing the browser cannot lose the application. Every step is digest-bound, idempotent, resumable, and visible in the affected profile, Spark, recipe, and activity timelines.

## Visual direction

The interface uses the discipline of a precision machine workcell rather than a generic dark dashboard:

- powder-coated graphite shell, light inspection surfaces, and warm metallic separators echo DGX Spark hardware without putting texture behind text;
- hard-working system typography, tabular numerals, compact measurement labels, and strict alignment make dense data calm;
- mint/green means selected or ready, amber means attention, red means blocked/danger, and every state also has text and shape;
- cards behave like labeled work fixtures with one strong edge and generous internal whitespace, not floating rounded rectangles;
- live/desired changes move through a clear left-to-right operation rail;
- motion is limited to fresh telemetry, progress, and state transitions and is disabled under reduced motion.

## Clean-slate implementation boundaries

- PostgreSQL remains the sole Fleet and profile authority.
- No browser-local profile is treated as authoritative.
- No direct SSH, browser-to-agent connection, or long-lived bootstrap secret is introduced.
- No compatibility route, redirect, dual write, or legacy profile reader is added.
- Existing recipe mapping, admission, operation, and route-publication safety contracts are reused rather than bypassed.
- Live NAS and Sparks remain out of scope; verification is repository, fixture, browser, and CI only.

## Completion evidence

Completion requires all of the following:

- reset-state and populated-state browser journeys for Fleet, Library, individual Spark management, profile composition, profile preview, application progress, recovery, and deep links;
- typed API and database contracts for saved profiles and durable applications;
- unit and integration coverage for profile digests, current-state comparison, idempotency, step ordering, interruption/resume, topology atomicity, and exact-install policy;
- no primary navigation beyond Fleet and Library;
- no document overflow at 360, 768, 1280, or 1920 CSS pixels;
- WCAG 2.2 AA semantics, keyboard operation, visible focus, live status announcements, and reduced motion;
- production build, full web suite, browser acceptance, complete control suite, and repository CI green;
- final visual review against the direction contract and durable `DESIGN.md` documentation.
