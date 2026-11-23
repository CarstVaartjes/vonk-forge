from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path


def added_lines(patch: str) -> dict[str, set[int]]:
    return changed_lines(patch)[0]


def changed_lines(patch: str) -> tuple[dict[str, set[int]], dict[str, list[str]]]:
    """Parse hunks, carrying unchanged relocated blocks without duplicating debt.

    A deleted block may account for one identical added block in the same
    language. Copies and changed lines remain additions. Matching preserves
    context and permits a uniform extraction indentation shift, so a common isolated line cannot hide new work.
    All evidence comes from the supplied patch, never repository history.
    Returns each file's added line numbers and its removed text; a deleted line
    that a relocated block spent is no longer in the removed text.
    """
    files: dict[str, set[int]] = {}
    additions: list[tuple[str, list[tuple[int, str]]]] = []
    deletions: list[tuple[str, list[str]]] = []
    path = ""
    old_path = ""
    line = 0
    in_hunk = False
    added: list[tuple[int, str]] = []
    deleted: list[str] = []

    def finish() -> None:
        if added:
            additions.append((path, added.copy()))
            added.clear()
        if deleted:
            deletions.append((old_path, deleted.copy()))
            deleted.clear()

    for text in patch.splitlines():
        if text.startswith("diff --git "):
            finish()
            in_hunk = False
        elif text.startswith("--- a/") and not in_hunk:
            old_path = text[6:]
        elif text.startswith("+++ b/") and not in_hunk:
            path = text[6:]
            files.setdefault(path, set())
        elif match := re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", text):
            finish()
            line = int(match.group(1))
            in_hunk = True
        elif in_hunk and text.startswith("+"):
            files[path].add(line)
            added.append((line, text[1:]))
            line += 1
        elif in_hunk and text.startswith("-"):
            deleted.append(text[1:])
        elif in_hunk and text.startswith(" "):
            finish()
            line += 1
    finish()
    # Hunk boundaries describe the original layout, not the relocated block.
    # Gather each file's changed text so a split module can spend deleted text
    # across hunks. Consumption still permits only one relocated occurrence.
    added_by_file: dict[str, list[tuple[int, str]]] = {}
    deleted_by_file: dict[str, list[str]] = {}
    for target, lines in additions:
        added_by_file.setdefault(target, []).extend(lines)
    for owner, removed in deletions:
        deleted_by_file.setdefault(owner, []).extend(removed)
    for target, lines in added_by_file.items():
        for owner, removed in deleted_by_file.items():
            if Path(owner).suffix != Path(target).suffix:
                continue
            old_lines = [
                (index, text) for index, text in enumerate(removed) if text.strip()
            ]
            new_lines = [
                (index, text) for index, (_, text) in enumerate(lines) if text.strip()
            ]
            matches = SequenceMatcher(
                a=[text.lstrip() for _, text in old_lines],
                b=[text.lstrip() for _, text in new_lines],
                autojunk=False,
            ).get_matching_blocks()
            shifted_blocks: list[tuple[int, int, int]] = []
            for old, new, size in matches:
                start = 0
                previous_shift = None
                for offset in range(size):
                    left = old_lines[old + offset][1]
                    right = new_lines[new + offset][1]
                    shift = (
                        len(left)
                        - len(left.lstrip())
                        - len(right)
                        + len(right.lstrip())
                    )
                    if previous_shift is not None and shift != previous_shift:
                        shifted_blocks.append(
                            (old + start, new + start, offset - start)
                        )
                        start = offset
                    previous_shift = shift
                shifted_blocks.append((old + start, new + start, size - start))
            for old, new, size in shifted_blocks:
                if size < 2:
                    continue
                old_indices = [index for index, _ in old_lines[old : old + size]]
                new_indices = [index for index, _ in new_lines[new : new + size]]
                # Require a block, including its separating blank line, never
                # an isolated vocabulary word. A uniform indentation shift is
                # expected when extracting a function from a dispatch wrapper.
                shifts = {
                    len(removed[left])
                    - len(removed[left].lstrip())
                    - len(lines[right][1])
                    + len(lines[right][1].lstrip())
                    for left, right in zip(old_indices, new_indices, strict=True)
                }
                if len(shifts) != 1 or any(
                    lines[index][0] not in files[target]
                    for index in range(new_indices[0], new_indices[-1] + 1)
                ):
                    continue
                old_end = old_indices[-1] + 1
                new_end = new_indices[-1] + 1
                while old_end < len(removed) and not removed[old_end].strip():
                    old_end += 1
                while new_end < len(lines) and not lines[new_end][1].strip():
                    new_end += 1
                if min(old_end - old_indices[0], new_end - new_indices[0]) < 3:
                    continue
                new_end = (
                    new_indices[-1]
                    + 1
                    + min(
                        old_end - old_indices[-1] - 1,
                        new_end - new_indices[-1] - 1,
                    )
                )
                for offset in range(new_indices[0], new_end):
                    files[target].discard(lines[offset][0])
                for offset in range(old_indices[0], old_end):
                    # A second copy cannot spend the same deleted occurrence.
                    removed[offset] = f"\0consumed:{offset}"
            # Reordered extracted methods need not preserve the old file's
            # sequence alignment. Spend remaining exact blocks independently.
            for _ in range(len(lines)):
                match = SequenceMatcher(
                    a=removed,
                    b=[
                        text if number in files[target] else f"\0added:{number}"
                        for number, text in lines
                    ],
                    autojunk=False,
                ).find_longest_match()
                if match.size < 3:
                    break
                for offset in range(match.size):
                    files[target].discard(lines[match.b + offset][0])
                    removed[match.a + offset] = f"\0consumed:{match.a + offset}"
    return files, deleted_by_file
