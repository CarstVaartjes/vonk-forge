"""Reviewed caller-owned RecipeRun writes; additions must receive a new verdict."""

from __future__ import annotations

import ast
import hashlib

from .optimistic_boundaries import scan_unwrapped_recipe_run_writes

# Caller-owned sessions are not automatically transition decisions. These
# verdicts classify writes/callbacks, not permission to add another occurrence.
RECIPE_RUN_WRITE_VERDICTS: dict[str, dict[str, tuple[str, str]]] = {
    "agent_jobs/claim_authority.py": {
        "_reconcile_agent_upgrade": (
            "97cce81852aeb70267943985e94ec504b170fd2f0501b12e892fa35595d091da",
            "HTTP wait: result consumer can lose a run fence; middleware disposition owns 503",
        ),
        "_reject_recipe_build_claim": (
            "5ed08309861b65cefad7a904dedcfe2386e165af8608ae85792a4d5182f1c2c2",
            "HTTP wait: claim result projection rolls back and next claim re-reads",
        ),
    },
    "agent_jobs/claim_reconcile.py": {
        "_fail_spent_start_budgets": (
            "75983ecbf3a527c421d9e195fa54df21966d8cd1f7a262b7edcb50d5e62476b2",
            "HTTP wait: plain claim transaction rolls back; next poll re-observes spent starts",
        ),
    },
    "agent_jobs/observation_receipts.py": {
        "adopt_observation_receipt": (
            "156cd72edacfa71b2379a363f61f7c0d3ac74ae7706d6a5f707e8e6e7d9d5522",
            "HTTP/background wait: middleware or per-candidate order sweep classifies conflicts; rollback retains receipt for next pass",
        ),
    },
    "recipe_operations/result_consumption.py": {
        "consume_agent_result": (
            "83e263f109216caa958091445e6cc00c959e6da17480790e196bddbd788e805a",
            "caller-owned: queue claim/result and background order-sweep transactions fence child effects; HTTP disposition or per-candidate sweep waits and re-observes",
        ),
    },
    "recipe_routes/candidate.py": {
        "_candidate_once": (
            "8c01b35d06068a88aaaef07138526448de2318ebaacc8ad70f066a0422e1c955",
            "caller-owned: lock=True bumps every candidate; publication transition fences it; reduce churn in follow-up",
        ),
    },
    "recipe_routes/service.py": {
        "_claim_run_publication_once": (
            "53bd46eb8959cd23d6fcc4000f0676c83776ac101bc8f93781a3aee7d0bff3a8",
            "callback: publication transition owns claim and run writes",
        ),
        "_complete_run_publication": (
            "dccccd0ec5a28e6ddc8cbc1931b1a41173a597d647971f4eee2edcd5179fa871",
            "callback: publication transition owns completion",
        ),
        "_fail_run_publication": (
            "8891db77c92869af619ef61853766a93c395ca56589f8c737a996306ff3e3edb",
            "callback: publication transition owns recovery failure projection",
        ),
        "_fail_recovery_publication_in_session": (
            "afa49de70990cfca07b8569135621e20adc7ec45d90e64f72bf272276657d49c",
            "callback: publication transition owns recovery completion",
        ),
    },
    "recipe_routes/withdrawal.py": {
        "_withdrawal_publication": (
            "b01e504e29b88fd5f9aeef1735e552f38fd467227402add1b1a4f9c21b7bb44a",
            "caller-owned: publication transition binds withdrawal effects",
        ),
        "_record_withdrawal_in_session": (
            "a191decc433ca034413d71e3c30208b41cb80cd916611c20ecbec7621dc9b873",
            "callback: publication transition owns run withdrawal",
        ),
        "_restore_abandoned_stop_withdrawals": (
            "98315cc8bf35abca43f52213a2625e240590f7d26bd30c97ac94880d9673ab86",
            "callback: publication transition reconciles abandoned intent",
        ),
        "_maintenance_step_in_session": (
            "4f795301236cbadd0dcad4c67d5e8edede7c43c8909613e2f1b274b98ff2dc18",
            "callback: publication transition owns maintenance projection",
        ),
        "_withdraw_runs_once.decide.record_reason": (
            "f244b7a61513aa8410aedc729e4f827b279568f597b414aac9bda9a89fa626a3",
            "callback: publication decision owns failure evidence",
        ),
        "_maintenance_step_in_session.complete": (
            "2ee83313992cf5dc656126fc8b26b0e317ffb847c86813a64bdb4d7652d119fe",
            "callback: publication completion transition owns generation projection",
        ),
        "_maintenance_step_in_session.record_reason": (
            "5e6c43e7f9f89a14fa956b855dd66a34e61efe8608dac4050abe3d14c59ae241",
            "callback: publication decision owns failure evidence",
        ),
    },
    "agent_api/ended_runs.py": {
        "reopen_ended_runs_still_running": (
            "0aa23b7bfaa6ece8b040b025714b2e5bfb308e19df5fc0029aca0b56587f59cc",
            "caller-owned: observations transition fences reopened runs",
        ),
    },
    "distributed_recovery/coordinator.py": {
        "_settle_unreadable_runs": (
            "ba77029e38924ed4234e5620066bce0f0e7cbe07bed636afc5b5552293844631",
            "worker: normal recovery re-observes after a rolled-back conflicting pass",
        ),
    },
    "distributed_recovery/common.py": {
        "_wait_for_plan_change": (
            "ce63a486057c2141e88207965916a3af8d3f828a9e2079227ec7b492da33f063",
            "caller-owned: recovery persists the current plan dependency",
        ),
        "_settle_unrecoverable": (
            "cfcdfd8559f6443242d7fc4d198c939bac329a5748154ee33df879c4d02a49a7",
            "caller-owned: recovery settles exact run evidence",
        ),
        "settle_absent_run_in_session": (
            "ad9cfde0ae84e6ea08bbe4d565ef5ed3fb3c32ba0b9e0915f2012b6205d39457",
            "caller-owned: confirmed-absence release shares its claim transaction",
        ),
        "_advance_recovery_check": (
            "cb94db8468e4b440d82d6b8bb27323cd8443b1d6c35d432b908b81084d0b5874",
            "caller-owned: recovery persists its next observation",
        ),
        "_schedule_recovery_wait": (
            "63c701a67ba3270819b875bea06b3e9b83370026fc130b2f349aa7057e8674bf",
            "caller-owned: recovery persists its visible wait",
        ),
    },
    "recipe_operations/job_run_stop.py": {
        "_stop_logical_job_run": (
            "2aa099c7af15bb489f95019da7ceb14b5f040b18a9d5fc58e319d5c90efea3f0",
            "retain: exact logical Stop admission owns run settlement",
        ),
    },
    "recipe_operations/stop_dispatch.py": {
        "_dispatch_stop_after_withdrawal": (
            "4f6bb90798f2de51080dcfe1416e33116dd18ec14f1f6ee7c4392939e6d572dc",
            "caller-owned: accepted Stop continuation binds exact dispatch",
        ),
        "queue_recovery_stop_in_session": (
            "f15bae176f776d1f39d953db142216b11aa8d4cc51b89a0abf528db4527ed0a7",
            "caller-owned: recovery dispatch fences child-only effects",
        ),
    },
    "recipe_operations/job_activation.py": {
        "_activate_job_run_once": (
            "4fc34de991282de6212c0ae52c27892a11605785c3171312b8b141286913abfe",
            "retain: activation admission binds accepted run intent",
        ),
    },
    "recipe_operations/start.py": {
        "_start_once": (
            "61b8299105bc9bb845446c8f54dc45f6b513c3642ba945a82e4cb0ac009c39f8",
            "retain: Start admission binds run and capacity",
        ),
    },
    "recipe_operations/projection.py": {
        "_project_node_result": (
            "3bb1b4ad857be8b18546e6f3d1fd3b3538e7db13fdfb8755d388a111f3b5229b",
            "caller-owned: queue result transaction owns rank and run projection",
        ),
    },
    "recipe_operations/terminal_projection.py": {
        "_project_terminal_result": (
            "0b78e6a7c0b4bfd01ba3549c72ebf703e35e24aa9de335b494d0db0dad8519d3",
            "caller-owned: queue result transaction owns terminal projection",
        ),
    },
    "recipe_operations/run_transitions.py": {
        "withdraw_for_cleanup": (
            "cb43d6140a0e7bef30b8294df87b29dda52924634ae1c7725334f058c4503d9e",
            "caller-owned: the head of end_unfinished_run; the failed-start terminal projection (queue result transaction) writes it alone and the Stop is queued after commit, with the run reaper as backstop",
        ),
        "end_unfinished_run": (
            "1220c14a96c97603f7645897d76022ffac436a3905f98649c0403b79209ef404",
            "caller-owned: the failed-start terminal projection (queue result transaction) and the recovery tick's run reaper (optimistic publication transaction) own the cleanup Stop or the lost verdict",
        ),
    },
    "runs/route_name.py": {
        "rename_run": (
            "90eb2b95c32040ba87484976f33211e69b0a513401f5dddc36702802a7856f70",
            "caller-owned: the profile admission transaction owns the rename; the next route publication pass fences the republish",
        ),
    },
    "recipe_operations/offline_stop.py": {
        "_finish_offline_stop": (
            "1b1c243c730f87115d8ea2a5f1fb4f24be1206cc9a883b7a0be389eb8bbbccc0",
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
    ``ast.dump`` differs between interpreters (3.15 omits ``ctx=Load()``), so
    the hashes are recorded under the `.python-version` interpreter.
    """

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
