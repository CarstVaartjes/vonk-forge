from typing import Literal

ReconcileCode = Literal['reconcile.active_effect_unknown', 'reconcile.agent_unavailable', 'reconcile.capacity_busy', 'reconcile.install_provenance_mismatch', 'reconcile.install_provenance_unavailable', 'reconcile.installation_effect_unknown', 'reconcile.installation_identity_mismatch', 'reconcile.installation_identity_unavailable', 'reconcile.membership_changed', 'reconcile.operation_active', 'reconcile.rank_membership_changed', 'reconcile.recipe_revision_unavailable', 'reconcile.spec_identity_mismatch']

RECONCILE_CODE_VALUES: set[ReconcileCode] = { 'reconcile.active_effect_unknown', 'reconcile.agent_unavailable', 'reconcile.capacity_busy', 'reconcile.install_provenance_mismatch', 'reconcile.install_provenance_unavailable', 'reconcile.installation_effect_unknown', 'reconcile.installation_identity_mismatch', 'reconcile.installation_identity_unavailable', 'reconcile.membership_changed', 'reconcile.operation_active', 'reconcile.rank_membership_changed', 'reconcile.recipe_revision_unavailable', 'reconcile.spec_identity_mismatch',  }

def check_reconcile_code(value: str) -> ReconcileCode:
    if value in RECONCILE_CODE_VALUES:
        return value
    raise TypeError(f"Unexpected value {value!r}. Expected one of {RECONCILE_CODE_VALUES!r}")
