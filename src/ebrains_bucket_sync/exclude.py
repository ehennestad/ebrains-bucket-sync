"""Leave out the files that match exclude patterns.

A pattern is a wildcard pattern:
    *   matches any characters except "/"
    **  matches any characters, "/" included
    ?   matches one character except "/"

A pattern without "/" is matched against every part of a path, so "*.tmp"
leaves out such files in every folder and ".git" leaves out a folder of
that name with everything in it. A pattern with "/" is matched against the
path from the root of the synced folder, and also leaves out what is below
a folder it matches: "raw/scratch" leaves out "raw/scratch/a.dat". A
leading or trailing "/" is ignored, so ".git/" and "build/" leave out
those folders wherever they are, as in a .gitignore file.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from .model import FileEntry

_REGEX_SPECIALS = set(".+^$(){}[]|\\")


def pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Regular expression that a path fully matches when the pattern excludes it."""
    anchored = "/" in pattern

    parts = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            parts.append(".*")
            i += 2
            continue
        char = pattern[i]
        if char == "*":
            parts.append("[^/]*")
        elif char == "?":
            parts.append("[^/]")
        elif char in _REGEX_SPECIALS:
            parts.append("\\" + char)
        else:
            parts.append(char)
        i += 1
    expression = "".join(parts)

    if anchored:
        expression = expression + "(/.*)?"
    else:
        expression = "(.*/)?" + expression + "(/.*)?"
    return re.compile(expression)


def exclude_files(files: Iterable[FileEntry], patterns: Sequence[str]) -> list[FileEntry]:
    """The files whose path matches none of the patterns, in the same order."""
    expressions = []
    for pattern in patterns:
        trimmed = pattern.strip("/")
        if trimmed:
            expressions.append(pattern_to_regex(trimmed))

    return [
        entry
        for entry in files
        if not any(expression.fullmatch(entry.path) for expression in expressions)
    ]
