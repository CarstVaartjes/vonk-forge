# List interfaces and contextual details

The user's latest direction is binding: collection tiles make little sense.
Use lists, with one detail panel opened from a selected row. This supersedes
earlier card-grid designs and visual approvals for both the private Controller
and the public model/recipe catalog. It does not change the underlying Model,
Recipe, operation or cache contracts.

## Shared visual design

Keep the compact horizontal navigation, existing typeface, graphite palette and
green action accent. Render each collection as one continuous list with subtle
row dividers. Use a quiet hover treatment and a visible selected-row indicator.
Avoid a separate rounded, shadowed or gradient container around every item.
Remove tile/grid view switches and their persistence; maintain one clear view.

Use the recovered width for aligned, comparable facts. The first column gives
the human-readable name with one secondary line of useful context. Numbers
align consistently and keep their units visible. Keep full hashes, long source
descriptions and configuration out of the row; show these in details. Use the
established readable body size, not smaller text to fit additional columns.

Desktop rows should usually be about 64–80 px high, with 80–96 px available for
a Spark's running workload and health summary. Allow text wrapping where needed;
these are target densities, not fixed heights that clip text. Preserve existing
chart colors and communicate status with words as well as color.

## Row contents

| Collection | Lead information | Comparable facts | Immediate action |
| --- | --- | --- | --- |
| Fleet | Spark name; running model and recipe, or Idle | State and freshness; unified memory; GPU activity; temperature; relevant run progress | Open details; contextually Run or inspect the operation |
| Models | Model name; family and selected version/variant | Main capabilities; format/quantization; download size; applicable recipe count | Open versions and recipes; Controller may offer local caching |
| Recipes | Recipe name; exact model/version | Engine; Spark count; applicable memory/context information; version | Open configuration; Controller Run |
| NAS cache | Model or image name; artifact type | Stored/total bytes; transfer rate and progress when active; last error | Inspect, retry or cancel where supported |
| Profiles | Profile name; short placement summary | Sparks in scope; assigned models/recipes; explicit Idle members | Switch; edit in details |

These are priorities, not a requirement to show unavailable fields or fit every
fact into a narrow viewport. Keep public catalog rows limited to published
facts. Private cache, telemetry and operations belong in the Controller.

Fleet must immediately answer what is running and the Spark's state. Put full
hardware/inference history, process details, source provenance, diagnostics and
recovery in its detail panel. All supported metrics remain available there;
unknown measurements stay unknown and offline history has gaps. Do not invent
zero values or count shared CPU/GPU memory twice.

## One contextual detail panel

Selecting a row opens one detail panel; selecting another replaces its content.
Keep list search, filters, ordering, pagination and scroll position intact.
Opening a panel must not refetch or reset an otherwise valid collection.

On wide screens, use a side panel of roughly 440–560 px while retaining a useful
list beside it. On narrow screens, use a full-width detail sheet or detail page
with a clear return action. Preserve supported deep links and browser Back:
returning to the list restores its prior state. Direct detail links must work
after refresh without requiring a prior row click.

Lead details with name, version and the relevant action. Follow with concise
facts, then collapsible configuration, source/version notes or full metric
history. Charts use the available panel width. Model details show versions and
applicable recipes as rows as well. Avoid nested tile grids and nested popups.
Run and Switch retain the agreed simple operation and durable progress flow.

Provide an explicit keyboard-accessible name/link or details button per row.
If the row also responds to pointer clicks, its action buttons must perform only
their own action. Do not nest interactive elements or make keyboard users tab
through every data cell. Escape closes an open detail surface and restores focus
to its originating row. A nonmodal desktop panel permits continued list use;
a modal mobile sheet traps focus, makes its background inert and restores focus
on close. Both have an accessible heading and a clearly named close/return button.

## Responsive and acceptance requirements

Mobile uses compact stacked rows within the same continuous list. Keep name,
state and one or two key facts immediately visible; move lower-priority columns
to details. Maintain touch targets of at least 44 px. Avoid horizontal scrolling
as the normal way to read a Model or Spark row.

Verify populated desktop and mobile screens, not only empty/loading states:

- Fleet with two Sparks, a distributed run, an Idle or unavailable Spark, and
  meaningful metrics and operation progress using accurate response shapes.
- Model and recipe lists using the published catalog, with search/filter results
  and a selected version/recipe detail.
- Profile rows and a switch progress view; cache rows with transfer/retry states.
- Keyboard selection, independent row actions, Escape/focus return, browser Back,
  direct-link refresh, filter preservation and scroll restoration.
- No collection tile/grid toggle, no empty grid gaps, no clipped row text and no
  stale screenshots that still show removed custom authoring controls.

Label fixture-based visual evidence honestly. Final API integration and deployed
data verification remain separate from screenshots. Sol owns integration; the
public-web and Controller UI owners implement their respective applications.
