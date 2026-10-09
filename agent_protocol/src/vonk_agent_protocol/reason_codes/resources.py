"""Resources reason codes."""

from ..wire_model import WireEnum


class ResourcePlanningCode(WireEnum):
    """Resource planning (memory, disk, parallelism) refusals and unknowns."""

    ENVELOPE_EXCEEDS_CAPACITY = "resource.envelope_exceeds_capacity"
    ENVELOPE_UNVERIFIED = "resource.envelope_unverified"
    ESTIMATE_UNCERTAIN = "resource.estimate_uncertain"
    EVIDENCE_INVALID = "resource.evidence_invalid"
    EVIDENCE_UNKNOWN = "resource.evidence_unknown"
    KNOBS_INVALID = "resource.knobs_invalid"
    PARALLELISM_DUPLICATE = "resource.parallelism_duplicate"
    PARALLELISM_INCONSISTENT = "resource.parallelism_inconsistent"
    PARALLELISM_TYPE = "resource.parallelism_type"
    PARALLELISM_UNKNOWN = "resource.parallelism_unknown"
    SETTINGS_KIND_UNKNOWN = "resource.settings_kind_unknown"
    SETTINGS_TYPE = "resource.settings_type"
    SETTINGS_UNKNOWN = "resource.settings_unknown"
    STOP_RELEASE_UNKNOWN = "resource.stop_release_unknown"
    CONTEXT_UNKNOWN = "resource.context_unknown"
    CONTEXT_EVIDENCE_INVALID = "resource.context_evidence_invalid"
    CONTEXT_UNSUPPORTED = "resource.context_unsupported"
    CONTEXT_EVIDENCE_UNKNOWN = "resource.context_evidence_unknown"
    CONCURRENCY_UNKNOWN = "resource.concurrency_unknown"
    CONCURRENCY_EVIDENCE_INVALID = "resource.concurrency_evidence_invalid"
    CONCURRENCY_UNSUPPORTED = "resource.concurrency_unsupported"
    CONCURRENCY_EVIDENCE_UNKNOWN = "resource.concurrency_evidence_unknown"
    BATCH_UNKNOWN = "resource.batch_unknown"
    BATCH_EVIDENCE_INVALID = "resource.batch_evidence_invalid"
    BATCH_UNSUPPORTED = "resource.batch_unsupported"
    BATCH_EVIDENCE_UNKNOWN = "resource.batch_evidence_unknown"


class ResourceTerm(WireEnum):
    """The effective settings whose capacity cost the resource planner derives."""

    CONTEXT = "context"
    CONCURRENCY = "concurrency"
    BATCH = "batch"


class ResourceTermProblem(WireEnum):
    """What is wrong with the evidence for one resource term."""

    UNKNOWN = "unknown"
    EVIDENCE_INVALID = "evidence_invalid"
    UNSUPPORTED = "unsupported"
    EVIDENCE_UNKNOWN = "evidence_unknown"


class StorageDemandCode(WireEnum):
    """Storage demand outcomes of an admission."""

    EVICTING = "storage.evicting"
    INSUFFICIENT_AFTER_EVICTION = "storage.insufficient_after_eviction"
    EVICTION_TIMED_OUT = "storage.eviction_timed_out"
