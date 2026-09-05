# Compact overviews and paired Model/Recipe lists

The user's final direction is binding: remove the tiles and use compact lists
for Model, Installation and Recipe overviews. For choosing a model and recipe
to install/run, use two interacting lists next to each other. This supersedes
earlier card grids and the broader proposal for new collection-detail popups.
Do not create new overview pages or redesign unrelated Fleet, Profile or cache
views as part of this correction.

## Compact overview design

Keep the horizontal navigation, existing typeface, graphite palette and green
action accent. Render each overview as one continuous list with subtle row
dividers, a quiet hover treatment and a clear selected-row indicator. Remove
individual rounded/shadowed tiles, grid/tile switches and saved grid preferences.
There is one compact view.

Align comparable facts into columns on desktop. Start with the human-readable
name and one useful secondary line. Numbers align consistently and retain units.
Keep full hashes, long descriptions and configuration in existing details.
Use the established readable body size rather than shrinking text to add columns.

Aim for approximately 56–72 px rows while allowing wrapping and text enlargement.
These are density targets, not fixed heights that clip content. Make the row's
main action obvious and communicate status with words as well as color.

| Overview | Name and context | Comparable facts | Existing action |
| --- | --- | --- | --- |
| Models | Model name; family, version and variant | Main capabilities; format/quantization; size; applicable recipes | Select model/version or open details |
| Installations | Model and recipe; assigned Sparks | Current state; relevant version; progress when active | Open installation or operation details |
| Recipes | Recipe name; exact model/version | Engine; Spark count; memory/context where applicable; version | Select recipe; Controller Run where available |

These are priorities, not a demand to show unavailable facts. Keep public rows
limited to published data; private installations and operations belong in the
Controller. Preserve meaningful existing actions and state.

## Two interacting lists for model and recipe selection

Use one workspace with Models on the left and Recipes on the right. A roughly
40/60 width split gives recipes room for engine, topology and resource comparison.
Use aligned list headers and a subtle divider, not a grid of individual tiles.

The left list retains model search and relevant family/capability/version/variant
filters. Selecting a model filters the right list to its matching recipes. Keep
the selected model visible in the right header. Where the selected model groups
several versions, show each recipe's exact version and variant; an explicit
version selection filters by that exact identity, not only a name or family.

The right list presents the compatible recipe choices as compact rows. Selecting
a recipe exposes the existing Spark selection and Run controls. Keep the model
and recipe relationship visible while choosing Sparks. Selection is navigation,
not an operation: nothing downloads, switches or starts until the user invokes
the corresponding action.

Changing the model/version clears an incompatible recipe selection and prevents
a stale Run target. If no recipe matches, explain that state within the right
list and offer the existing filter recovery. Do not silently choose a different
model/version or recipe. Preserve valid filters and selection when returning
from details; browser Back and direct-link refresh must work.

On mobile, stack the paired lists and retain visible selected-model context
above the recipe list. Use compact stacked rows, show the most useful facts,
and move secondary columns into existing details. Keep touch targets at least
44 px. Avoid horizontal scrolling as the normal way to read an overview.

## Details and acceptance

Keep existing detail navigation. This correction does not require a new popup
or drawer workflow. Preserve search, filters, ordering, pagination and scroll
position when entering and leaving details. An inline action must perform its
own action without triggering row navigation. Use accessible links/buttons;
do not nest interactive elements or make every data cell a tab stop.

Verify populated desktop/mobile overviews and the paired selection journey:
select model, select exact version, compare matching recipes, select recipe,
choose Sparks and invoke the existing Run flow. Also check no matching recipes,
model changes that invalidate selection, keyboard use, independent row actions,
detail/Back navigation, direct links and text enlargement. Confirm no tile/grid
toggle or saved grid preference remains and no row text clips.

Update active browser tests and captures. Label fixture data honestly and verify
the published catalog and Controller API separately. Sol owns integration;
public-web and Controller UI owners implement their respective applications.
Backend, package, runtime and upstream-refresh work continues independently.
