# Controller model and recipe cache

The Controller downloads and verifies immutable model files into its cache.
Sparks receive authorized model and image bytes over the LAN and keep their own
copies. The Controller cache is not a serving-time dependency after installation.

## Browse, download, and refresh

Use the exact selector shown by the list:

```bash
vonkctl model
vonkctl model library
vonkctl model download MODEL
vonkctl recipe library
vonkctl recipe download RECIPE
vonkctl recipe update --all
```

A model can be downloaded without a recipe or an online Spark. Recipe download
also prepares its image and downloads a missing model. Repeating an active
download follows it; repeating a failed transfer resumes it; repeating a
completed download refreshes it. No separate repair command or force flag is
required. A failed refresh must preserve the last verified copy.

Only fully verified objects are published. Partial transfers remain outside
the published namespace. Admission uses actual filesystem free space, reserve,
and temporary transfer/build requirements—not just logical model size.
Unknown sizes are reported as unknown.

## Remove and cancel

```bash
vonkctl model remove MODEL
vonkctl recipe remove RECIPE
vonkctl recipe remove RECIPE --with-model
```

Removal cancels the corresponding download or build and removes the Controller
cache entry. It does not stop a Spark, uninstall its local copy, or delete saved
profile assignments. A late worker completion must not republish a removed entry.

When removing the last cached recipe for a model, the CLI asks whether to remove
the model too. Use `--keep-model` or `--with-model` to make that choice explicit.
Shared physical objects remain while another cache entry still requires them.

Profiles show missing Controller cache alongside observed Spark running state.
Loading a profile resolves the latest verified cached compatible recipe/model;
updating the cache alone does not change running workloads.

## Credentials and evidence

See [Hugging Face authentication](../model-cache-huggingface-auth.md) for gated
models. Upstream credentials stay on the Controller and are not sent to Sparks
or unrelated redirect authorities. After correcting access, repeat download.

Progress reports measured transfer/build state. Successful caching and LAN
distribution prove preparation; runtime quality and multi-Spark fabric
acceptance still require the designated hardware lane.

See [the CLI runbook](vonkctl.md) for output, selectors, profiles, and automation.
