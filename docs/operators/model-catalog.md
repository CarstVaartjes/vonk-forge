# Model catalog

The local catalog answers two different questions: what model material exists,
and how an exact revision can run on this fleet. Resolved catalog documents are
immutable and content-addressed. PostgreSQL is authoritative for local catalog,
installation, placement, and run state; the optional global catalog can supply
documents but does not own local workloads, images, or model files.

## Four catalog layers

A **model group** is the human product family, such as DeepSeek. A **model** is
one member of that family with a stable upstream identity, such as DeepSeek V4
Flash. A **model version** fixes publisher, upstream revision, format,
quantization, artifact inventory, checksums, size, lineage, access, and license.
A **recipe** selects one model version and adds an execution harness, runtime
distribution, optional patch bundle, build input, parameters, topology,
resources, interfaces, and validation ladder.

The layers are deliberately separate. Two recipes can use the same model
version with different runtimes or topology, and a new model artifact revision
does not silently rewrite an accepted recipe.

The broader discovery list is maintained separately in the
[model target ledger](model-targets.md). It distinguishes accepted recipes from
candidate and blocked upstreams, so the Library can remain trustworthy while
the project continues to add defaults.

The reviewed public recipe source is the separate [standard recipe
library](recipe-library.md). The local catalog imports an exact library
snapshot, then remains authoritative for installation, placement, runs, and
fleet evidence.

## Exact immutable identity

Every direct reference is the tuple `kind`, `publisher`, `slug`, and
`content_sha256`. A slug is a readable lookup key, not sufficient execution
authority. Resolution verifies that tuple against the canonical checked-in
document. A resolved revision is never edited in place; a change creates a new
revision and digest.

Installation evidence binds the exact recipe revision, source-bundle digest,
build-input digest, OCI image digest, artifact-set digest, placement generation,
and certificate-bound `spk_…` node identities. Mutable image tags, display
names, host IP addresses, and “latest” model revisions cannot substitute for
those identities.

Input-dependent jobs may declare a non-OpenAI interface input contract with
accepted media types and a byte limit. The harness projects each request's
content-addressed input staging directory as a read-only, isolated `/inputs`
mount alongside `/models` and `/outputs`. Operators provide input artifacts
through the job boundary; recipes never receive arbitrary host paths or
runtime URLs. Such a recipe must also declare the matching `inputs` security
mount.

## One Spark, many Sparks, and replicas

A one-Spark recipe has topology mode `single`: one node owns the model endpoint
and all role resources. A many-Spark distributed recipe is one gang: its exact
rank count, rank roles, world size, parallelism, common artifact set, direct
fabric, start order, and stop order are admitted together. One missing or stale
rank withdraws the gang's published endpoint.

Replicas are different from distributed execution. Replicas are independent
complete runs, each capable of serving a request and each with its own
placement and health. Distributed ranks cooperate on one request and expose
only the recipe's endpoint-owner rank. Adding two Sparks therefore does not by
itself mean either “two replicas” or “one two-rank model”; the selected recipe
decides.

## Custom recipes and license responsibility

An operator may author a custom recipe against the same v1 contracts. Keep
secrets out of every catalog document, pin all upstream revisions and digests,
declare full resource and topology requirements, use one of the built-in
harnesses where it implements the lifecycle, and run structural plus physical
qualification before treating the recipe as accepted. A recipe-local patch
bundle must identify its target and exact source; it is not an untracked edit to
an installed container.

Vonk Forge records upstream license metadata and whether explicit operator
acceptance is required. That metadata is evidence, not legal advice or a grant
of rights. The operator remains responsible for model, dataset, runtime,
dependency, redistribution, export, and acceptable-use terms before download,
installation, publication, or sharing.

## Catalog operations

Use the Library and plan APIs for normal work. First resolve exact catalog and
recipe revisions. Then preview placement, build, distribution, installation,
and run actions against current Fleet inventory. Apply only the digest returned
by the matching preview. The controller records operations and node evidence;
operators do not create parallel installation state in files or by starting a
container manually.

## Install and invoke

Installation is the ordered public lifecycle: verify source, build an exact OCI
image, distribute that digest to every mapped node, verify artifact digests,
install the exact recipe revision, and start a run. Route publication follows
fresh rank and endpoint evidence; it is not part of image build or download.

For an `openai` interface, clients invoke the recipe's published alias through
Caddy and LiteLLM. The controller, not the client or LiteLLM discovery, selects
the accepted entrypoint. Artifact-producing jobs use their declared direct job
or result API and are not registered as OpenAI model aliases.

## Stop and uninstall

Stop preview identifies the exact active run and returns a plan digest. Applying
that digest withdraws the route and stops the ranks. Uninstall is a separate
preview/apply operation against the exact installation and is allowed only
after its run is stopped. Uninstall removes runtime installation state but
retains immutable catalog history. Deleting model or build caches is a separate
bounded operation; never infer it from “stop.”

## Update and exact-revision rollback

An update authors or imports a new immutable entity or recipe revision, resolves
it, previews fresh placement and resources, then installs and canaries that
exact revision. Existing runs stay bound to their prior revision until an
explicit stop and replacement. A failed canary does not mutate the older
revision.

Rollback means selecting the previously accepted recipe revision and exact
image/artifact digests, then running the same preview, install, start, and
evidence gates. It is not an Alembic downgrade, a database restore over newer
state, a mutable-tag pull, or an edit of a resolved row. Cache reuse is allowed
only when the retained object independently verifies to the exact required
digest.
