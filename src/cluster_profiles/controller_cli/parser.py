"""Current Controller command tree and argument declarations."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..cli_artifact_jobs import (
    add_artifact_job_commands,
)
from .common import (
    MAX_LOG_LINES,
    _action_flags,
    _activity_cursor,
    _activity_limit,
    _activity_state,
    _activity_target,
    _add_output,
    _add_profile_selection,
    _add_yes,
    _filters,
    _line_limit,
    _option_flag,
    _profile_edit_flags,
    _selection_controls,
    _selector,
    _uuid_argument,
    _uuid_selector,
    _value_list,
    _watch_controls,
)


def add_controller_commands[ControllerParserT: argparse.ArgumentParser](
    commands: argparse._SubParsersAction[ControllerParserT],
) -> None:
    """Register the Fleet, Model, Recipe, Profile and Key command namespaces."""
    platform = commands.add_parser(
        "platform", help="Actual Controller processes and installed contracts"
    )
    _add_output(platform)
    fleet = commands.add_parser(
        "fleet",
        help="Sparks, their health, and what they run",
        description="Enrolled Sparks: their health, memory, and the workloads "
        "they run.",
        epilog="Without a command, shows the fleet overview.",
    )
    _add_output(fleet)
    fleet_actions = fleet.add_subparsers(dest="fleet_action", parser_class=type(fleet))

    detail = fleet_actions.add_parser("detail", help="Show one Spark")
    _selector(detail, "spark", help="Exact Spark selector or friendly name")
    detail.add_argument(
        "--watch", action="store_true", help="Keep refreshing until it settles"
    )
    _watch_controls(detail)
    detail.add_argument(
        "--technical", action="store_true", help="Include the canonical definition"
    )
    _add_output(detail)
    rename = fleet_actions.add_parser("rename", help="Change a Spark's friendly name")
    _selector(rename, "spark", help="Exact Spark selector or friendly name")
    rename.add_argument("new_name")
    _action_flags(rename)
    enroll = fleet_actions.add_parser(
        "enroll", help="Create a one-time enrollment grant"
    )
    enroll.set_defaults(outcome_context="mutation")
    _add_yes(enroll)
    enroll.add_argument("name")
    enroll.add_argument("--output", type=Path, required=True, metavar="FILE")
    enroll.add_argument("--request-key", type=_uuid_argument)
    _add_output(enroll)
    for action_name, help_text in (
        ("re-enroll", "Replace a Spark certificate"),
        ("remove", "Revoke and remove a Spark"),
    ):
        action = fleet_actions.add_parser(action_name, help=help_text)
        action.set_defaults(outcome_context="mutation")
        _selector(action, "spark", help="Exact Spark selector or friendly name")
        _add_output(action)
        if action_name == "remove":
            action.add_argument(
                "--yes", action="store_true", help="Confirm without asking"
            )
        else:
            action.add_argument("--output", type=Path, required=True, metavar="FILE")
            action.add_argument("--request-key", type=_uuid_argument)
            action.add_argument(
                "--yes", action="store_true", help="Confirm without asking"
            )
    enrollment = fleet_actions.add_parser(
        "enrollment",
        help="Inspect or revoke an enrollment grant",
        description="One-time grants that let a new Spark enroll.",
    )
    enrollment_actions = enrollment.add_subparsers(
        dest="enrollment_action", parser_class=type(fleet)
    )
    for action_name, action_help in (
        ("status", "Show whether a grant is pending, used, expired, or revoked"),
        ("revoke", "Revoke an unused grant"),
    ):
        action = enrollment_actions.add_parser(action_name, help=action_help)
        action.add_argument("grant_id", type=_uuid_argument)
        _add_output(action)
        if action_name == "revoke":
            action.set_defaults(outcome_context="mutation")
            action.add_argument(
                "--yes", action="store_true", help="Confirm without asking"
            )
    upgrade = fleet_actions.add_parser(
        "upgrade", help="Upgrade the Spark agent, one Spark at a time"
    )
    upgrade.set_defaults(outcome_context="mutation")
    upgrade.add_argument("selector", nargs="?", metavar="SPARK")
    upgrade.add_argument("--all", action="store_true")
    upgrade.add_argument("--request-key", type=_uuid_argument)
    upgrade.add_argument(
        "--detach",
        action="store_true",
        help="Return once accepted instead of following",
    )
    upgrade.add_argument("--yes", action="store_true", help="Confirm without asking")
    _watch_controls(upgrade)
    _add_output(upgrade)
    loginfo = fleet_actions.add_parser("loginfo", help="Recent logs of one Spark")
    _selector(loginfo, "spark", help="Exact Spark selector or friendly name")
    loginfo.add_argument("--since", default="15m")
    loginfo.add_argument(
        "--lines",
        type=_line_limit,
        default=100,
        metavar="1-1000",
        help=f"Tail lines, 1 to {MAX_LOG_LINES} (default: 100)",
    )
    loginfo.add_argument("--recipe")
    loginfo.add_argument("--source", choices=("client", "monitor", "runtime", "job"))
    loginfo.add_argument(
        "--follow", action="store_true", help="Keep following until it finishes"
    )
    _watch_controls(loginfo)
    _add_output(loginfo)

    fleet_progress = fleet_actions.add_parser(
        "progress", help="Show or follow one fleet job"
    )
    fleet_progress.add_argument("job_id")
    fleet_progress.add_argument(
        "--follow", action="store_true", help="Keep following until it finishes"
    )
    _watch_controls(fleet_progress)
    _add_output(fleet_progress)

    activity = fleet_actions.add_parser(
        "activity", help="Recent operations and their state"
    )
    activity.add_argument("--limit", type=_activity_limit, default=20)
    activity.add_argument("--cursor", type=_activity_cursor)
    activity.add_argument("--state", type=_activity_state)
    activity.add_argument("--target", type=_activity_target)
    activity.add_argument("--request-id", type=_uuid_argument)
    _add_output(activity)

    locks = fleet_actions.add_parser(
        "locks",
        help="Admission locks held now and the transactions that hold them",
    )
    _add_output(locks)

    evidence = fleet_actions.add_parser(
        "evidence", help="Download the diagnostics of one failed operation attempt"
    )
    evidence.add_argument("operation_id")
    evidence.add_argument(
        "--attempt",
        type=int,
        metavar="N",
        help="Attempt number (default: the operation's current attempt)",
    )
    evidence.add_argument("--output", type=Path, required=True, metavar="FILE")
    _add_output(evidence)

    resume = fleet_actions.add_parser(
        "resume", help="Resume a job that waits for an operator"
    )
    resume.set_defaults(outcome_context="mutation")
    resume.add_argument("job_id")
    resume.add_argument("--yes", action="store_true", required=True)
    _add_output(resume)

    model = commands.add_parser(
        "model",
        help="Model library and the NAS model cache",
        description="Published models and the copies cached on the NAS.",
        epilog="Without a command, lists cached models.",
    )
    _add_output(model)
    model_actions = model.add_subparsers(dest="model_action", parser_class=type(model))
    model_library = model_actions.add_parser(
        "library", help="List published model variants"
    )
    _filters(model_library)
    _add_output(model_library)
    model_detail = model_actions.add_parser(
        "detail", help="Show an exact model variant"
    )
    _selector(model_detail, "model", help="Exact model selector or friendly name")
    model_detail.add_argument(
        "--watch", action="store_true", help="Keep refreshing until it settles"
    )
    _watch_controls(model_detail)
    model_detail.add_argument(
        "--technical", action="store_true", help="Include the canonical definition"
    )
    _add_output(model_detail)
    model_download = model_actions.add_parser("download", help="Cache a model variant")
    _selector(model_download, "model", help="Exact model selector or friendly name")
    _action_flags(model_download, followable=True)
    model_remove = model_actions.add_parser(
        "remove", help="Review and remove Controller model cache assets"
    )
    _selector(model_remove, "model", help="Exact model selector or friendly name")
    _action_flags(model_remove, followable=True)
    model_remove.add_argument(
        "--review",
        action="store_true",
        help="Show the Controller-owned removal impact without submitting it",
    )
    model_cancel = model_actions.add_parser(
        "cancel", help="Cancel one model download while preserving resumable files"
    )
    model_cancel.add_argument("operation_id", type=_uuid_selector)
    _action_flags(model_cancel, followable=True)
    model_cancel.add_argument(
        "--reason",
        default="operator requested cancellation",
        help="Short reason retained with the durable cancellation request",
    )

    recipe = commands.add_parser(
        "recipe",
        help="Runnable recipes, their cache, and their outputs",
        description="Recipes say how to run a model on one or more Sparks.",
        epilog="Without a command, lists cached recipes.",
    )
    _add_output(recipe)
    recipe_actions = recipe.add_subparsers(
        dest="recipe_action", parser_class=type(recipe)
    )
    recipe_library = recipe_actions.add_parser(
        "library", help="List compatible recipes"
    )
    _filters(recipe_library)
    _value_list(recipe_library, "--model", "model", "Only recipes serving this model")
    recipe_library.add_argument(
        "--ready",
        action="store_true",
        default=None,
        help="Require exact usable NAS assets and an admissible placement",
    )
    recipe_library.add_argument(
        "--fits-fleet",
        action="store_true",
        default=None,
        help="Require a placement fitting fresh current fleet capacity",
    )
    _value_list(
        recipe_library,
        "--engine",
        "engine",
        "Runtime engine, such as vllm or sglang",
    )
    _value_list(
        recipe_library,
        "--creator",
        "creator",
        "Upstream creator of the recipe, such as MiaAI-Lab",
    )
    _value_list(
        recipe_library,
        "--sparks",
        "count",
        "Exact topology node count",
        item=int,
    )
    _add_output(recipe_library)
    recipe_sync_status = recipe_actions.add_parser(
        "sync-status", help="Show when the recipe catalog last synced and what it found"
    )
    _add_output(recipe_sync_status)
    recipe_detail = recipe_actions.add_parser("detail", help="Show an exact recipe")
    _selector(recipe_detail, "recipe", help="Exact recipe selector or friendly name")
    recipe_detail.add_argument(
        "--watch", action="store_true", help="Keep refreshing until it settles"
    )
    _watch_controls(recipe_detail)
    recipe_detail.add_argument(
        "--technical", action="store_true", help="Include the canonical definition"
    )
    _add_output(recipe_detail)
    recipe_download = recipe_actions.add_parser(
        "download", help="Cache a recipe and missing model"
    )
    _selector(recipe_download, "recipe", help="Exact recipe selector or friendly name")
    _action_flags(recipe_download, followable=True)
    recipe_update = recipe_actions.add_parser(
        "update", help="Refresh an exact recipe or all currently cached recipes"
    )
    recipe_update.add_argument("selector", nargs="?", metavar="RECIPE")
    recipe_update.add_argument("--all", action="store_true")
    _action_flags(recipe_update, followable=True)
    recipe_remove = recipe_actions.add_parser(
        "remove", help="Review and remove Controller recipe cache assets"
    )
    _selector(recipe_remove, "recipe", help="Exact recipe selector or friendly name")
    _action_flags(recipe_remove, recipe_remove=True, followable=True)
    recipe_remove.add_argument(
        "--review",
        action="store_true",
        help="Show the Controller-owned removal impact without submitting it",
    )
    recipe_retry = recipe_actions.add_parser(
        "retry", help="Submit preparation using the original frozen recipe intent"
    )
    recipe_retry.add_argument("operation_id", type=_uuid_argument)
    _action_flags(recipe_retry, followable=True)
    recipe_cancel = recipe_actions.add_parser(
        "cancel", help="Cancel one accepted recipe preparation"
    )
    recipe_cancel.add_argument("operation_id")
    _action_flags(recipe_cancel, followable=True)
    recipe_cancel.add_argument("--reason", default="operator requested cancellation")

    recipe_installation = recipe_actions.add_parser(
        "installation",
        help="Inspect and reconcile one installed recipe identity",
        description="Repair an installed recipe that no longer matches its record.",
    )
    installation_actions = recipe_installation.add_subparsers(
        dest="installation_action", parser_class=type(recipe)
    )
    installation_reconcile = installation_actions.add_parser(
        "reconcile",
        help="Review or reconcile one invalid stopped installation",
    )
    installation_reconcile.add_argument(
        "installation_id", type=_uuid_argument, help="Exact installation UUID"
    )
    _action_flags(installation_reconcile, followable=True)
    installation_reconcile.add_argument(
        "--review",
        action="store_true",
        help="Show the exact reconciliation plan without submitting it",
    )

    add_artifact_job_commands(
        recipe_actions, add_output=_add_output, watch_controls=_watch_controls
    )

    for noun, actions in (("model", model_actions), ("recipe", recipe_actions)):
        progress = actions.add_parser(
            "progress", help=f"Inspect or follow one {noun} operation"
        )
        target = progress.add_mutually_exclusive_group(required=True)
        target.add_argument("operation_id", nargs="?")
        target.add_argument(
            "--request-key",
            help="Original request UUID; resolves once before following",
        )
        progress.add_argument(
            "--follow", action="store_true", help="Keep following until it finishes"
        )
        _watch_controls(progress)
        _add_output(progress)

    profile = commands.add_parser(
        "profile",
        help="Whole-fleet profiles: edit, load, and endpoints",
        description="A profile is the saved set of recipes the whole fleet "
        "should run. Commands act on profile 1 unless you pass "
        "--profile N, before or after the command.",
        epilog="Without a command, shows the selected profile.",
    )
    _add_output(profile)
    profile_actions = profile.add_subparsers(
        dest="profile_action", parser_class=type(profile)
    )
    profile_list = profile_actions.add_parser(
        "list", help="List stable numbered profiles"
    )
    _add_output(profile_list)
    profile_add = profile_actions.add_parser(
        "add", help="Assign a recipe to Sparks and autosave"
    )
    profile_add.add_argument("recipe_selector", metavar="RECIPE")
    profile_add.add_argument(
        "--spark", action="append", required=True, help="Spark to assign"
    )
    profile_add.add_argument(
        "--as", dest="assignment_name", metavar="NAME", help="Name in the profile"
    )
    profile_add.add_argument("--model-variant", help="Exact model variant to serve")
    profile_add.add_argument(
        "--state", dest="desired_state", choices=("installed", "running")
    )
    _option_flag(profile_add)
    _profile_edit_flags(profile_add)
    _selection_controls(profile_add)
    profile_remove = profile_actions.add_parser(
        "remove", help="Remove an assignment or Spark members"
    )
    profile_remove.add_argument("assignment")
    profile_remove.add_argument("--spark", action="append", default=[])
    _profile_edit_flags(profile_remove)
    _selection_controls(profile_remove)
    profile_configure = profile_actions.add_parser(
        "configure", help="Edit saved metadata without loading"
    )
    profile_configure.add_argument("--name")
    profile_configure.add_argument("--description")
    profile_configure.add_argument("--retention", choices=("keep-cached", "exact"))
    profile_configure.add_argument("--favorite", choices=("true", "false"))
    profile_configure.add_argument(
        "--label", action="append", default=[], metavar="KEY=VALUE"
    )
    profile_configure.add_argument(
        "--remove-label", action="append", default=[], metavar="KEY"
    )
    profile_configure.add_argument(
        "--assignment",
        metavar="NAME",
        help="Assignment whose --option choices to change (default: the only one)",
    )
    _option_flag(profile_configure)
    _profile_edit_flags(profile_configure)
    profile_export = profile_actions.add_parser(
        "export", help="Export the canonical saved definition as JSON"
    )
    profile_export.add_argument("--output", type=Path)
    _add_output(profile_export)
    profile_import = profile_actions.add_parser(
        "import", help="Save a definition; never load it"
    )
    profile_import.add_argument(
        "--file", required=True, help="JSON file or - for stdin"
    )
    _profile_edit_flags(profile_import, require_revision=True)
    profile_load = profile_actions.add_parser(
        "load", help="Apply the entire profile to the fleet"
    )
    profile_load.set_defaults(outcome_context="mutation", requires_profile=True)
    profile_load.add_argument(
        "--review",
        dest="dry_run",
        action="store_true",
        help="Review the plan without applying it",
    )
    profile_load.add_argument(
        "--yes",
        action="store_true",
        help="Load the plan that is current when accepted, without a review",
    )
    profile_load.add_argument("--request-key")
    profile_load.add_argument(
        "--detach",
        action="store_true",
        help="Return once accepted instead of following",
    )
    _watch_controls(profile_load)
    _add_output(profile_load)
    profile_cancel = profile_actions.add_parser(
        "cancel", help="Cancel one profile application and reconcile its effects"
    )
    profile_cancel.set_defaults(outcome_context="mutation", requires_profile=True)
    profile_cancel.add_argument("application_id", type=_uuid_selector)
    profile_cancel.add_argument(
        "--yes", action="store_true", help="Confirm without asking"
    )
    profile_cancel.add_argument("--request-key", type=_uuid_selector)
    profile_cancel.add_argument(
        "--detach",
        action="store_true",
        help="Return once accepted instead of following",
    )
    _watch_controls(profile_cancel)
    _add_output(profile_cancel)
    profile_progress = profile_actions.add_parser(
        "progress", help="Show the latest profile load"
    )
    profile_progress.add_argument(
        "--follow", action="store_true", help="Keep following until it finishes"
    )
    profile_progress_selectors = profile_progress.add_mutually_exclusive_group()
    profile_progress_selectors.add_argument("--application", type=_uuid_selector)
    profile_progress_selectors.add_argument("--request-key", type=_uuid_selector)
    _watch_controls(profile_progress)
    _add_output(profile_progress)
    profile_endpoint = profile_actions.add_parser(
        "endpoint",
        help="API base, model names, and client setup of the loaded profile",
    )
    profile_endpoint.add_argument(
        "alias", nargs="?", help="Only show this endpoint alias from the profile"
    )
    _add_output(profile_endpoint)
    # `--profile N` selects the profile for every profile command and `run`.
    # It is accepted before the command (`vonkctl --profile 2 profile load`)
    # and after it (`vonkctl profile load --profile 2`).
    for selecting in (profile, *profile_actions.choices.values()):
        _add_profile_selection(selecting)

    key = commands.add_parser(
        "key",
        help="Client keys for the inference gateway",
        description="Keys that let apps call the models the fleet serves.",
        epilog="Without a command, lists the keys.",
    )
    _add_output(key)
    key_actions = key.add_subparsers(dest="key_action", parser_class=type(key))
    key_create = key_actions.add_parser(
        "create", help="Create a client key; it is shown only once"
    )
    _add_yes(key_create)
    key_create.add_argument("name")
    _value_list(
        key_create,
        "--model",
        "alias",
        "Restrict the key to these models (default: every model)",
        dest="models",
    )
    key_create.add_argument(
        "--expires", metavar="DURATION", help="Expire after e.g. 30d or 12h"
    )
    key_create.add_argument(
        "--output", type=Path, help="Write the key to this new private file"
    )
    _add_output(key_create)
    key_list = key_actions.add_parser("list", help="List client keys, never secrets")
    _add_output(key_list)
    key_roll = key_actions.add_parser(
        "roll", help="Replace a client key's secret; the new one is shown only once"
    )
    key_roll.add_argument("name")
    key_roll.add_argument("--yes", action="store_true", help="Confirm without asking")
    key_roll.add_argument(
        "--output", type=Path, help="Write the key to this new private file"
    )
    _add_output(key_roll)
    key_revoke = key_actions.add_parser("revoke", help="Revoke a client key by name")
    key_revoke.add_argument("name")
    key_revoke.add_argument("--yes", action="store_true", help="Confirm without asking")
    _add_output(key_revoke)

    run = commands.add_parser(
        "run",
        help="Prepare a recipe, review it, and start it",
        description="Prepare a recipe, review the plan, and start it on the fleet. "
        "The recipe is first saved into the profile as a draft so the plan can be "
        "reviewed; declining leaves that draft and the fleet unchanged.",
    )
    run.set_defaults(outcome_context="mutation")
    run.add_argument(
        "selector", metavar="RECIPE", help="Recipe name or exact model/recipe selector"
    )
    run.add_argument(
        "--spark",
        action="append",
        default=[],
        help="Spark to run on; repeatable (default: every Spark)",
    )
    run.add_argument(
        "--as", dest="assignment_name", metavar="NAME", help="Name in the profile"
    )
    _option_flag(run)
    run.add_argument(
        "--yes",
        action="store_true",
        help="Start the plan that is current when accepted, without asking",
    )
    run.add_argument(
        "--request-key",
        help="Original request UUID; supply and retain it to reconnect after process death",
    )
    _watch_controls(run)
    _add_profile_selection(run)
    _add_output(run)
