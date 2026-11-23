"""The release's words for capabilities and statuses, as the Controller serves them.

``GET /api/library/vocabulary`` is the one source: the command fetches it once
and calls :func:`use_vocabulary`; the renderers ask here and never spell a label
themselves. Without it (none published, an older Controller) the names show.
"""

from __future__ import annotations

from dataclasses import dataclass

from cluster_profiles.generated_control.models.catalog_vocabulary import (
    CatalogVocabulary,
)

#: The word shown where a value is missing, the same in the dashboard.
NOT_REPORTED = "Not reported"


@dataclass
class _ServedVocabulary:
    vocabulary: CatalogVocabulary | None = None


_served = _ServedVocabulary()


def use_vocabulary(vocabulary: CatalogVocabulary | None) -> None:
    """Remember the served vocabulary for this command; ``None`` forgets it."""
    _served.vocabulary = vocabulary


def capability_label(name: str) -> str:
    """The published label; a capability the release does not list shows its name."""
    if _served.vocabulary is not None:
        for capability in _served.vocabulary.capabilities:
            if capability.name == name:
                return capability.label
    return name


def status_meaning(status: str) -> str | None:
    """What the release says a qualification status means, if it says."""
    if _served.vocabulary is not None:
        for entry in _served.vocabulary.statuses:
            if entry.status == status:
                return entry.meaning
    return None
