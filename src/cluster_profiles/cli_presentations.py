"""Registered presentations for every Controller command response."""

from cluster_profiles.cli_json_contract import Word
from cluster_profiles.cli_render.dispatch import render_payload

COMMAND_ACTIONS = {
    "platform": (None,),
    "metrics": (None, "show"),
    "fleet": (
        None,
        "detail",
        "rename",
        "enroll",
        "re-enroll",
        "remove",
        "enrollment",
        "upgrade",
        "loginfo",
        "progress",
        "activity",
        "locks",
        "evidence",
        "resume",
    ),
    "model": (None, "library", "detail", "download", "remove", "cancel", "progress"),
    "recipe": (
        None,
        "library",
        "sync-status",
        "detail",
        "download",
        "update",
        "remove",
        "retry",
        "cancel",
        "installation",
        "progress",
    ),
    "profile": (
        None,
        "list",
        "add",
        "remove",
        "configure",
        "export",
        "import",
        "delete",
        "load",
        "cancel",
        "progress",
        "endpoint",
    ),
    Word.JOB: (
        None,
        Word.RUN,
        Word.CONTRACT,
        Word.LIST,
        Word.CREATE,
        Word.UPLOAD,
        Word.SUBMIT,
        Word.DETAIL,
        Word.CANCEL,
        Word.DOWNLOAD,
    ),
    "key": (None, "create", "list", "roll", "revoke"),
    "run": (None,),
}
COMMAND_ACTIONS["model"] += ("resume-access",)

#: ``remove --review`` rewrites the action after parsing; it renders the review.
REVIEW_NOUNS = ("model", "recipe")
PRESENTATIONS = {
    (noun, action): render_payload
    for noun, actions in COMMAND_ACTIONS.items()
    for action in actions
} | {(noun, "preview"): render_payload for noun in REVIEW_NOUNS}
