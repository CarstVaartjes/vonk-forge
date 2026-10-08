from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from importlib.resources import files
from threading import Barrier

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_agent_protocol import DesiredAssignmentState, ModelCacheBlockerCode
from vonk_control.fleet_profile_contract import (
    FleetProfileAssignmentInput,
    FleetProfileInput,
)
from vonk_control.fleet_profiles import FleetProfileConflict, FleetProfileService
from vonk_control.model_cache_contract import (
    CachedModelResolution,
    CachedRecipeResolution,
    CachedResourceEstimate,
    CacheResolution,
)
from vonk_control.models import (
    AgentNode,
    AgentNodeProfile,
    Base,
    CatalogDocument,
    CatalogDocumentRevision,
    FleetProfileApplication,
    User,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
NODE_1 = "spk_" + "1" * 32
NODE_2 = "spk_" + "2" * 32
MODEL_DOCUMENT_ID = "00000000-0000-4000-8000-000000000010"
MODEL_REVISION_ID = "00000000-0000-4000-8000-000000000011"
RECIPE_DOCUMENT_ID = "00000000-0000-4000-8000-000000000020"
RECIPE_REVISION_ID = "00000000-0000-4000-8000-000000000021"


def _sessions() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


RECIPE_OPTIONS = [
    {
        "name": "verification",
        "label": "Verification",
        "help": "How drafted tokens are verified.",
        "choices": [
            {
                "value": "standard",
                "label": "Standard",
                "help": "Verify every drafted token.",
                "default": True,
            },
            {
                "value": "adaptive",
                "label": "Adaptive",
                "help": "Verify a per-step prefix.",
                "env": {"VERIFY_MODE": "adaptive"},
            },
        ],
    }
]


def _seed(sessions: sessionmaker, *, options: bool = False) -> None:
    with sessions.begin() as session:
        session.add(User(subject="test", role="administrator"))
    model_document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    recipe_document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text(encoding="utf-8")
    )
    if options:
        recipe_document["options"] = RECIPE_OPTIONS
    model = ModelDefinition.model_validate(model_document)
    recipe = RecipeDefinition.model_validate(recipe_document)
    model_digest = document_sha256(model_document)
    assert recipe.models[0].model.content_sha256 == model_digest
    with sessions.begin() as session:
        session.add_all(
            [
                AgentNode(
                    node_id=node_id,
                    state="active",
                    protocol_version=2,
                    architecture="linux-arm64",
                    last_seen_at=NOW,
                )
                for node_id in (NODE_1, NODE_2)
            ]
        )
        session.add_all(
            [
                AgentNodeProfile(
                    node_id=NODE_1,
                    display_name="Spark One",
                    hostname="spark-one",
                ),
                AgentNodeProfile(
                    node_id=NODE_2,
                    display_name="Spark Two",
                    hostname="spark-two",
                ),
            ]
        )
        session.add_all(
            [
                CatalogDocument(
                    id=MODEL_DOCUMENT_ID,
                    kind="model",
                    publisher=model.identity.publisher,
                    slug=model.identity.slug,
                    title=model.identity.model.title,
                    created_by="test",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                CatalogDocument(
                    id=RECIPE_DOCUMENT_ID,
                    kind="recipe",
                    publisher=recipe.identity.publisher,
                    slug=recipe.identity.slug,
                    title=recipe.metadata.title,
                    created_by="test",
                    created_at=NOW,
                    updated_at=NOW,
                ),
            ]
        )
        session.add_all(
            [
                CatalogDocumentRevision(
                    id=MODEL_REVISION_ID,
                    document_id=MODEL_DOCUMENT_ID,
                    kind="model",
                    publisher=model.identity.publisher,
                    slug=model.identity.slug,
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=model_document,
                    content_digest=model_digest,
                    artifact_key="a" * 64,
                    created_by="test",
                    created_at=NOW,
                ),
                CatalogDocumentRevision(
                    id=RECIPE_REVISION_ID,
                    document_id=RECIPE_DOCUMENT_ID,
                    kind="recipe",
                    publisher=recipe.identity.publisher,
                    slug=recipe.identity.slug,
                    revision_number=1,
                    schema_version=2,
                    state="active",
                    document=recipe_document,
                    content_digest=document_sha256(recipe_document),
                    execution_key="b" * 64,
                    created_by="test",
                    created_at=NOW,
                ),
            ]
        )


def test_profile_uses_canonical_recipe_and_model_revisions() -> None:
    sessions = _sessions()
    _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Canonical idle",
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-build",
                        "spark_ids": [NODE_1],
                        "desired_state": "running",
                        "assignment_name": "canonical",
                    }
                ],
            }
        ),
        actor="test",
    )

    assert profile.number == 1
    assert profile.revision == 1
    assert profile.assignments[0].recipe_id == RECIPE_DOCUMENT_ID
    assert profile.assignments[0].recipe_selector == "vonk-forge/synthetic-tiny-build"
    assert profile.assignments[0].spark_ids == [NODE_1]
    preview = service.preview(profile.id)
    assert preview.scope.node_ids == [NODE_1, NODE_2]
    assert preview.scope.idle_node_ids == [NODE_2]
    assert preview.assignments[0].recipe_revision_id == RECIPE_REVISION_ID


def test_definition_preserves_authoring_fields_without_consulting_cache() -> None:
    sessions = _sessions()
    _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    value = FleetProfileInput(
        name="Installed draft",
        description="keep",
        favorite=True,
        labels={"use": "images"},
        installation_policy="exact",
        assignments=[
            FleetProfileAssignmentInput(
                recipe_selector="vonk-forge/synthetic-tiny-build",
                spark_ids=[NODE_1],
                assignment_name="draft",
                model_variant="precise-variant",
                desired_state=DesiredAssignmentState.INSTALLED,
            )
        ],
    )
    created = service.create(value, actor="test")

    def unavailable(*args, **kwargs):
        raise AssertionError("definition read consulted cache availability")

    service._cache_resolver = unavailable
    read = service.definition_number(created.number)
    assert read.definition is not None
    assert read.definition.model_dump() == value.model_dump(
        exclude={"expected_revision"}
    )
    assert read.revision == created.revision


@pytest.mark.parametrize("existing", [False, True])
def test_competing_profile_saves_accept_only_one_observed_revision(
    postgres_engine, existing
):
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    if existing:
        service.update_number(2, FleetProfileInput(name="Original"), actor="test")
    observed = service.definition_number(2)
    ready = Barrier(2)

    def save(name):
        value = FleetProfileInput(name=name, expected_revision=observed.revision)
        ready.wait(timeout=10)
        try:
            return service.update_number(2, value, actor="test")
        except FleetProfileConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ("First", "Second")))
    accepted = [result for result in results if result is not None]
    assert len(accepted) == 1
    saved = service.definition_number(2)
    assert saved.revision == observed.revision + 1
    assert saved.definition == accepted[0].definition


@pytest.mark.usefixtures("damaged_json_rows")
def test_definition_read_preserves_a_malformed_saved_choice_as_unknown():
    from vonk_control.models import FleetProfile

    sessions = _sessions()
    service = FleetProfileService(sessions, clock=lambda: NOW)
    created = service.create(FleetProfileInput(name="Damaged"), actor="test")
    with sessions.begin() as session:
        row = session.get(FleetProfile, created.id)
        assert row is not None
        row.assignments = [{"recipe_selector": "vonk-forge/missing-fields"}]
    # Observability retains the identity without inventing an empty intent.
    observed = service.definition_number(created.number)
    assert observed.id == created.id and observed.revision == created.revision
    assert observed.definition is None and observed.projection_issue is not None
    with sessions() as session:
        row = session.get(FleetProfile, created.id)
        assert row is not None
        assert row.assignments == [{"recipe_selector": "vonk-forge/missing-fields"}]


@pytest.mark.usefixtures("damaged_json_rows")
@pytest.mark.parametrize(
    "field,value", [("labels", []), ("assignments", [{"invalid": "choice"}])]
)
def test_saved_profile_identity_never_embeds_a_damaged_column(field, value):
    from vonk_control.fleet_profiles import _profile_document
    from vonk_control.models import FleetProfile

    sessions = _sessions()
    service = FleetProfileService(sessions, clock=lambda: NOW)
    created = service.create(FleetProfileInput(name="Damaged"), actor="test")
    with sessions() as session:
        row = session.get(FleetProfile, created.id)
        assert row is not None
        original = _profile_document(row)
    with sessions.begin() as session:
        row = session.get(FleetProfile, created.id)
        assert row is not None
        setattr(row, field, value)
    with sessions() as session:
        row = session.get(FleetProfile, created.id)
        assert row is not None
        # A marker substituted as labels would silently acquire a content
        # identity. No bound plan may mistake that for saved authoring intent.
        with pytest.raises(ValidationError):
            _profile_document(row)
    with sessions.begin() as session:
        row = session.get(FleetProfile, created.id)
        assert row is not None
        setattr(row, field, getattr(original, field))
    with sessions() as session:
        row = session.get(FleetProfile, created.id)
        assert row is not None
        assert _profile_document(row) == original


def test_profile_accepts_the_library_publisher_slug_selector() -> None:
    sessions = _sessions()
    _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile = service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Library selector",
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-build",
                        "spark_ids": [NODE_1],
                        "desired_state": "running",
                        "assignment_name": "library-selector",
                    }
                ],
            }
        ),
        actor="test",
    )

    assert profile.assignments[0].recipe_selector == "vonk-forge/synthetic-tiny-build"
    assert profile.assignments[0].recipe_id == RECIPE_DOCUMENT_ID


def test_profile_contract_rejects_a_bare_recipe_slug() -> None:
    with pytest.raises(ValidationError, match="recipe_selector"):
        FleetProfileInput.model_validate(
            {
                "name": "Bare slug",
                "assignments": [
                    {
                        "recipe_selector": "synthetic-tiny-build",
                        "spark_ids": [NODE_1],
                    }
                ],
            }
        )


def test_all_idle_canonical_profile_previews_without_assignments() -> None:
    sessions = _sessions()
    _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile = service.create(
        FleetProfileInput(
            name="All idle",
            assignments=[],
        ),
        actor="test",
    )
    preview = service.preview(profile.id)
    assert preview.allowed
    assert preview.assignments == []
    assert preview.preparations == []
    assert preview.scope.idle_node_ids == [NODE_1, NODE_2]


def test_profile_authoring_accepts_incomplete_group_without_revision_or_scope() -> None:
    value = FleetProfileInput.model_validate(
        {
            "name": "Draft",
            "assignments": [
                {
                    "recipe_selector": "vonk-forge/vision-dual",
                    "spark_ids": [NODE_1],
                    "model_variant": "fp8",
                }
            ],
        }
    )
    document = value.model_dump(mode="json")
    assert document["assignments"] == [
        {
            "recipe_selector": "vonk-forge/vision-dual",
            "spark_ids": [NODE_1],
            "assignment_name": None,
            "model_variant": "fp8",
            "desired_state": "running",
            "option_choices": {},
        }
    ]
    assert "scope" not in document
    assert "recipe_revision_id" not in document["assignments"][0]


def test_numbered_autosave_uses_revision_and_load_freezes_whole_roster() -> None:
    sessions = _sessions()
    _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    created = service.update_number(
        2,
        FleetProfileInput(name="Second", assignments=[]),
        actor="test",
    )
    assert created.number == 2
    assert [item.model_dump() for item in created.fleet] == [
        {"selector": NODE_1, "display_name": "Spark One", "state": "Idle"},
        {"selector": NODE_2, "display_name": "Spark Two", "state": "Idle"},
    ]

    changed = service.update_number(
        2,
        FleetProfileInput(name="Second", expected_revision=1, assignments=[]),
        actor="test",
    )
    assert changed.revision == 2
    with pytest.raises(Exception, match="revision conflict"):
        service.update_number(
            2,
            FleetProfileInput(name="Stale", expected_revision=1, assignments=[]),
            actor="other",
        )

    application = service.load(
        2,
        actor="test",
        request_key="00000000-0000-4000-8000-000000000099",
    )
    assert application.state == "succeeded"
    assert application.progress.intended_profile is not None
    assert application.progress.intended_profile.scope.node_ids == [NODE_1, NODE_2]


def _resolution(
    revision_id: object,
    *,
    recipe_cached: bool,
    blockers: tuple[str, ...] = (),
    per_spark_memory_bytes: int | None = None,
    additional_disk_bytes: int | None = None,
) -> CacheResolution:
    """A typed cache resolution for a recipe revision, as the resolver answers."""

    return CacheResolution(
        recipe=CachedRecipeResolution(
            recipe_revision_id=str(revision_id),
            document_id=RECIPE_DOCUMENT_ID,
            publisher="vonk-forge",
            slug="synthetic-tiny-build",
            revision_number=1,
            content_sha256=None,
            cached=recipe_cached,
            cache_state="cached" if recipe_cached else "missing",
            artifact_set_sha256=None,
            expected_bytes=None,
            verified_bytes=None,
            image_digest=None,
            update_available=False,
        ),
        model=CachedModelResolution(
            content_sha256="a" * 64,
            cached=True,
            cache_state="cached",
            artifact_set_sha256=None,
            expected_bytes=None,
            verified_bytes=None,
            variant="fp16",
        ),
        resources=CachedResourceEstimate(
            per_spark_memory_bytes=per_spark_memory_bytes,
            additional_disk_bytes=additional_disk_bytes,
        ),
        blockers=[ModelCacheBlockerCode(item) for item in blockers],
    )


def test_profile_read_uses_the_read_only_latest_cache_resolver() -> None:
    sessions = _sessions()
    _seed(sessions)
    newer_revision_id = "00000000-0000-4000-8000-000000000022"
    with sessions.begin() as session:
        current = session.get(CatalogDocumentRevision, RECIPE_REVISION_ID)
        assert current is not None
        session.add(
            CatalogDocumentRevision(
                id=newer_revision_id,
                document_id=RECIPE_DOCUMENT_ID,
                kind="recipe",
                publisher=current.publisher,
                slug=current.slug,
                revision_number=2,
                schema_version=2,
                state="active",
                document=current.document,
                content_digest="d" * 64,
                execution_key="c" * 64,
                created_by="test",
                created_at=datetime(2026, 9, 6, tzinfo=UTC),
            )
        )
    calls: list[dict[str, object]] = []

    def resolve(**kwargs: object) -> CacheResolution:
        calls.append(kwargs)
        return _resolution(
            kwargs["exact_revision_id"],
            recipe_cached=True,
            per_spark_memory_bytes=10,
            additional_disk_bytes=20,
        )

    from .test_fleet_profiles import _SwitchAdapter

    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        cache_resolver=resolve,
        switch_adapter=_SwitchAdapter(),
    )
    profile = service.create(
        FleetProfileInput(
            name="Cached",
            assignments=[
                FleetProfileAssignmentInput(
                    recipe_selector="vonk-forge/synthetic-tiny-build",
                    spark_ids=[NODE_1],
                    model_variant="fp16",
                )
            ],
        ),
        actor="test",
    )
    assert calls == [
        {
            "recipe_identity": RECIPE_DOCUMENT_ID,
            "model_variant": "fp16",
            "exact_revision_id": newer_revision_id,
        }
    ]
    assert profile.assignments[0].recipe.state == "Cached"
    assert profile.assignments[0].model.state == "Cached"
    preview = service.preview(profile.id)
    assert preview.assignments[0].recipe_revision_id == newer_revision_id
    application = service.load(
        profile.number,
        actor="test",
        request_key="00000000-0000-4000-8000-000000000097",
    )
    assert application.progress.intended_profile is not None
    assert (
        application.progress.intended_profile.assignments[0].recipe_revision_id
        == newer_revision_id
    )


def test_profile_read_ignores_a_resolver_that_substitutes_an_older_revision() -> None:
    # The reported defect: the cache resolver returned the newest *cached*
    # revision even when the profile's head was a newer uncached one, and the
    # profile silently bound the older bytes.  A resolver that still substitutes
    # is not believed: its evidence is dropped (the cache state is unknown) and
    # the profile stays on the selected head, never retargeted to the older bytes.
    sessions = _sessions()
    _seed(sessions)
    newer_revision_id = "00000000-0000-4000-8000-000000000022"
    with sessions.begin() as session:
        current = session.get(CatalogDocumentRevision, RECIPE_REVISION_ID)
        assert current is not None
        session.add(
            CatalogDocumentRevision(
                id=newer_revision_id,
                document_id=RECIPE_DOCUMENT_ID,
                kind="recipe",
                publisher=current.publisher,
                slug=current.slug,
                revision_number=2,
                schema_version=2,
                state="active",
                document=current.document,
                content_digest="d" * 64,
                execution_key="c" * 64,
                created_by="test",
                created_at=datetime(2026, 9, 6, tzinfo=UTC),
            )
        )

    def substitute(**_kwargs: object) -> CacheResolution:
        return _resolution(
            RECIPE_REVISION_ID,
            recipe_cached=True,
            per_spark_memory_bytes=10,
            additional_disk_bytes=20,
        )

    from .test_fleet_profiles import _SwitchAdapter

    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        cache_resolver=substitute,
        switch_adapter=_SwitchAdapter(),
    )
    created = service.create(
        FleetProfileInput(
            name="Substituted",
            assignments=[
                FleetProfileAssignmentInput(
                    recipe_selector="vonk-forge/synthetic-tiny-build",
                    spark_ids=[NODE_1],
                    model_variant="fp16",
                )
            ],
        ),
        actor="test",
    )
    # Bound to the selected head; the substituted cache claim is not shown as cached.
    assert [item.recipe_id for item in created.assignments] == [RECIPE_DOCUMENT_ID]
    assert all(item.recipe.state != "Cached" for item in created.assignments)


def test_profile_read_names_a_missing_exact_cache_instead_of_substituting() -> None:
    # The other half of the reported defect: when the selected head is not in
    # the local cache, the profile keeps that exact revision and names the
    # prepare-cache blocker rather than silently binding an older cached one.
    sessions = _sessions()
    _seed(sessions)
    newer_revision_id = "00000000-0000-4000-8000-000000000022"
    with sessions.begin() as session:
        current = session.get(CatalogDocumentRevision, RECIPE_REVISION_ID)
        assert current is not None
        session.add(
            CatalogDocumentRevision(
                id=newer_revision_id,
                document_id=RECIPE_DOCUMENT_ID,
                kind="recipe",
                publisher=current.publisher,
                slug=current.slug,
                revision_number=2,
                schema_version=2,
                state="active",
                document=current.document,
                content_digest="d" * 64,
                execution_key="c" * 64,
                created_by="test",
                created_at=datetime(2026, 9, 6, tzinfo=UTC),
            )
        )

    def uncached(**kwargs: object) -> CacheResolution:
        return _resolution(
            kwargs["exact_revision_id"],
            recipe_cached=False,
            blockers=("recipe-not-cached",),
        )

    from .test_fleet_profiles import _SwitchAdapter

    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        cache_resolver=uncached,
        switch_adapter=_SwitchAdapter(),
    )
    profile = service.create(
        FleetProfileInput(
            name="Uncached",
            assignments=[
                FleetProfileAssignmentInput(
                    recipe_selector="vonk-forge/synthetic-tiny-build",
                    spark_ids=[NODE_1],
                    model_variant="fp16",
                )
            ],
        ),
        actor="test",
    )

    assert profile.assignments[0].recipe.revision_id == newer_revision_id
    assert profile.assignments[0].recipe.state == "Recipe not cached"
    assert any("is not in the local cache" in warning for warning in profile.warnings)


@pytest.mark.usefixtures("damaged_json_rows")
def test_profile_reads_the_newest_readable_revision_of_its_recipe() -> None:
    sessions = _sessions()
    _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile = service.create(
        FleetProfileInput(
            name="Upgrade",
            assignments=[
                FleetProfileAssignmentInput(
                    recipe_selector="vonk-forge/synthetic-tiny-build",
                    spark_ids=[NODE_1],
                )
            ],
        ),
        actor="test",
    )
    unreadable = {"contract": 1}
    with sessions.begin() as session:
        current = session.get(CatalogDocumentRevision, RECIPE_REVISION_ID)
        assert current is not None
        session.add(
            CatalogDocumentRevision(
                id="00000000-0000-4000-8000-000000000022",
                document_id=RECIPE_DOCUMENT_ID,
                kind="recipe",
                publisher=current.publisher,
                slug=current.slug,
                revision_number=2,
                schema_version=2,
                state="active",
                document=unreadable,
                content_digest=hashlib.sha256(
                    json.dumps(
                        unreadable, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest(),
                execution_key="c" * 64,
                created_by="test",
                created_at=datetime(2026, 9, 6, tzinfo=UTC),
            )
        )

    # The newest revision is unreadable, so the profile keeps the readable one.
    view = service.get_number(profile.number)
    assert view.assignments[0].recipe.revision_id == RECIPE_REVISION_ID

    with sessions.begin() as session:
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == RECIPE_REVISION_ID)
            .values(document=unreadable)
        )

    # Nothing readable: the choice needs attention, the profile still reads.
    view = service.get_number(profile.number)
    assert view.assignments[0].recipe.state == "Needs attention"
    assert view.assignments[0].spark_ids == [NODE_1]


@pytest.mark.usefixtures("damaged_json_rows")
def test_stored_application_with_a_retired_field_does_not_block_apply() -> None:
    from .test_fleet_profiles import _SwitchAdapter

    sessions = _sessions()
    _seed(sessions)
    service = FleetProfileService(
        sessions, clock=lambda: NOW, switch_adapter=_SwitchAdapter()
    )
    profile = service.create(
        FleetProfileInput(
            name="Retired",
            assignments=[
                FleetProfileAssignmentInput(
                    recipe_selector="vonk-forge/synthetic-tiny-build",
                    spark_ids=[NODE_1],
                )
            ],
        ),
        actor="test",
    )
    first = service.load(
        profile.number,
        actor="test",
        request_key="00000000-0000-4000-8000-000000000097",
    )
    # An older build stored fields the current contract no longer has.
    with sessions.begin() as session:
        row = session.get(FleetProfileApplication, first.id)
        assert row is not None
        session.execute(
            update(FleetProfileApplication)
            .where(FleetProfileApplication.id == first.id)
            .values(
                progress={**row.progress, "retired_field": 1},
                plan={**row.plan, "retired_field": 1},
            )
        )

    assert service.get_number(profile.number).number == profile.number
    assert service.application(first.id).id == first.id
    again = service.load(
        profile.number,
        actor="test",
        request_key="00000000-0000-4000-8000-000000000098",
    )
    assert again.id != first.id


def _option_profile(service: FleetProfileService, **choices: str):
    return service.create(
        FleetProfileInput.model_validate(
            {
                "name": "Options",
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-build",
                        "spark_ids": [NODE_1],
                        "assignment_name": "optioned",
                        "option_choices": choices,
                    }
                ],
            }
        ),
        actor="test",
    )


def test_a_missing_option_choice_is_saved_as_the_recipe_default() -> None:
    sessions = _sessions()
    _seed(sessions, options=True)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    created = _option_profile(service)

    assert created.definition.assignments[0].option_choices == {
        "verification": "standard"
    }
    assert created.assignments[0].option_choices == {"verification": "standard"}
    chosen = service.update(
        created.id,
        FleetProfileInput.model_validate(
            {
                "name": "Options",
                "expected_revision": created.revision,
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/synthetic-tiny-build",
                        "spark_ids": [NODE_1],
                        "assignment_name": "optioned",
                        "option_choices": {"verification": "adaptive"},
                    }
                ],
            }
        ),
        actor="test",
    )
    # A changed choice is a new profile revision that the load will act on.
    assert chosen.revision == created.revision + 1
    assert chosen.profile_digest != created.profile_digest
    assert chosen.assignments[0].option_choices == {"verification": "adaptive"}
    assert service.preview(chosen.id).assignments[0].option_choices == {
        "verification": "adaptive"
    }


def test_an_unknown_option_or_value_is_replaced_by_the_default_and_named() -> None:
    sessions = _sessions()
    _seed(sessions, options=True)
    service = FleetProfileService(sessions, clock=lambda: NOW)

    created = _option_profile(service, verification="nope", sampling="greedy")

    assert created.assignments[0].option_choices == {"verification": "standard"}
    assert created.definition.assignments[0].option_choices == {
        "verification": "standard"
    }
    assert any("nope" in warning for warning in created.warnings)
    assert any("sampling" in warning for warning in created.warnings)


def test_a_profile_holding_a_retired_choice_can_still_be_edited() -> None:
    """Every edit re-submits all assignments, so one stale choice blocked them all."""

    from vonk_control.models import FleetProfile

    sessions = _sessions()
    _seed(sessions, options=True)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    created = _option_profile(service, verification="adaptive")
    with sessions.begin() as session:
        row = session.get(FleetProfile, created.id)
        assert row is not None
        row.assignments = [
            {**assignment, "option_choices": {"verification": "retired"}}
            for assignment in row.assignments
        ]

    definition = service.get(created.id).definition
    edited = service.update(
        created.id,
        FleetProfileInput.model_validate(
            {
                **definition.model_dump(mode="json"),
                "expected_revision": 1,
                "description": "edited while a choice was stale",
            }
        ),
        actor="test",
    )

    assert edited.description == "edited while a choice was stale"
    assert edited.assignments[0].option_choices == {"verification": "standard"}
    assert any("retired" in warning for warning in edited.warnings)


def test_a_profile_saved_without_choices_runs_the_recipe_defaults() -> None:
    from vonk_control.models import FleetProfile

    sessions = _sessions()
    _seed(sessions, options=True)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    created = _option_profile(service, verification="adaptive")
    with sessions.begin() as session:
        row = session.get(FleetProfile, created.id)
        assert row is not None
        row.assignments = [
            {key: value for key, value in assignment.items() if key != "option_choices"}
            for assignment in row.assignments
        ]

    read = service.get(created.id)
    assert read.assignments[0].option_choices == {"verification": "standard"}
    assert service.preview(created.id).assignments[0].option_choices == {
        "verification": "standard"
    }


def test_a_stored_choice_the_newer_recipe_no_longer_offers_falls_back_visibly() -> None:
    from vonk_control.models import FleetProfile

    sessions = _sessions()
    _seed(sessions, options=True)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    created = _option_profile(service, verification="adaptive")
    with sessions.begin() as session:
        row = session.get(FleetProfile, created.id)
        assert row is not None
        row.assignments = [
            {**assignment, "option_choices": {"verification": "retired"}}
            for assignment in row.assignments
        ]
    read = service.get(created.id)
    assert read.assignments[0].option_choices == {"verification": "standard"}
    assert any("retired" in warning for warning in read.warnings)


def test_a_run_made_with_other_option_choices_is_not_the_assignments_state() -> None:
    from vonk_control.models import ClusterMapping, ClusterMappingNode, FleetProfile

    sessions = _sessions()
    _seed(sessions, options=True)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    created = _option_profile(service, verification="adaptive")
    with sessions.begin() as session:
        row = session.get(FleetProfile, created.id)
        assert row is not None
        [assignment] = service._execution_assignments(session, row)
        node = assignment.nodes[0]
        mapping = ClusterMapping(
            recipe_revision_id=RECIPE_REVISION_ID,
            topology_name=assignment.topology_name,
            generation=1,
            node_count=1,
            state="ready",
            parameters={"option_choices": {"verification": "standard"}},
            placement_digest="a" * 64,
            endpoint_owner_node_id=NODE_1,
            created_by="test",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(mapping)
        session.flush()
        session.add(
            ClusterMappingNode(
                mapping_id=mapping.id,
                node_id=node.node_id,
                rank=node.rank,
                role=node.role,
                endpoint_owner=node.endpoint_owner,
                created_at=NOW,
            )
        )
        session.flush()
        # The mapping was made for "standard": not this assignment's.
        assert service._assignment_state(session, assignment).current_state == (
            "not-placed"
        )
        matching = assignment.model_copy(
            update={"option_choices": {"verification": "standard"}}
        )
        assert service._assignment_state(session, matching).current_state == "placed"
