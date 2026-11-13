---
name: Vonk Forge Control
description: A precision Spark workcell for inspecting live and desired local AI state.
colors:
  bg: "#090a09"
  recess: "#060706"
  panel: "#121412"
  panel-2: "#0f110f"
  raised: "#1a1d19"
  hover: "#20231e"
  line: "#262924"
  line-strong: "#3a3e36"
  line-control: "#666b5f"
  ink: "#f2efe8"
  muted: "#aaa396"
  faint: "#958e80"
  green: "#76b900"
  green-text: "#a3d93a"
  green-ink: "#0b0d08"
  gold: "#d9b56f"
  gold-text: "#d9b56f"
  ok: "#a3d93a"
  warn: "#ffb347"
  bad: "#ff7a6b"
  info: "#8fc4ff"
  model-ink: "#f2efe8"
  mem-system: "#7f7a6e"
  track: "#2a2e27"
  series-1: "#6aa82e"
  series-2: "#4a8ae6"
  series-3: "#c4862c"
  series-4: "#c765ac"
  neutral-series: "#8d877a"
  grid: "#23261f"
  axis: "#958e80"
typography:
  display:
    fontFamily: "Onest Variable, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "clamp(1.55rem, 3vw, 2.2rem)"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "-0.04em"
  headline:
    fontFamily: "Onest Variable, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "1.15rem"
    fontWeight: 760
    lineHeight: 1.15
    letterSpacing: "-0.035em"
  title:
    fontFamily: "Onest Variable, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "1rem"
    fontWeight: 800
    lineHeight: 1.2
    letterSpacing: "normal"
  body:
    fontFamily: "Onest Variable, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "normal"
  label:
    fontFamily: "Onest Variable, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "0.7rem"
    fontWeight: 800
    lineHeight: 1.2
    letterSpacing: "0.08em"
rounded:
  card: "1rem"
  inner: "0.625rem"
  control: "0.5rem"
  pill: "999px"
spacing:
  compact: "0.35rem"
  control: "0.65rem"
  fixture: "0.75rem"
  section: "1rem"
  shell: "1.25rem"
components:
  button-primary:
    backgroundColor: "{colors.green}"
    textColor: "{colors.green-ink}"
    rounded: "{rounded.control}"
    padding: "0.7rem 1rem"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0.7rem 1rem"
  search-input:
    backgroundColor: "{colors.recess}"
    textColor: "{colors.ink}"
    rounded: "{rounded.control}"
    padding: "0.7rem"
  nav-link-active:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.green-text}"
    rounded: "{rounded.inner}"
    padding: "0.65rem 0.75rem"
  status-pill-healthy:
    backgroundColor: "{colors.raised}"
    textColor: "{colors.green-text}"
    rounded: "{rounded.pill}"
    padding: "0.25rem 0.55rem"
  spark-workcell:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.ink}"
    rounded: "{rounded.card}"
    padding: "1rem"
---

# Design System: Vonk Forge Control

## Overview

**Creative North Star: "The Spark Workcell"**

Vonk Forge feels like a local AI CNC-machine inspection board: a powder-coated graphite control shell with restrained champagne hardware cues. The shell is calm and compact; live fleet state, installed and running models, and discrepancies read like a job sheet of aligned rows.

Precision comes from rows, rails, thin dividers, tabular measurements, and exact state language rather than ornamental dashboard chrome. NVIDIA green is deliberately scarce and belongs to positive health, readiness, and safe primary action. Amber requests attention, red proves failure, and blue identifies installed or informational state; text and structure always repeat the meaning.

**Key Characteristics:**

- Graphite hardware shell with champagne separators and labels.
- One graphite surface family; state is carried by words, dots, and edges.
- Dense, aligned operating rows with generous local whitespace.
- Green reserved for positive health, readiness, and safe action.
- Flat fixtures first; ambient depth only where hierarchy requires it.

## Colors

The palette is one graphite operating shell with a small state vocabulary that text and shape reinforce.

### Primary

- **NVIDIA Action Green:** The scarce primary accent for healthy, ready, or safe-to-apply states and the main affirmative action.
- **Charged Green:** A lighter green for legible positive text and active detail against graphite.
- **Green Ink:** The near-black foreground used on solid green controls.

### Secondary

- **Champagne Titanium:** A restrained hardware cue for authority notes, separators, and quiet contextual metadata; never an operational state.
- **Aged Titanium:** The muted companion for low-priority shell copy.

### Tertiary

- **Attention Amber:** Drift, delay, capacity concern, and operator attention.
- **Proven Failure Red:** Offline, blocked, degraded, or failed evidence only.
- **Installed Blue:** Installed state and neutral informational evidence.

### Neutral

- **Graphite Base:** The page ground behind the controller.
- **Sidebar Graphite:** The distinct header bar behind the navigation.
- **Panel Graphite:** The default dark fixture and form surface.
- **Raised Graphite:** Selected or hovered structural layers that remain subordinate to state color.
- **Recessed Graphite:** Search wells, metric cells, and inset evidence areas.
- **Structural Line / Strong Structural Line:** Thin separators and control outlines that carry most grouping.
- **Primary Text / Muted Text / Subtle Text:** The three-level graphite-surface text hierarchy.

### Named Rules

**The Green Means Go Rule.** Reserve green for positive health, readiness, and safe primary action; never use it as general decoration or as the only state signal.

## Typography

**Display Font:** UI Sans (with system-ui and platform sans-serif fallbacks)
**Body Font:** UI Sans (with system-ui and platform sans-serif fallbacks)
**Label/Mono Font:** UI Monospace (with SFMono-Regular, Consolas, and monospace fallbacks) for identifiers and exact technical evidence only.

**Character:** The system uses one hard-working sans family, tight headings, compact body text, and tabular numerals. Hierarchy comes from weight, alignment, and density rather than a decorative display face.

### Hierarchy

- **Display** (700, fluid compact display, tight line-height): Library-level titles and the rare page title that must orient a whole workspace.
- **Headline** (760, compact, tight tracking): Command titles, workcell headings, and primary section names.
- **Title** (800, compact): Spark names, recipe names, and decisive fixture labels.
- **Body** (400, compact, 1.45 line-height): Operational explanations, helper text, and result context; keep paragraphs narrow enough to scan inside their fixture.
- **Label** (800, widely tracked, uppercase): Measurement headings, field labels, table axes, and small control categories.

### Named Rules

**The Measure Before Decorate Rule.** Use tabular numerals for counts, capacity, percentages, versions, and timestamps; use monospace only for identifiers, digests, and commands.

## Layout

The shell is a sticky graphite header above a centered content frame capped at 88rem. Main padding is fluid, and working surfaces favor grid rows, aligned facts, and dividers over independent floating cards. Fleet is table-first: a fleet summary line, a Spark search, and one sortable table with a row per Spark (state, unified memory, GPU, disk, running recipes), then a detail view for the selected Spark. Needs-attention items sit above the summary.

At 54rem the shell becomes a compact top bar and action groups stack. Below 54rem each Spark row of the fleet table becomes a stacked block (label beside value) instead of a table row, so labels are never shrunk past legibility. At 34rem primary actions become full-width and dense legends become two columns. Horizontal overflow is confined to explicit data regions; the document itself never scrolls sideways.

Spacing follows a compact operating rhythm: small control gaps, roughly three-quarter-rem fixture padding, one-rem section separation, and 1.25rem shell spacing. Touch targets remain at least 2.75rem (44px) where interaction is primary.

### Named Rules

**The Rows Before Cards Rule.** Use rows, rails, and dividers for repeated operational facts; introduce a card only when it represents a complete inspectable fixture.


## Elevation & Depth

The system is flat by default. Tonal layering, a one-pixel structural line, and an occasional strong top or left edge do most of the hierarchy work. Ambient shadows are reserved for complete fixtures, selected workcells, and modal overlays; they never substitute for grouping or state.

### Shadow Vocabulary

- **Fixture Ambient** (`0 1.4rem 4rem #0002, inset 0 1px #ffffff0b`): Dark Spark and recipe fixtures that need quiet separation from the shell.
- **Overlay Ambient** (`0 2rem 6rem #000b`): Dialogs and consequential control overlays only.

### Named Rules

**The Ambient Only Rule.** Keep shadows soft and material; structure comes from tonal planes, lines, and strong edges rather than hard offsets.

## Shapes

Controls use gently machined corners, repeated fixtures use medium curves, and complete boards use the broadest corners. Pills are reserved for compact status, counts, and state filters. Tables and their cells stay square inside their enclosing fixture so the grid reads as one instrument rather than a pile of cards.

### Named Rules

**The Fixture Corners Rule.** Apply curvature to the outside silhouette; keep internal rows and table cells square and separated by lines.

## Components

### Buttons

- **Shape:** Compact machined control corners with a 44px minimum interactive height.
- **Primary:** NVIDIA green with dark green ink and compact horizontal padding; one clear affirmative action per local decision area.
- **Hover / Focus:** Secondary controls lift tonally on hover; all variants use a high-contrast three-pixel focus outline with a three-pixel offset.
- **Secondary:** Transparent graphite control with a strong structural border; use for inspection, cancellation, and alternate paths.

### Chips

- **Style:** Status is a small shape and the word, never a filled pill: filled circle with a soft ring is ok (green), diamond is warning (amber), square is failure (red), triangle is information (blue), hollow circle is neutral or unknown. Tones are named `ok`, `warn`, `bad`, `info`, `neutral`.
- **State:** The word always repeats the meaning; the shape and colour never carry it alone.

### Cards / Containers

- **Corner Style:** Medium fixture corners for inspectable Spark and recipe units; broad panel corners for complete workspaces.
- **Background:** Graphite panels in the shell.
- **Shadow Strategy:** Flat at rest unless the component is a complete fixture or overlay.
- **Border:** One-pixel structural lines, with a state-colored strong edge when the whole fixture carries health.
- **Internal Padding:** Three-quarter-rem to one-rem density, increasing only for complete workcells.

### Inputs / Fields

- **Style:** Recessed graphite background, strong one-pixel border, compact control corner, and full-width treatment inside search/filter rails.
- **Focus:** High-contrast three-pixel focus outline with offset; selection and caret use green without removing the visible structural border.
- **Error / Disabled:** Error copy and border use proven-failure red; disabled controls remain readable and visibly muted, never merely hidden.

### Navigation

The header holds the brand mark, Fleet, Library, and Activity as primary destinations, and the administrator menu. Links are compact; the active location uses a tonal field, while hover changes the graphite plane. On narrow screens the header stays one compact row with the icons hidden.

### Named Rules

**The State Before Control Rule.** Show health, current workload, capacity, and live-versus-desired evidence before presenting the action that changes them.

## Do's and Don'ts

### Do:

- **Do** keep operational text on solid graphite surfaces with WCAG 2.2 AA contrast.
- **Do** pair every color-coded state with explicit words and, where useful, a dot, border, or edge.
- **Do** preserve exact alignment and tabular numerals across counts, measurements, and table cells.
- **Do** confine horizontal scrolling to an explicit data region and provide a stacked mobile representation where a table would not fit.
- **Do** honor reduced motion and keep visible focus unobscured.

### Don't:

- **Don't** use green as ambient decoration, a generic brand wash, or an unsupported success claim.
- **Don't** put texture, metallic noise, or champagne fields behind operational text.
- **Don't** use shadow as the primary grouping device or scatter floating rounded cards across dense data.
- **Don't** compress a desktop table until labels or touch targets become illegible.
- **Don't** encode healthy, warning, blocked, installed, or running state through color alone.
- **Don't** introduce eyebrow or kicker labels above headings, or glyph-style icons in place of the established icon system; use direct headings and recognizable visual controls.
