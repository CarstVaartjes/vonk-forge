# vonkctl `--json` client contract

`vonkctl --json` is read by tools other than the CLI: the `@vonkforge/pi`
extension, the recipe sweep and any third-party client. The documents listed
below are a versioned contract. Each is defined by one Pydantic model, has a
published JSON Schema in [`schemas/vonkctl/`](../schemas/vonkctl), and is proven
against the real Controller in CI.

Every definition lives once in
[`definitions.schema.json`](../schemas/vonkctl/definitions.schema.json). A
kind's file holds only its root and refers into it with
`"$ref": "definitions.schema.json#/$defs/X"`, so a client loads the kind file
together with the shared file (a JSON Schema registry keyed by file name, for
example `referencing.Registry` with `jsonschema`). The shared file is not
versioned on its own: it is the union of the definitions the kind files use, and
a change to one is judged through the kinds that reference it. Moving the
definitions out of the kind files changed no document, so the kind versions
stayed (the rules below bump a version only for a changed document).

## Version field

Every document in the table carries a top-level integer `schema_version`. The
number belongs to the document kind, not to the vonkctl release.

| Kind | Printed by | Defined by | Version | Schema |
| --- | --- | --- | --- | --- |
| `connection` | `--check-connection` | `ConnectionDocument` | 1 | `connection-v1.schema.json` |
| `fleet` | `fleet` | `FleetSnapshot` | 1 | `fleet-v1.schema.json` |
| `profile-list` | `profile list` | `FleetProfileList` | 1 | `profile-list-v1.schema.json` |
| `profile` | `profile --profile N` | `FleetProfileReadView` | 1 | `profile-v1.schema.json` |
| `profile-review` | `profile load --review`, and `profile load` when nothing changes | `FleetProfilePreviewResponse` | 3 | `profile-review-v3.schema.json` |
| `profile-application` | `profile load`, `progress`, `cancel` | `FleetProfileApplicationView` | 1 | `profile-application-v1.schema.json` |
| `profile-endpoints` | `profile endpoint` | `FleetProfileEndpointsView` | 1 | `profile-endpoints-v1.schema.json` |
| `recipe-library` | `recipe`, `recipe library` | `RecipeLibraryResponse` | 4 | `recipe-library-v4.schema.json` |
| `recipe-detail` | `recipe detail` | `RecipeDetailResponse` | 4 | `recipe-detail-v4.schema.json` |
| `artifact-job` | `job create`, `detail`, `submit`, `cancel`, `run --no-wait` (and `run` when the job did not succeed) | `ArtifactJobResponse` | 1 | `artifact-job-v1.schema.json` |
| `artifact-download` | `job download`, `job run` (finished) | `ArtifactDownloadDocument` | 1 | `artifact-download-v1.schema.json` |
| `observation` | any followed command that timed out or was interrupted | `ObservationDocument` | 3 | `observation-v3.schema.json` |
| `error` | any failure | `ErrorDocument` | 3 | `error-v3.schema.json` |

`schema_version` is required in every published schema. A document the
Controller already answers is that Controller response plus `schema_version`;
the CLI and the web client read one shape. The review document versions itself:
its `schema_version` is owned by `FleetProfilePreview` alone, and the table in
`cli_json_contract.py` holds no second copy. Other
documents carry no `schema_version` and no promise. Source of truth for the
table: `DOCUMENT_VERSIONS` in `src/cluster_profiles/cli_json_contract.py`.

A followed command (`--follow`, `--watch`, a deadline) that cannot finish prints
an `observation` document instead of the kind above. It is a tagged union:
`result_kind` names the kind of the command's own document, `result` holds the
last confirmed document of that kind (stamped, or `{}` when nothing was
confirmed) and `observation` says where to reconnect. An ended followed command
that fails prints the same `result_kind` / `result` / `observation` inside the
`error` document. A wrapper around a document with no published kind is itself
unpublished.

`error` documents type `error_type` (`arguments`, `control_api`, `declined`,
`local`, `update`), `code` (the Controller code grammar), `detail` and
`recovery_actions`, the error context, and an unconfirmed `submission` with its
`reconcile` command. Enrollment-delivery receipts are not part of the contract.

One cause, one code. A failure the CLI concludes itself carries `error_type`
`local` and never `control.api_error`: `local.io_failed` (a local file or
directory cannot be read or written), `cli.invalid_input` (a value or local
input is not valid), `job.not_finished` (the job has no result yet; the
follow command is in `recovery_actions`) and `job.ended_without_result`.

A list that has more pages carries a non-null `next_cursor`; a null one means
the page is the last.

## Compatibility rules

- Adding a field, or a value to an open string, keeps the version. Clients
  must ignore fields they do not know; the published schemas allow them. A
  capability is such an open string (`CapabilityName`, a string of 1 to 64 characters): a
  capability a newer recipe contract adds never changes these schemas, and a
  client shows a name it does not know as the name.
- A `$ref` that names another definition keeps the version when that
  definition is the old one plus compatible additions (the same check, applied
  to both definitions): a type may become a subtype with optional fields.
- Removing or renaming a field, changing its type, or making an optional field
  required bumps the version. The new schema file replaces the old one and the
  change lands in the same PR as every consumer in this repository.
- A client reads `schema_version` first. A version it does not know is unknown
  data: it degrades the feature that needs that document with a typed reason
  ("unsupported vonkctl document version") and re-checks on its next refresh;
  it does not guess at the fields. `--check-connection --json` `.client.version`
  identifies the CLI build.
- Regeneration refuses to rewrite a published file with an incompatible schema
  (checked with the shared definitions inlined; a removed property, a `required` list that grew or shrank, a changed type). A test
  fails when a committed file (shared definitions included) is not exactly the model's current export, so a
  stale or hand-edited file is caught. Telling an incompatible change from a
  compatible one needs the previously published file, which only that
  regeneration step reads; the version bump is a review decision.

## Absent and `null`

The repository rule applies ([API contracts](api-contracts.md)): an optional
nullable field with no value is omitted, and a required nullable field is an
explicit `null`. The schema tells them apart: a property in `required` is always
present, possibly `null`; any other property may be absent, which means the
same as `null`. Read optional fields as `doc.field ?? null`; do not branch on
the presence of a key. `false`, `0`, empty strings and empty lists are real
values and are always preserved.

## Changing a contract

Edit the model (or the table), then regenerate the schema files:

```sh
uv run --project control --frozen python tools/vonkctl_json_contract.py schemas/vonkctl
```

`control/tests/test_vonkctl_json_contract.py` fails when a file drifts from its
model, and runs each command against the real Controller API, checking its
output against both the published schema and the strict model.
`tests/cluster_profiles/test_cli_json_client_contract.py` does the same for the
documents that need no API (jobs, download, connection, error).

## Consumers in this repository

The recipe sweep (`vonk-forge-recipes`, `spark_sweep`) reads these documents
with null-tolerant lookups and never branches on a key's presence in them. The
only change for it is the additional `schema_version` field.
