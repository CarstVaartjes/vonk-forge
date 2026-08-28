---
name: Vonk Forge Control
description: A precision Spark workcell for inspecting live and desired local AI state.
colors:
  surface-base: "#090c09"
  surface-sidebar: "#0d120d"
  surface-panel: "#111912"
  surface-raised: "#19231a"
  surface-recessed: "#07110e"
  border: "#3b4336"
  border-strong: "#5f6854"
  text: "#f0f3eb"
  text-muted: "#abb5a7"
  text-subtle: "#8d9988"
  mint: "#7cc64b"
  mint-strong: "#a9ec7a"
  mint-ink: "#0b1907"
  titanium: "#c8b996"
  titanium-muted: "#92866d"
  focus: "#c3f89d"
  warning: "#f4c66b"
  danger: "#ff919d"
  info: "#94cfff"
  workcell-canvas: "#f4f1e8"
  workcell-ink: "#181a16"
  workcell-muted: "#5d6258"
  workcell-line: "#c9c4b6"
typography:
  display:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "clamp(1.55rem, 3vw, 2.2rem)"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "-0.04em"
  headline:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "1.15rem"
    fontWeight: 760
    lineHeight: 1.15
    letterSpacing: "-0.035em"
  title:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "1rem"
    fontWeight: 800
    lineHeight: 1.2
    letterSpacing: "normal"
  body:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "0.8rem"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "normal"
  label:
    fontFamily: "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif"
    fontSize: "0.7rem"
    fontWeight: 800
    lineHeight: 1.2
    letterSpacing: "0.08em"
rounded:
  control: "0.45rem"
  item: "0.65rem"
  fixture: "0.85rem"
  panel: "1rem"
  pill: "999px"
spacing:
  compact: "0.35rem"
  control: "0.65rem"
  fixture: "0.75rem"
  section: "1rem"
  shell: "1.25rem"
components:
  button-primary:
    backgroundColor: "{colors.mint}"
    textColor: "{colors.mint-ink}"
    rounded: "{rounded.control}"
    padding: "0.7rem 1rem"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "0.7rem 1rem"
  search-input:
    backgroundColor: "{colors.surface-recessed}"
    textColor: "{colors.text}"
    rounded: "{rounded.control}"
    padding: "0.7rem"
  nav-link-active:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.mint-strong}"
    rounded: "{rounded.item}"
    padding: "0.65rem 0.75rem"
  status-pill-healthy:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.mint-strong}"
    rounded: "{rounded.pill}"
    padding: "0.25rem 0.55rem"
  spark-workcell:
    backgroundColor: "{colors.surface-panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.panel}"
    padding: "1rem"
  profile-rail-item:
    backgroundColor: "{colors.surface-raised}"
    textColor: "{colors.text}"
    rounded: "0.35rem"
    padding: "0.7rem 0.65rem"
  workload-map-row:
    backgroundColor: "{colors.workcell-canvas}"
    textColor: "{colors.workcell-ink}"
    rounded: "0"
    padding: "0.68rem 0.75rem"
---

# Design System: Vonk Forge Control

## Overview

**Creative North Star: "The Spark Workcell"**

Vonk Forge feels like a local AI CNC-machine inspection board: a powder-coated graphite control shell with restrained champagne hardware cues surrounding a warm, light inspection canvas. The shell is calm and compact; the canvas makes live workload placement, desired profile state, and discrepancies read like a physical job sheet under task lighting.

Precision comes from rows, rails, thin dividers, tabular measurements, and exact state language rather than ornamental dashboard chrome. NVIDIA green is deliberately scarce and belongs to positive health, readiness, and safe primary action. Amber requests attention, red proves failure, and blue identifies installed or informational state; text and structure always repeat the meaning.

**Key Characteristics:**

- Graphite hardware shell with champagne separators and labels.
- Warm inspection canvas for live-versus-desired workload truth.
- Dense, aligned operating rows with generous local whitespace.
- Green reserved for positive health, readiness, and safe action.
- Flat fixtures first; ambient depth only where hierarchy requires it.

## Colors

The palette separates the graphite operating shell from the warm inspection sheet, then uses a small state vocabulary with text and shape reinforcement.

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
- **Sidebar Graphite:** The distinct navigation rail.
- **Panel Graphite:** The default dark fixture and form surface.
- **Raised Graphite:** Selected or hovered structural layers that remain subordinate to state color.
- **Recessed Graphite:** Search wells, metric cells, and inset evidence areas.
- **Structural Line / Strong Structural Line:** Thin separators and control outlines that carry most grouping.
- **Primary Text / Muted Text / Subtle Text:** The three-level graphite-surface text hierarchy.
- **Inspection Canvas:** The warm paper-like field for desired and live workload comparison.
- **Inspection Ink / Inspection Muted / Inspection Line:** The corresponding dark text and divider system for the light field.

### Named Rules

**The Green Means Go Rule.** Reserve green for positive health, readiness, and safe primary action; never use it as general decoration or as the only state signal.

**The Two Material Zones Rule.** Dense control chrome stays graphite; desired-versus-live workload truth moves onto the warm inspection canvas.

## Typography

**Display Font:** UI Sans (with system-ui and platform sans-serif fallbacks)
**Body Font:** UI Sans (with system-ui and platform sans-serif fallbacks)
**Label/Mono Font:** UI Monospace (with SFMono-Regular, Consolas, and monospace fallbacks) for identifiers and exact technical evidence only.

**Character:** The system uses one hard-working sans family, tight headings, compact body text, and tabular numerals. Hierarchy comes from weight, alignment, and density rather than a decorative display face.

### Hierarchy

- **Display** (700, fluid compact display, tight line-height): Library-level titles and the rare page title that must orient a whole workspace.
- **Headline** (760, compact, tight tracking): Command titles, workcell headings, and primary section names.
- **Title** (800, compact): Spark names, workload names, and decisive fixture labels.
- **Body** (400, compact, 1.45 line-height): Operational explanations, helper text, and result context; keep paragraphs narrow enough to scan inside their fixture.
- **Label** (800, widely tracked, uppercase): Measurement headings, field labels, table axes, and small control categories.

### Named Rules

**The Measure Before Decorate Rule.** Use tabular numerals for counts, capacity, percentages, versions, and timestamps; use monospace only for identifiers, digests, and commands.

## Layout

The desktop shell is a fixed graphite rail beside a centered content frame capped at 88rem. Main padding is fluid, and working surfaces favor grid rows, aligned facts, and dividers over independent floating cards. Fleet's signature board is a three-part fixture: profile rail, workload map, and plan/exception rail, with the selected profile and fleet facts spanning its top edge.

At 54rem the shell becomes a top bar, the board becomes one column, the profile rail scrolls horizontally, and action groups stack. At 42rem the workload matrix is replaced by workload-first stacked rows rather than shrinking labels past legibility. At 34rem primary actions become full-width and dense legends become two columns. Horizontal overflow is confined to explicit data regions; the document itself never scrolls sideways.

Spacing follows a compact operating rhythm: small control gaps, roughly three-quarter-rem fixture padding, one-rem section separation, and 1.25rem shell spacing. Touch targets remain at least 2.75rem (44px) where interaction is primary.

### Named Rules

**The Rows Before Cards Rule.** Use rows, rails, and dividers for repeated operational facts; introduce a card only when it represents a complete inspectable fixture.

**The Workload-First Mobile Rule.** On phones, preserve each workload and reveal its Spark states beneath it; do not miniaturize the desktop matrix.

## Elevation & Depth

The system is flat by default. Tonal layering, a one-pixel structural line, and an occasional strong top or left edge do most of the hierarchy work. Ambient shadows are reserved for complete fixtures, selected workcells, and modal overlays; they never substitute for grouping or state.

### Shadow Vocabulary

- **Fixture Ambient** (`0 1.4rem 4rem #0002, inset 0 1px #ffffff0b`): Dark Spark and recipe fixtures that need quiet separation from the shell.
- **Inspection Board Ambient** (`0 1.3rem 3.4rem #0005`): The complete warm operating board as one lifted instrument.
- **Overlay Ambient** (`0 2rem 6rem #000b`): Dialogs and consequential control overlays only.

### Named Rules

**The Ambient Only Rule.** Keep shadows soft and material; structure comes from tonal planes, lines, and strong edges rather than hard offsets.

## Shapes

Controls use gently machined corners, repeated fixtures use medium curves, and complete boards use the broadest corners. Pills are reserved for compact status, counts, and state filters. Tables and workload cells stay square inside their enclosing fixture so the grid reads as one instrument rather than a pile of cards.

### Named Rules

**The Fixture Corners Rule.** Apply curvature to the outside silhouette; keep internal rows and matrix cells square and separated by lines.

## Components

### Buttons

- **Shape:** Compact machined control corners with a 44px minimum interactive height.
- **Primary:** NVIDIA green with dark green ink and compact horizontal padding; one clear affirmative action per local decision area.
- **Hover / Focus:** Secondary controls lift tonally on hover; all variants use a high-contrast three-pixel focus outline with a three-pixel offset.
- **Secondary:** Transparent graphite control with a strong structural border; use for inspection, cancellation, and alternate paths.

### Chips

- **Style:** Compact pill with tinted background, matching border, bold state text, and a textual label.
- **State:** Healthy is green, attention is amber, proven failure is red, and informational/installed is blue. Never rely on the tint alone.

### Cards / Containers

- **Corner Style:** Medium fixture corners for inspectable Spark and recipe units; broad panel corners for complete workspaces.
- **Background:** Graphite panels in the shell; warm canvas only for workload/profile inspection.
- **Shadow Strategy:** Flat at rest unless the component is a complete fixture or overlay.
- **Border:** One-pixel structural lines, with a state-colored strong edge when the whole fixture carries health.
- **Internal Padding:** Three-quarter-rem to one-rem density, increasing only for complete workcells.

### Inputs / Fields

- **Style:** Recessed graphite background, strong one-pixel border, compact control corner, and full-width treatment inside search/filter rails.
- **Focus:** High-contrast three-pixel focus outline with offset; selection and caret use green without removing the visible structural border.
- **Error / Disabled:** Error copy and border use proven-failure red; disabled controls remain readable and visibly muted, never merely hidden.

### Navigation

The desktop rail contains only Fleet and Library as primary destinations. Links are compact rows; the active location uses a tonal field and a strong inset rail, while hover changes the graphite plane. On narrow screens navigation becomes a top-bar disclosure with focus containment and a scrim.

### Workload Map

The signature component is a warm inspection fixture that aligns model/recipe rows against Spark columns and a profile rail. Every cell states Running, Installed, Profile change, Attention, or empty in words, reinforces that state with color and a dot, and exposes horizontal scrolling only within the matrix. Below 42rem the same truth becomes workload-first stacked rows.

### Named Rules

**The State Before Control Rule.** Show health, current workload, capacity, and live-versus-desired evidence before presenting the action that changes them.

## Do's and Don'ts

### Do:

- **Do** keep operational text on solid graphite or warm inspection surfaces with WCAG 2.2 AA contrast.
- **Do** pair every color-coded state with explicit words and, where useful, a dot, border, or edge.
- **Do** use warm inspection surfaces for desired-versus-live comparisons and graphite for control chrome.
- **Do** preserve exact alignment and tabular numerals across counts, measurements, and workload cells.
- **Do** confine horizontal scrolling to an explicit data region and provide a stacked mobile representation.
- **Do** honor reduced motion and keep visible focus unobscured.

### Don't:

- **Don't** use green as ambient decoration, a generic brand wash, or an unsupported success claim.
- **Don't** put texture, metallic noise, or champagne fields behind operational text.
- **Don't** use shadow as the primary grouping device or scatter floating rounded cards across dense data.
- **Don't** compress a desktop matrix until labels or touch targets become illegible.
- **Don't** encode healthy, warning, blocked, installed, or running state through color alone.
- **Don't** introduce eyebrow or kicker labels above headings, or glyph-style icons in place of the established icon system; use direct headings and recognizable visual controls.
