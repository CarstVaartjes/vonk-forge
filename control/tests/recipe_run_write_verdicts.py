"""Reviewed caller-owned RecipeRun writes; additions must receive a new verdict."""

from __future__ import annotations

import ast
import hashlib

# Caller-owned sessions are not automatically transition decisions. These
# verdicts classify writes/callbacks, not permission to add another occurrence.
RECIPE_RUN_WRITE_VERDICTS: dict[str, dict[str, tuple[str, str]]] = {
    "agent_jobs/claim_authority.py": {
        "_reconcile_agent_upgrade": (
            "c53cdd168ced1ab26b9b1db93d8d01cf617b76d3d620df315fde650d5a8e85df",
            "HTTP wait: result consumer can lose a run fence; middleware disposition owns 503",
        ),
        "_reject_recipe_build_claim": (
            "7e0465c17bd2c79c77e911437e75a5654ad2248fb05a1df596b0b39c32955cf7",
            "HTTP wait: claim result projection rolls back and next claim re-reads",
        ),
    },
    "agent_jobs/claim_reconcile.py": {
        "_fail_spent_start_budgets": (
            "f4c2e990417a6f824df4dc14c5c72936d2b986c5a8d825696ad4186683e55f38",
            "HTTP wait: plain claim transaction rolls back; next poll re-observes spent starts",
        ),
    },
    "agent_jobs/observation_receipts.py": {
        "adopt_observation_receipt": (
            "907a98f4c3a0c8a4d01a82845e9a5b99607389ad5308755885e1e33c46cffe97",
            "HTTP/background wait: middleware or per-candidate order sweep classifies conflicts; rollback retains receipt for next pass",
        ),
    },
    "recipe_operations/result_consumption.py": {
        "consume_agent_result": (
            "4650161a7924cd9401743cf8a6bf56aa3f75b0430b658eae841bb1a4d193d24b",
            "caller-owned: queue claim/result and background order-sweep transactions fence child effects; HTTP disposition or per-candidate sweep waits and re-observes",
        ),
    },
    "recipe_routes/candidate.py": {
        "_candidate_once": (
            "bf8ea87e8746ea4fa6e76f6cdf992b1c3507f5b5db6b900a0d2bbbfe26260026",
            "caller-owned: lock=True bumps every candidate; publication transition fences it; reduce churn in follow-up",
        ),
    },
    "recipe_routes/service.py": {
        "_claim_run_publication_once": (
            "c6c3431315aedc64bc88fbc07f3c332a9bf775743493fe0c40dac61117655285",
            "callback: publication transition owns claim and run writes",
        ),
        "_complete_run_publication": (
            "6c79b6f9a337295b9aba1562098614fc3ce2533542e0b5790f6c6f7fa3291efa",
            "callback: publication transition owns completion",
        ),
        "_fail_run_publication": (
            "0d3a450f677c649d378da90a224ac73485ee272070254e28460383ae8b438afc",
            "callback: publication transition owns recovery failure projection",
        ),
        "_fail_recovery_publication_in_session": (
            "732275ed24b91930b64a3f696f761cac126331fa035f4af943fcd3235aab14dd",
            "callback: publication transition owns recovery completion",
        ),
    },
    "recipe_routes/withdrawal.py": {
        "_withdrawal_publication": (
            "3ccb864a12296eaf8e7e75cf1645583a5e5fa7ff9a7ced896aa81d400d4da550",
            "caller-owned: publication transition binds withdrawal effects",
        ),
        "_record_withdrawal_in_session": (
            "77faeea14defaa438bb4cdebe11617e0e8e573f3698fe75f7929ec85af82f3db",
            "callback: publication transition owns run withdrawal",
        ),
        "_restore_abandoned_stop_withdrawals": (
            "abc38b9f711b1f462bb83c8c11de2d8c3446a73ecae0469283b40655173e0fe8",
            "callback: publication transition reconciles abandoned intent",
        ),
        "_maintenance_step_in_session": (
            "719ada994e350b319dd78f0796e6fbf1c9277cb1d5f9bf9f6ae17452e6ee57e9",
            "callback: publication transition owns maintenance projection",
        ),
        "_withdraw_runs_once.decide.record_reason": (
            "c930dff5ca3030d5dd95e86fc9b152319fd52129fa661bb8015cad3206827ede",
            "callback: publication decision owns failure evidence",
        ),
        "_maintenance_step_in_session.complete": (
            "c28636563e3402c50902415508d8097a2b88f60afd769cf7e4942da9622be471",
            "callback: publication completion transition owns generation projection",
        ),
        "_maintenance_step_in_session.record_reason": (
            "25b02d0beed621f343beaefc414a3e2dbcc8faad3e084dbbd0c3102abc28e86a",
            "callback: publication decision owns failure evidence",
        ),
    },
    "agent_api/ended_runs.py": {
        "reopen_ended_runs_still_running": (
            "3172bbcb268e8b1e5e53cbec4db1badf697f6e9e09e00b0fa66fa8e879b295f1",
            "caller-owned: observations transition fences reopened runs",
        ),
    },
    "distributed_recovery/coordinator.py": {
        "_settle_unreadable_runs": (
            "d075af82573cd171732763a4062b600707f85210d9556391836dea297d0efc33",
            "worker: normal recovery re-observes after a rolled-back conflicting pass",
        ),
    },
    "distributed_recovery/common.py": {
        "_wait_for_plan_change": (
            "34416d1d88abf8e38529142601cb041ac59cc2118c53bdf50381a42b4de8aa2b",
            "caller-owned: recovery persists the current plan dependency",
        ),
        "_settle_unrecoverable": (
            "14bbe9a2c301c58279989308cc1d26c06eaccf613a528c0eb4acf466709d0459",
            "caller-owned: recovery settles exact run evidence",
        ),
        "settle_absent_run_in_session": (
            "c06938429bca8fe10595773c2a3628e6918a818651344a2d4ee92c1f9cd57d17",
            "caller-owned: confirmed-absence release shares its claim transaction",
        ),
        "_advance_recovery_check": (
            "581f174d3fc36d9cda653fc78c01d6ef209feacc281cd02b52a28d7df64bc12b",
            "caller-owned: recovery persists its next observation",
        ),
        "_schedule_recovery_wait": (
            "d18841fc8ae040359e874ec02887fd023541ac1c3151f04923e30e6042aa8a6d",
            "caller-owned: recovery persists its visible wait",
        ),
    },
    "recipe_operations/job_run_stop.py": {
        "_stop_logical_job_run": (
            "33f8fe948db25ddcff4206a5f9602a5555f220cda8c324d70b4e399f4fad93ca",
            "retain: exact logical Stop admission owns run settlement",
        ),
    },
    "recipe_operations/stop_dispatch.py": {
        "_dispatch_stop_after_withdrawal": (
            "e642022cb930ec565b9fe968700fa6399b08a835416aedc0c1e38303da1e9150",
            "caller-owned: accepted Stop continuation binds exact dispatch",
        ),
        "queue_recovery_stop_in_session": (
            "2c269907f69fb51b3f1571591616271befbf501a8f154dc2991067dde028a195",
            "caller-owned: recovery dispatch fences child-only effects",
        ),
    },
    "recipe_operations/job_activation.py": {
        "_activate_job_run_once": (
            "ed0afa8b088e40eadce7d0eb46b1b8b63a19ea30fd9c708a5e09f4cdf2512a2f",
            "retain: activation admission binds accepted run intent",
        ),
    },
    "recipe_operations/start.py": {
        "_start_once": (
            "23a7b52f4272c6ca8dffb9ef65011dec7bfedc29900d155705f26c881e1a4594",
            "retain: Start admission binds run and capacity",
        ),
    },
    "recipe_operations/projection.py": {
        "_project_node_result": (
            "89645c096523f986a563ddb04e33910f87c9175b6e46385d7a7d1fe179e7b85b",
            "caller-owned: queue result transaction owns rank and run projection",
        ),
    },
    "recipe_operations/terminal_projection.py": {
        "_project_terminal_result": (
            "381aab92244881ed24e907acca256e794897dd9442bdcdd3228a65b36385efe1",
            "caller-owned: queue result transaction owns terminal projection",
        ),
    },
    "recipe_operations/offline_stop.py": {
        "_finish_offline_stop": (
            "21ddcb729c738342d18dd6bd039fe2cb08c21ed7a647aa0b1dea53a46795f093",
            "retain: confirmed offline Stop releases only its exact run",
        ),
    },
}


def writer_name(
    node: ast.FunctionDef | ast.AsyncFunctionDef, parents: dict[ast.AST, ast.AST]
) -> str:
    names = [node.name]
    parent = parents.get(node)
    while parent is not None:
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(parent.name)
        parent = parents.get(parent)
    return ".".join(reversed(names))


def classified_write_lines(path: str, source: str) -> set[int]:
    """An unchanged reviewed AST is classified; new writes cannot inherit it.

    Function ASTs tolerate formatting/movement, but any changed helper needs
    an explicit verdict, even in an already listed file and function.
    """
    from .optimistic_boundaries import scan_unwrapped_recipe_run_writes

    verdicts = RECIPE_RUN_WRITE_VERDICTS.get(
        path.removeprefix("control/src/vonk_control/"), {}
    )
    if not verdicts:
        return set()
    tree = ast.parse(source)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    classified = {
        line
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (name := writer_name(node, parents)) in verdicts
        and hashlib.sha256(ast.dump(node).encode()).hexdigest() == verdicts[name][0]
        for line in range(node.lineno, (node.end_lineno or node.lineno) + 1)
    }
    return classified.intersection(scan_unwrapped_recipe_run_writes(source))
