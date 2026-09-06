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

Making a Model available downloads its selected files to the Controller/NAS and
verifies them. Other Recipes can reuse those same cached files. A cached Model
is not yet a running workload.

## Recipe: how the model runs

A Recipe selects exact Model definitions and adds the engine, runtime arguments,
settings, container image or build instructions, required writable paths,
resources, Spark topology and serving checks. One model can have several Recipes
from the same or different creators, with different engines or Spark counts.

Making a Recipe available prepares its Model files and runtime image on the
Controller/NAS. Run selects its Sparks, prepares anything missing, distributes
the verified files and starts the workload. See the
[availability and recovery design](../library-availability-design-2026-09-06.md)
for progress and update behavior.

## Local state and updates

PostgreSQL records the consumed definitions and local cache, preparation,
placement, run and operation state. These records support execution and history;
they do not create a second editable recipe catalog. There is no manual import
or local recipe-authoring workflow.

A changed global definition has a new digest. Refresh can prepare that revision
while a running workload keeps its original Model, Recipe and image receipts.
Only an explicit Run or Switch changes the running configuration. Model files
and images stay reusable until an explicit cache-cleanup operation removes them.

For repository layout and validation commands, see the
[recipe library guide](recipe-library.md).
