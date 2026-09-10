# Models and recipes

The [global recipe repository](https://github.com/CarstVaartjes/vonk-forge-recipes)
owns two authored contracts: **Model** and **Recipe**. Their Pydantic definitions
and published schemas govern every consumer. The Controller follows its latest
`main` and records the exact commit and document digests it consumes.

## Model: what the AI can do

A Model describes one exact set of model files: its publisher, family, version,
capabilities, format, quantization, source revision, file sizes and checksums.
It also records access and license information. Families and versions are data
inside this structure; adding a family or model does not require a new Python
class or database schema.

Making a Model available downloads its selected files to the trusted
Controller/NAS cache and verifies them. Other Recipes can reuse those same
cached files. A cached Model is not yet a running workload, and a Spark-local
copy does not make the Model available for profile authoring.

## Recipe: how the model runs

A Recipe selects exact Model definitions and adds the engine, runtime arguments,
settings, container image or build instructions, required writable paths,
resources, Spark topology and serving checks. One model can have several Recipes
from the same or different creators, with different engines or Spark counts.

Preparing a Recipe is an explicit operation that makes its exact Model files
and recipe image available and verified in the Controller/NAS cache. Profile
choices may reference only complete cached assets. Run or apply first checks
that cache gate; missing assets block the operation and offer **Prepare cache**.
Once the gate succeeds, the Controller distributes the exact assets to the
selected Sparks in parallel, skips verified local copies, safely replaces
conflicting workloads, and reports per-Spark progress and readiness. Run and
apply do not silently prepare missing assets or fetch from upstream. See the
[availability and recovery design](../library-availability-design-2026-09-06.md)
for progress and update behavior.

## Local state and updates

PostgreSQL records the consumed definitions, Controller/NAS cache receipts,
preparation, placement, run and operation state. These records support
execution and history; they do not create a second editable recipe catalog.
Spark-local copies are derived execution caches and do not authorize profiles
or pin NAS objects. There is no manual import or local recipe-authoring
workflow.

A changed global definition has a new digest. An explicit cache preparation can
prepare that revision while a running workload keeps its original Model, Recipe
and image receipts. Only an explicit Run or Switch changes the running
configuration. Model files and images stay reusable until an explicit
cache-cleanup operation removes them; NAS garbage collection removes only
unreferenced local model objects.

For repository layout and validation commands, see the
[recipe library guide](recipe-library.md).
