"""Offline shell completion derived from the installed argparse tree."""

from __future__ import annotations

import argparse
import shlex


def completion_script(parser: argparse.ArgumentParser, shell: str) -> str:
    transitions: list[str] = []
    contexts: list[str] = []
    choices: list[str] = []

    def visit(current: argparse.ArgumentParser, path: str) -> None:
        candidates: list[str] = []
        for action in current._actions:
            candidates.extend(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                for name, child in action.choices.items():
                    candidates.append(name)
                    child_path = f"{path} {name}".strip()
                    transitions.append(
                        f"      {shlex.quote(path + '|' + name)}) "
                        f"context={shlex.quote(child_path)} ;;"
                    )
                    visit(child, child_path)
            elif action.option_strings and action.nargs != 0:
                for option in action.option_strings:
                    key = shlex.quote(path + "|" + option)
                    transitions.append(f"      {key}) skip=1 ;;")
                    values = " ".join(map(str, action.choices or ()))
                    choices.append(f"    {key}) options={shlex.quote(values)} ;;")
            elif not action.option_strings and action.choices is not None:
                candidates.extend(map(str, action.choices))
        contexts.append(
            f"    {shlex.quote(path)}) options={shlex.quote(' '.join(candidates))} ;;"
        )

    visit(parser, "")
    if shell == "bash":
        preamble = """_vonkctl() {
  local context='' word options='' skip=0 i
  local cur="${COMP_WORDS[COMP_CWORD]}"
  for ((i=1; i<COMP_CWORD; i++)); do
    word="${COMP_WORDS[i]}"
"""
        previous = "${COMP_WORDS[COMP_CWORD-1]}"
        finish = """  COMPREPLY=()
  while IFS= read -r word; do
    COMPREPLY+=("$word")
  done < <(compgen -W "$options" -- "$cur")
}
complete -F _vonkctl vonkctl
"""
    elif shell == "zsh":
        preamble = """#compdef vonkctl
_vonkctl() {
  local context='' word options='' skip=0 i
  for ((i=2; i<CURRENT; i++)); do
    word="${words[i]}"
"""
        previous = "${words[CURRENT-1]}"
        finish = """  [[ -n "$options" ]] && compadd -- ${(z)options}
}
compdef _vonkctl vonkctl
"""
    else:
        raise ValueError("completion requires bash or zsh")
    return (
        preamble
        + "    if ((skip)); then skip=0; continue; fi\n"
        + '    case "$context|$word" in\n'
        + "\n".join(transitions)
        + "\n    esac\n  done\n"
        + '  case "$context" in\n'
        + "\n".join(contexts)
        + "\n  esac\n"
        + f'  case "$context|{previous}" in\n'
        + "\n".join(choices)
        + "\n  esac\n"
        + finish
    )
