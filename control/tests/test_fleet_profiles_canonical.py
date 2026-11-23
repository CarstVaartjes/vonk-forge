from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from importlib.resources import files
from threading import Barrier

import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256

from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.reason_codes.model_cache import ModelCacheBlockerCode
from vonk_agent_protocol.state_machines import DesiredAssignmentState
from vonk_control.catalog.revision_contract import (
    rederive_projection,
    write_catalog_projection,
)
from vonk_control.fleet_profile_contract.definitions import (
    FleetProfileAssignmentInput,
    FleetProfileDefinition,
    FleetProfileInput,
    FleetProfileView,
    UnavailableFleetProfileView,
)
from vonk_control.fleet_profile_contract.review import FleetProfilePreview
from vonk_control.fleet_profiles.contracts import FleetProfileConflict
from vonk_control.fleet_profiles.service import FleetProfileService
from vonk_control.model_cache.contract import (
    CachedModelResolution,
    CachedRecipeResolution,
    CachedResourceEstimate,
    CacheResolution,
)
from vonk_control.model_primitives import Base
from vonk_control.models.catalog import (
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
)
from vonk_control.models.fleet import (
    AgentNode,
    AgentNodeContact,
    AgentNodeProfile,
    FleetProfile,
    FleetProfileApplication,
    User,
)

from .display_fixtures import create_draft, reworded
from .runtime_identity_support import (
    PACKAGED_RUNTIME_IDENTITY,
    inventory_items_fixture,
    set_inventory_hostname,
)

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
                    inventory_snapshot=inventory_items_fixture(
                        node_id,
                        observed_at=NOW,
                        identity=AgentRuntimeIdentity.model_validate(
                            {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                        ),
                    ),
                    contact=AgentNodeContact(protocol_version=2, last_seen_at=NOW),
                )
                for node_id in (NODE_1, NODE_2)
            ]
        )
        session.add_all(
            [
                AgentNodeProfile(node_id=NODE_1, display_name="Spark One"),
                AgentNodeProfile(node_id=NODE_2, display_name="Spark Two"),
            ]
        )
        set_inventory_hostname(session, NODE_1, "spark-one")
        set_inventory_hostname(session, NODE_2, "spark-two")
        session.add_all(
            [
                CatalogDocument(
                    id=MODEL_DOCUMENT_ID,
                    kind="model",
                    publisher=model.identity.publisher,
                    slug=model.identity.slug,
                    title=model.identity.slug,
                    created_by="test",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                CatalogDocument(
                    id=RECIPE_DOCUMENT_ID,
                    kind="recipe",
                    publisher=recipe.identity.publisher,
                    slug=recipe.identity.slug,
                    title=recipe.identity.slug,
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
    from vonk_control.models.fleet import FleetProfile

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
    from vonk_control.fleet_profiles.projection_support import _profile_document
    from vonk_control.models.fleet import FleetProfile

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
        # Past the recipe projection, as damage reaches a stored row.
        session.execute(
            update(FleetProfile)
            .where(FleetProfile.id == created.id)
            .values({field: value})
        )
    with sessions() as session:
        row = session.get(FleetProfile, created.id)
        assert row is not None
        # A marker substituted as labels would silently acquire a content
        # identity. No bound plan may mistake that for saved authoring intent.
        with pytest.raises(Exception) as _ending:
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
    with pytest.raises(Exception) as _ending:
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


def _draft_input(name: str = "Draft") -> FleetProfileDefinition:
    return FleetProfileDefinition.model_validate(
        {
            "name": name,
            "assignments": [
                {
                    "recipe_selector": "vonk-forge/synthetic-tiny-build",
                    "spark_ids": [NODE_1],
                    "desired_state": "running",
                    "assignment_name": "canonical",
                }
            ],
        }
    )


def _stored_state(sessions: sessionmaker) -> list[tuple]:
    with sessions() as session:
        return [
            (row.id, row.number, row.revision, row.name)
            for row in session.scalars(select(FleetProfile))
        ]


def test_a_draft_preview_writes_nothing_and_names_no_revision() -> None:
    sessions = _sessions()
    _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    before = _stored_state(sessions)

    preview = service.preview_draft(7, _draft_input())

    assert preview.profile_revision is None
    assert preview.profile_name == "Draft"
    assert preview.assignments[0].recipe_revision_id == RECIPE_REVISION_ID
    assert _stored_state(sessions) == before == []


@pytest.mark.parametrize("number", [1, 7])
def test_a_draft_review_equals_the_review_after_the_same_save(number: int) -> None:
    sessions = _sessions()
    _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    if number == 1:
        service.update_number(1, FleetProfileInput(name="Old"), actor="test")
    draft = _draft_input()
    reviewed = service.preview_draft(number, draft)

    saved = service.update_number(
        number,
        FleetProfileInput(**draft.model_dump(), expected_revision=int(number == 1)),
        actor="test",
    )
    after = service.preview(saved.id)

    assert after.profile_revision == saved.revision
    assert reviewed.effects_digest == after.effects_digest
    assert reviewed.profile_definition == after.profile_definition


def _draft_input_as_save() -> FleetProfileInput:
    return FleetProfileInput(**_draft_input().model_dump())


def test_a_draft_equal_to_the_stored_definition_is_reviewed_as_the_replayed_save() -> (
    None
):
    from vonk_control.catalog.entities import CatalogEntityService

    sessions = _sessions()
    _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    saved = service.update_number(1, _draft_input_as_save(), actor="test")
    # A catalog refresh adds options; a save of equal content still replays
    # without normalising, so the draft review must not add them either.
    with sessions() as session:
        document = json.loads(
            json.dumps(
                session.get(CatalogDocumentRevision, RECIPE_REVISION_ID).document
            )
        )
    document["options"] = RECIPE_OPTIONS
    catalog = CatalogEntityService(sessions, clock=lambda: NOW)
    revision = catalog.revise(RECIPE_DOCUMENT_ID, document, actor="test")
    catalog.resolve(revision.id, actor="test")

    definition = service.definition_number(1).definition
    assert definition is not None
    draft = service.preview_draft(1, definition)
    replayed = service.update_number(
        1,
        FleetProfileInput(**definition.model_dump(), expected_revision=saved.revision),
        actor="test",
    )
    after = service.preview(replayed.id)

    assert draft.profile_revision is None
    assert draft.effects_digest == after.effects_digest


def test_a_draft_is_refused_like_a_save() -> None:
    sessions = _sessions()
    _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    bad = FleetProfileInput.model_validate(
        {
            "name": "Bad",
            "assignments": [
                {"recipe_selector": "vonk-forge/no-such", "spark_ids": [NODE_1]}
            ],
        }
    )
    with pytest.raises(Exception) as save_error:
        service.update_number(3, bad, actor="test")
    with pytest.raises(type(save_error.value)) as draft_error:
        service.preview_draft(
            3, FleetProfileDefinition(**bad.model_dump(exclude={"expected_revision"}))
        )
    assert str(draft_error.value) == str(save_error.value)


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

    replayed = service.update_number(
        2,
        FleetProfileInput(name="Second", expected_revision=1, assignments=[]),
        actor="test",
    )
    assert replayed.revision == created.revision
    assert replayed.profile_digest == created.profile_digest
    changed = service.update_number(
        2,
        FleetProfileInput(name="Edited", expected_revision=1, assignments=[]),
        actor="test",
    )
    assert changed.revision == 2
    with pytest.raises(Exception) as _ending:
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


def test_a_profile_read_parses_its_saved_definition_once(monkeypatch) -> None:
    """The choices, the profile digest and the view share one parsed definition."""

    sessions = _sessions()
    _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    created = service.create(
        FleetProfileInput(
            name="Once",
            assignments=[
                FleetProfileAssignmentInput(
                    recipe_selector="vonk-forge/synthetic-tiny-build",
                    spark_ids=[NODE_1],
                )
            ],
        ),
        actor="test",
    )
    parses: list[str] = []
    parse = FleetProfileDefinition.model_validate_json.__func__

    def counted(cls, *args, **kwargs):
        parses.append(cls.__name__)
        return parse(cls, *args, **kwargs)

    monkeypatch.setattr(
        FleetProfileDefinition, "model_validate_json", classmethod(counted)
    )
    view = service.read_number(created.number)
    assert isinstance(view, FleetProfileView)
    assert [item.recipe_selector for item in view.assignments] == [
        "vonk-forge/synthetic-tiny-build"
    ]
    assert parses.count("FleetProfileDefinition") == 1


def test_profile_read_uses_the_read_only_latest_cache_resolver() -> None:
    sessions = _sessions()
    _seed(sessions)
    newer_revision_id = "00000000-0000-4000-8000-000000000022"
    with sessions.begin() as session:
        current = session.get(CatalogDocumentRevision, RECIPE_REVISION_ID)
        assert current is not None
        recipe = RecipeDefinition.model_validate(current.document)
        newer = reworded(recipe, "New revision").model_dump(mode="json")
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
                document=newer,
                content_digest=document_sha256(newer),
                execution_key=current.execution_key,
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

    from .fleet_profiles_support import _SwitchAdapter

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
    profile = service.get(profile.id)
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
        recipe = RecipeDefinition.model_validate(current.document)
        newer = reworded(recipe, "New revision").model_dump(mode="json")
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
                document=newer,
                content_digest=document_sha256(newer),
                execution_key=current.execution_key,
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

    from .fleet_profiles_support import _SwitchAdapter

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
    created = service.get(created.id)
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
        recipe = RecipeDefinition.model_validate(current.document)
        newer = reworded(recipe, "New revision").model_dump(mode="json")
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
                document=newer,
                content_digest=document_sha256(newer),
                execution_key=current.execution_key,
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

    from .fleet_profiles_support import _SwitchAdapter

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

    profile = service.get(profile.id)
    assert profile.assignments[0].recipe.revision_id == newer_revision_id
    assert profile.assignments[0].recipe.state == "Recipe not cached"
    assert any("is not in the local cache" in warning for warning in profile.warnings)


@pytest.mark.usefixtures("damaged_json_rows")
def test_profile_reports_an_unreadable_head_without_substituting_older_content() -> (
    None
):
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

    # A broken head is actionable; it must not silently select older content.
    view = service.get_number(profile.number)
    assert view.assignments[0].recipe.state == "Needs attention"

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


def test_a_retracted_recipe_needs_attention_until_published_again() -> None:
    """A recipe the library no longer lists has no head: its revisions stay
    active, but the profile does not load one of them; publishing it again
    makes it current and admits a fresh load."""
    sessions = _sessions()
    _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    profile = service.create(
        FleetProfileInput(
            name="Retracted",
            assignments=[
                FleetProfileAssignmentInput(
                    recipe_selector="vonk-forge/synthetic-tiny-build",
                    spark_ids=[NODE_1],
                )
            ],
        ),
        actor="test",
    )

    def set_head(revision_id: str | None) -> None:
        with sessions.begin() as session:
            session.execute(
                update(CatalogDocumentHead)
                .where(CatalogDocumentHead.kind == "recipe")
                .values(active_revision_id=revision_id)
            )

    set_head(None)
    view = service.get_number(profile.number)
    assert view.assignments[0].recipe.state == "Needs attention"
    assert any("no longer published" in warning for warning in view.warnings)
    retracted = service.preview(profile.id)
    assert not retracted.allowed
    assert any(
        reason.code == "profile.recipe_unavailable" for reason in retracted.reasons
    )

    set_head(RECIPE_REVISION_ID)
    view = service.get_number(profile.number)
    assert view.assignments[0].recipe.state != "Needs attention"
    with sessions() as session:
        assert FleetProfileService._recipe_identity(
            session, "vonk-forge/synthetic-tiny-build"
        ) == (RECIPE_DOCUMENT_ID, RECIPE_REVISION_ID)
    republished = service.preview(profile.id)
    assert all(
        reason.code != "profile.recipe_unavailable" for reason in republished.reasons
    )


@pytest.mark.usefixtures("damaged_json_rows")
def test_stored_application_with_a_retired_field_does_not_block_apply() -> None:
    from .fleet_profiles_support import _SwitchAdapter

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

    from vonk_control.models.fleet import FleetProfile

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
    from vonk_control.models.fleet import FleetProfile

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
    from vonk_control.models.fleet import FleetProfile

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
    from vonk_control.models.fleet import FleetProfile
    from vonk_control.models.mapping import ClusterMapping, ClusterMappingNode

    sessions = _sessions()
    _seed(sessions, options=True)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    created = _option_profile(service, verification="adaptive")
    with sessions.begin() as session:
        row = session.get(FleetProfile, created.id)
        assert row is not None
        [assignment] = service._execution_assignments(session, service._choices(row))
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


@pytest.mark.parametrize("assignment_count,selector_count", [(1, 1), (34, 1), (34, 34)])
def test_profile_save_bounds_document_parses_and_database_round_trips(
    monkeypatch,
    postgres_engine,
    assignment_count,
    selector_count,
):
    """A save must not resolve and parse the same selector per assignment and view."""
    from collections import Counter, OrderedDict

    import vonk_control.catalog.revision_contract as contract

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(sessions)
    from vonk_control.catalog.entities import CatalogEntityService

    selectors = ["vonk-forge/synthetic-tiny-build"]
    with sessions.begin() as session:
        template_row = session.get(CatalogDocumentRevision, RECIPE_REVISION_ID)
        assert template_row is not None
        template = template_row.document
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == template_row.id)
            .values(
                projected=contract.write_catalog_projection(
                    contract.read_recipe_projection(template_row)
                )
            )
        )
    catalog = CatalogEntityService(sessions, clock=lambda: NOW)
    for index in range(1, selector_count):
        document = json.loads(json.dumps(template))
        document["identity"]["slug"] = f"synthetic-{index}"
        revision = create_draft(catalog, document, actor="test")
        catalog.resolve(revision.id, actor="test")
        selectors.append(f"vonk-forge/synthetic-{index}")

    def reject_observation(*_args, **_kwargs):
        raise AssertionError("a draft save consulted cache storage or placement")

    service = FleetProfileService(
        sessions,
        clock=lambda: NOW,
        cache_resolver=reject_observation,
        assessment_provider=reject_observation,
    )
    created = service.update_number(1, FleetProfileInput(name="Before"), actor="test")
    monkeypatch.setattr(contract, "_document_cache", OrderedDict(), raising=False)
    monkeypatch.setattr(contract, "_document_cache_size", 0, raising=False)
    parses = Counter()
    for kind in ("model", "recipe"):
        original = getattr(contract, f"read_{kind}")

        def counting(raw, parser=original, name=kind):
            parses[name] += 1
            return parser(raw)

        monkeypatch.setattr(contract, f"read_{kind}", counting)
    project = contract.RecipeRevisionProjection.model_validate_json

    def projected(_cls, raw, *args, **kwargs):
        parses["projection"] += 1
        return project(raw, *args, **kwargs)

    monkeypatch.setattr(
        contract.RecipeRevisionProjection, "model_validate_json", classmethod(projected)
    )
    original_hash = hashlib.sha256

    def reject_catalog_hash(data=b"", *args, **kwargs):
        try:
            payload = json.loads(data)
        except (TypeError, ValueError, UnicodeError):
            payload = None
        assert not (
            isinstance(payload, dict)
            and "identity" in payload
            and "kind" in payload
            and ("models" in payload or "files" in payload)
        ), "profile save re-hashed a stored catalog document"
        return original_hash(data, *args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", reject_catalog_hash)
    queries = []
    engine = sessions.kw["bind"]

    def counted(_connection, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", counted)
    try:
        saved = service.update_number(
            1,
            FleetProfileInput(
                name="Pins",
                expected_revision=created.revision,
                assignments=[
                    FleetProfileAssignmentInput(
                        recipe_selector=selectors[index % selector_count],
                        spark_ids=[NODE_1],
                        assignment_name=f"slot-{index}",
                    )
                    for index in range(assignment_count)
                ],
            ),
            actor="test",
        )
    finally:
        event.remove(engine, "before_cursor_execute", counted)
    assert len(saved.assignments) == assignment_count
    assert saved.cache_summary.unknown == assignment_count
    assert parses["recipe"] <= selector_count, parses
    assert parses["projection"] <= selector_count, parses
    assert parses["model"] <= 1, parses
    # Recipe heads, shared model titles and gates are batched; this fixture
    # shares one model, so adding distinct recipes adds no SQL round trips.
    assert len(queries) <= 20, (len(queries), queries)


@pytest.mark.parametrize("projection_fails", [False, True])
def test_save_observer_timeout_is_atomic_and_reconnects_to_committed_content(
    postgres_engine, monkeypatch, projection_fails
):
    """Catches an early commit, a partial draft, or a replay editing twice."""
    from threading import Event

    from vonk_control.models.fleet import FleetProfile

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    before = service.update_number(13, FleetProfileInput(name="Before"), actor="test")
    requested = FleetProfileInput(
        name="After",
        expected_revision=before.revision,
        assignments=[
            FleetProfileAssignmentInput(
                recipe_selector="vonk-forge/synthetic-tiny-build",
                spark_ids=[NODE_1],
            )
        ],
    )
    entered, release = Event(), Event()
    original = service._view

    def delayed(session, row, **kwargs):
        # The write has flushed, but the transaction must still be atomic.
        entered.set()
        assert release.wait(timeout=10)
        if projection_fails:
            raise RuntimeError("injected response projection fault")
        return original(session, row, **kwargs)

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as pool:
        patch.setattr(service, "_view", delayed)
        pending = pool.submit(service.update_number, 13, requested, actor="test")
        try:
            assert entered.wait(timeout=10)
            with pytest.raises(TimeoutError):
                pending.result(
                    timeout=0.01
                )  # wall-clock: deliberately expire observation while the injected fault holds
            # Losing the response does not cancel the server thread, and other
            # readers see the whole old document until the transaction commits.
            observed = service.definition_number(13)
            assert observed.revision == before.revision
            assert observed.definition == before.definition
        finally:
            release.set()
        if projection_fails:
            with pytest.raises(
                RuntimeError, match="injected response projection fault"
            ):
                pending.result(timeout=10)
        else:
            assert pending.result(timeout=10).revision == before.revision + 1
    with sessions() as session:
        stored = session.get(FleetProfile, before.id)
        assert stored is not None
        assert stored.revision == before.revision + (not projection_fails)
        assert service._definition(stored) == (
            before.definition
            if projection_fails
            else FleetProfileDefinition.model_validate(
                requested.model_dump(exclude={"expected_revision"})
            )
        )
    # A failed transaction is retryable; a committed lost response is an equal
    # content replay, even with the client's original expected revision.
    replayed = service.update_number(13, requested, actor="test")
    assert replayed.revision == before.revision + 1


@pytest.mark.parametrize("endpoint", ["get", "preview"])
@pytest.mark.parametrize("selector_count", [1, 34])
def test_profile_read_observes_distinct_content_once(
    monkeypatch, postgres_engine, tmp_path, endpoint, selector_count
):
    """Duplicate assignments must not repeat storage observation or model lookup."""
    import time

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(sessions)
    from collections import Counter, OrderedDict

    from vonk_control.catalog import revision_contract as contract
    from vonk_control.catalog.entities import CatalogEntityService

    selectors = ["vonk-forge/synthetic-tiny-build"]
    with sessions() as session:
        template_row = session.get(CatalogDocumentRevision, RECIPE_REVISION_ID)
        assert template_row is not None
        template = template_row.document
    catalog = CatalogEntityService(sessions, clock=lambda: NOW)
    for index in range(1, selector_count):
        document = json.loads(json.dumps(template))
        document["identity"]["slug"] = f"latency-{index}"
        revision = create_draft(catalog, document, actor="test")
        catalog.resolve(revision.id, actor="test")
        selectors.append(f"vonk-forge/latency-{index}")
    from vonk_control.fleet_profiles.read_observation import observe_storage
    from vonk_control.model_cache.service import ModelCacheService
    from vonk_control.runtime_image_preparation.storage import (
        FilesystemRuntimeImageStorage,
    )

    cache = ModelCacheService(sessions, tmp_path / "models", clock=lambda: NOW)
    images = FilesystemRuntimeImageStorage(tmp_path / "images")
    storage_calls = Counter()

    def model_receipt(_digest, _size):
        storage_calls["model"] += 1
        time.sleep(0.5)  # injected network storage latency

    def image_receipt(_digest):
        storage_calls["image"] += 1
        time.sleep(0.5)  # injected network storage latency

    # The dedup layer is the decorator on the storage owner; keep it on the fake.
    monkeypatch.setattr(cache, "_stored_object", observe_storage(model_receipt))
    monkeypatch.setattr(images, "_observe_stored_image", image_receipt)
    calls = []

    def resolve(**kwargs):
        calls.append(kwargs["exact_revision_id"])
        cache._stored_object("a" * 64, 1)
        present = images.build_archive_available("b" * 64, 1)
        return _resolution(kwargs["exact_revision_id"], recipe_cached=present)

    service = FleetProfileService(sessions, clock=lambda: NOW, cache_resolver=resolve)
    service.update_number(
        1,
        FleetProfileInput(
            name="Latency",
            assignments=[
                FleetProfileAssignmentInput(
                    recipe_selector=selectors[index % selector_count],
                    spark_ids=[NODE_1],
                    assignment_name=f"slot-{index}",
                )
                for index in range(34)
            ],
        ),
        actor="test",
    )
    parses = Counter()
    monkeypatch.setattr(contract, "_document_cache", OrderedDict())
    monkeypatch.setattr(contract, "_document_cache_size", 0)
    parser = contract.read_recipe

    def parsed(raw):
        parses["recipe"] += 1
        return parser(raw)

    monkeypatch.setattr(contract, "read_recipe", parsed)
    from vonk_control.fleet_profiles import read_observation as observation

    submissions = []
    submit = observation._POOL.submit

    def submitted(*args, **kwargs):
        submissions.append(True)
        return submit(*args, **kwargs)

    monkeypatch.setattr(observation._POOL, "submit", submitted)
    queries = []

    def counted(_connection, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)

    event.listen(postgres_engine, "before_cursor_execute", counted)
    started = time.monotonic()
    try:
        if endpoint == "get":
            result = service.get_number(1)
            assert len(result.assignments) == 34
        else:
            # This is the numbered route's producer sequence.
            identity = service.definition_number(1)
            assert identity.id is not None
            result = service.preview(identity.id)
            assert len(result.assignments) == 34
    finally:
        elapsed = time.monotonic() - started
        event.remove(postgres_engine, "before_cursor_execute", counted)
    print(
        f"{endpoint} selectors={selector_count}: {elapsed:.3f}s SQL={len(queries)} resolutions={len(calls)} storage={storage_calls} parses={parses} submissions={len(submissions)}"
    )
    # Distinct revision identities still resolve exactly once, but shared
    # content must not pay an executor handoff for every assignment/selector.
    # This catches a serial observe_storage(resolver) dispatch in the loop,
    # even on a host where its scheduling latency stays below five seconds.
    assert len(calls) == (selector_count if endpoint == "get" else 0)
    assert len(set(calls)) == len(calls)
    assert len(submissions) == (1 if endpoint == "get" else 0)
    assert storage_calls == (
        Counter(model=1, image=1) if endpoint == "get" else Counter()
    )
    assert parses["recipe"] <= selector_count
    assert elapsed < 5.0  # wall-clock: injected latency budget
    assert len(queries) <= (12 if endpoint == "get" else 25)
    assert not any("FOR UPDATE" in query for query in queries)


def test_profile_list_skips_cache_check_and_single_read_observes(
    postgres_engine,
):
    """The list is the cheap overview; cache truth is read per profile."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(sessions)
    calls = []

    def resolve(**kwargs):
        calls.append(kwargs["exact_revision_id"])
        return _resolution(kwargs["exact_revision_id"], recipe_cached=True)

    service = FleetProfileService(sessions, clock=lambda: NOW, cache_resolver=resolve)
    service.update_number(
        1,
        FleetProfileInput(
            name="Overview",
            assignments=[
                FleetProfileAssignmentInput(
                    recipe_selector="vonk-forge/synthetic-tiny-build",
                    spark_ids=[NODE_1],
                    assignment_name="slot",
                )
            ],
        ),
        actor="test",
    )
    calls.clear()
    listed = service.list()
    assert calls == []
    assert listed.profiles
    entries = [e for e in listed.profiles if isinstance(e, FleetProfileView)]
    assert len(entries) == len(listed.profiles)
    for entry in entries:
        assert not any("retry this profile read" in w for w in entry.warnings)
    assert entries[0].cache_summary.unknown == 1
    read = service.read_number(1)
    assert isinstance(read, FleetProfileView)
    assert len(calls) == 1
    assert read.cache_summary.unknown == 0


def _add_recipe(sessions: sessionmaker, slug: str) -> str:
    """Publish another active recipe revision; returns its selector."""
    with sessions.begin() as session:
        base = session.get(CatalogDocumentRevision, RECIPE_REVISION_ID)
        assert base is not None
        document = json.loads(json.dumps(base.document))
        document["identity"]["slug"] = slug
        root = CatalogDocument(
            kind="recipe",
            publisher=base.publisher,
            slug=slug,
            title=slug,
            created_by="test",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(root)
        session.flush()
        session.add(
            CatalogDocumentRevision(
                document_id=root.id,
                kind="recipe",
                publisher=base.publisher,
                slug=slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=document,
                content_digest=document_sha256(document),
                # Every catalog writer stores the projection beside the source.
                projected=write_catalog_projection(
                    rederive_projection({}, RecipeDefinition.model_validate(document)),
                    kind="recipe",
                ),
                execution_key="c" * 64,
                created_by="test",
                created_at=NOW,
            )
        )
        return f"{base.publisher}/{slug}"


def test_profile_list_reads_a_page_in_queries_independent_of_its_rows() -> None:
    """Each listed row shows exactly its single read, without per-row queries."""
    sessions = _sessions()
    _seed(sessions)
    service = FleetProfileService(sessions, clock=lambda: NOW)
    engine = sessions.kw["bind"]

    def add_profiles(first: int, count: int) -> None:
        for index in range(first, first + count):
            service.create(
                FleetProfileInput(
                    name=f"Page {index}",
                    assignments=[
                        FleetProfileAssignmentInput(
                            recipe_selector=_add_recipe(sessions, f"page-{index}"),
                            spark_ids=[NODE_1],
                        )
                    ],
                ),
                actor="test",
            )

    def listed_statements() -> list[str]:
        statements: list[str] = []

        def record(*args: object) -> None:
            statements.append(str(args[2]))

        event.listen(engine, "before_cursor_execute", record)
        try:
            service.list()
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return statements

    # A recipe without a current revision, and a definition that cannot be
    # read, keep their own row projections on both pages.
    add_profiles(0, 4)
    with sessions.begin() as session:
        session.execute(
            update(CatalogDocumentHead)
            .where(CatalogDocumentHead.slug == "page-2")
            .values(active_revision_id=None)
        )
        session.execute(
            update(FleetProfile).where(FleetProfile.number == 4).values(assignments={})
        )
    few = listed_statements()
    add_profiles(4, 6)
    listed = service.list()

    many = listed_statements()
    assert len(many) <= len(few)
    # The overview reads stored projections, never the recipes' source documents.
    assert not [
        item
        for item in many
        if re.search(r"catalog_document_revisions\.document\b", item)
    ]
    assert len(listed.profiles) == 10
    with sessions() as session:
        rows = session.scalars(select(FleetProfile).order_by(FleetProfile.number))
        assert listed.profiles == [
            service._read_view(session, row, observe=False) for row in rows
        ]
    states = [
        entry.assignments[0].recipe.state
        for entry in listed.profiles
        if isinstance(entry, FleetProfileView)
    ]
    assert states.count("Needs attention") == 1
    assert sum(isinstance(e, UnavailableFleetProfileView) for e in listed.profiles) == 1


@pytest.mark.parametrize("endpoint", ["get", "preview"])
@pytest.mark.parametrize("asset", ["model", "image"])
def test_profile_storage_wait_is_bounded_and_recovers(
    monkeypatch, postgres_engine, tmp_path, endpoint, asset, frozen_pass_budgets
):
    """A stalled receipt cannot hold a profile response or be called per assignment."""
    from threading import Event

    from tests.fleet_profiles_support import _assessment, _exact_preparation
    from vonk_agent_protocol.reason_codes.profiles import ProfileReasonCode
    from vonk_control.categorized_errors import BookkeepingUnknown
    from vonk_control.fleet_profiles.read_observation import (
        observe_storage,
        profile_read,
    )
    from vonk_control.model_cache.service import ModelCacheService
    from vonk_control.runtime_image_preparation.storage import (
        FilesystemRuntimeImageStorage,
    )

    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    _seed(sessions)
    cache = ModelCacheService(sessions, tmp_path / "cache", clock=lambda: NOW)
    entered, release, completed = Event(), Event(), Event()
    storage_calls = []
    images = FilesystemRuntimeImageStorage(tmp_path / "images")
    original = (
        cache._stored_object if asset == "model" else images._observe_stored_image
    )

    def delayed(digest, *args):
        storage_calls.append(digest)
        entered.set()
        released = release.wait(30)
        try:
            assert released, "test did not release its injected storage fault"
            return original(digest, *args)
        finally:
            completed.set()

    if asset == "model":
        monkeypatch.setattr(cache, "_stored_object", observe_storage(delayed))
    else:
        monkeypatch.setattr(images, "_observe_stored_image", delayed)

    def stored():
        if asset == "model":
            return cache._stored_object("a" * 64, 1)
        return images.build_archive_available("a" * 64, 1)

    def resolve(**kwargs):
        stored()
        return _resolution(kwargs["exact_revision_id"], recipe_cached=True)

    def assess(_session, _assignment, nodes, **_kwargs):
        stored()
        return _assessment(_exact_preparation(nodes))

    service = FleetProfileService(
        sessions, clock=lambda: NOW, cache_resolver=resolve, assessment_provider=assess
    )
    saved = service.update_number(
        1,
        FleetProfileInput(
            name="Fault",
            assignments=[
                FleetProfileAssignmentInput(
                    recipe_selector="vonk-forge/synthetic-tiny-build",
                    spark_ids=[NODE_1],
                    assignment_name=f"slot-{index}",
                )
                for index in range(34)
            ],
        ),
        actor="test",
    )

    def read():
        if endpoint == "preview":
            return service.preview(saved.id)
        return service.read_number(1)

    from vonk_control.fleet_profiles import read_observation as profile_read_observation

    from .pass_budget_clocks import completing_wait, expiring_wait

    # The request budget runs out while the receipt is stalled, never by load.
    monkeypatch.setattr(
        profile_read_observation, "wait", expiring_wait(frozen_pass_budgets, entered)
    )
    try:
        # Establish the stalled I/O before measuring the profile projection.
        with pytest.raises(BookkeepingUnknown):
            profile_read(stored)()
        assert entered.wait(30)
        started = frozen_pass_budgets.seconds
        result = read()
        # The response honours its own request deadline, whatever the stall.
        assert (
            frozen_pass_budgets.seconds - started
            <= profile_read_observation.PROFILE_READ_STORAGE_SECONDS
        )
        assert entered.is_set()
        # The stalled receipt completes only after release: order, not latency.
        assert not completed.is_set(), "the response waited for the stalled receipt"
        assert not release.is_set(), "the response waited for the fault to clear"
        assert storage_calls == ["a" * 64]
        if endpoint == "preview":
            assert isinstance(result, FleetProfilePreview)
            assert not result.allowed
            assert any(
                reason.code == ProfileReasonCode.PREPARATION_UNAVAILABLE
                and "pending" in reason.detail
                for reason in result.reasons
            )
        else:
            assert isinstance(result, FleetProfileView)
            assert result.cache_summary.unknown == 34
            assert any("pending" in warning for warning in result.warnings)
    finally:
        release.set()
        assert completed.wait(30)
    monkeypatch.setattr(profile_read_observation, "wait", completing_wait)
    # A new observation uses the storage owner again after the fault clears.
    result = read()
    if endpoint == "preview":
        assert isinstance(result, FleetProfilePreview)
        assert not any("pending after" in reason.detail for reason in result.reasons)
    else:
        assert isinstance(result, FleetProfileView)
        assert result.cache_summary.unknown == 0
    assert 1 <= len(storage_calls) <= 2
    # Admission outside a profile observation never uses the request memo.
    before = len(storage_calls)
    stored()
    assert len(storage_calls) == before + 1


def test_profile_observation_reuses_only_fresh_results(monkeypatch):
    """Completed probes expire; admission outside the read scope always observes."""
    from threading import Event
    from types import SimpleNamespace

    from vonk_control.fleet_profiles import read_observation as observation

    now = [0.0]
    monkeypatch.setattr(observation, "time", SimpleNamespace(monotonic=lambda: now[0]))
    published = Event()
    finish = observation._finished

    def finished(key, future):
        finish(key, future)
        published.set()

    monkeypatch.setattr(observation, "_finished", finished)
    calls = []

    @observation.observe_storage
    def probe():
        calls.append(True)
        return len(calls)

    @observation.profile_read
    def read():
        return probe()

    assert read() == 1
    assert published.wait(30)
    assert read() == 1
    now[0] = 6.0
    assert read() == 2
    assert probe() == 3


def test_profile_read_preserves_observer_progress_and_next_check():
    """Catches dropping the observer's progress in a generic unknown warning."""
    from vonk_agent_protocol.lifecycle_vocabulary import WaitReason
    from vonk_control.categorized_errors import BookkeepingUnknown

    sessions = _sessions()
    _seed(sessions)
    detail = (
        "Cache observation: 7/146 objects checked; Controller cache observer "
        "resumes durable checkpoints; next check 2026-10-22T16:00:05Z."
    )

    def pending(**_kwargs):
        raise BookkeepingUnknown(detail, reason=WaitReason.OBSERVATION_UNAVAILABLE)

    service = FleetProfileService(sessions, clock=lambda: NOW, cache_resolver=pending)
    service.update_number(
        1,
        FleetProfileInput(
            name="Observing",
            assignments=[
                FleetProfileAssignmentInput(
                    recipe_selector="vonk-forge/synthetic-tiny-build",
                    spark_ids=[NODE_1],
                )
            ],
        ),
        actor="test",
    )
    view = service.get_number(1)
    assert view.cache_summary.unknown == 1
    assert any(detail in warning for warning in view.warnings)


def test_profile_preview_preserves_cache_inspection_progress():
    """Catches replacing the inspector's actual wait with a generic cache message."""
    from tests.fleet_profiles_support import _assessment, _exact_preparation
    from vonk_agent_protocol.agent_words import (
        ProfileReasonSeverity,
        RunSwitchReasonScopeName,
    )
    from vonk_agent_protocol.reason_codes.profiles import ProfileReasonCode
    from vonk_agent_protocol.reason_codes.runs import RunSwitchCode
    from vonk_control.run_switch_contract.evidence import RunSwitchReason

    sessions = _sessions()
    _seed(sessions)
    detail = (
        "Cache observation: 7/146 objects checked; next check 2026-10-22T16:00:05Z."
    )

    def assess(_session, _assignment, nodes, **_kwargs):
        assessment = _assessment(_exact_preparation(nodes))
        return assessment.model_copy(
            update={
                "allowed": False,
                "preparation": None,
                "blockers": [
                    RunSwitchReason(
                        code=RunSwitchCode.ARTIFACT_INSPECTION_UNAVAILABLE,
                        detail=detail,
                        scope=RunSwitchReasonScopeName.ARTIFACT.value,
                        severity=ProfileReasonSeverity.BLOCKER.value,
                    )
                ],
            }
        )

    service = FleetProfileService(
        sessions, clock=lambda: NOW, assessment_provider=assess
    )
    saved = service.update_number(
        1,
        FleetProfileInput(
            name="Inspecting",
            assignments=[
                FleetProfileAssignmentInput(
                    recipe_selector="vonk-forge/synthetic-tiny-build",
                    spark_ids=[NODE_1],
                )
            ],
        ),
        actor="test",
    )
    preview = service.preview(saved.id)
    assert not preview.allowed
    assert any(
        reason.code == ProfileReasonCode.PREPARATION_UNAVAILABLE
        and reason.detail == detail
        for reason in preview.reasons
    )
