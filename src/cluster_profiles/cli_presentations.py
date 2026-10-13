"""Registered presentations for every Controller command response."""

from .cli_render import render_payload

COMMAND_ACTIONS = {
    "platform": (None,),
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
        "job",
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
        "load",
        "cancel",
        "progress",
        "endpoint",
    ),
    "key": (None, "create", "list", "roll", "revoke"),
    "run": (None,),
}
#: ``remove --review`` rewrites the action after parsing; it renders the review.
REVIEW_NOUNS = ("model", "recipe")
PRESENTATIONS = {
    (noun, action): render_payload
    for noun, actions in COMMAND_ACTIONS.items()
    for action in actions
} | {(noun, "preview"): render_payload for noun in REVIEW_NOUNS}
