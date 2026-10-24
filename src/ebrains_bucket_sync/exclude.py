"""Leave out the files that match exclude patterns.

A pattern is a wildcard pattern, with the meaning it has in a .gitignore
file:
    *   matches any characters except "/"
    ?   matches one character except "/"
    **  between separators matches any number of folders, none included:
        "**/cache" matches "cache" at the root as well as "a/cache",
        "a/**/b" matches "a/b" as well as "a/x/y/b", and "a/**" matches
        everything below "a". Elsewhere "**" matches any characters, "/"
        included.

A pattern without "/" is matched against every part of a path, so "*.tmp"
leaves out such files in every folder and ".git" leaves out a folder of
that name with everything in it. A pattern with "/" inside it or in front
of it is matched against the path from the root of the synced folder, and
also leaves out what is below a folder it matches: "raw/scratch" leaves out
"raw/scratch/a.dat", and "/build" leaves out the "build" folder at the root
but not "sub/build". A trailing "/" is ignored, so ".git/" and "build/"
leave out those folders wherever they are.

An exclude file holds one pattern per line, in the layout of a .gitignore
file. Its syntax is a subset of .gitignore: a line that re-includes paths
("!") is refused rather than skipped, since skipping it would silently
leave out the files the author meant to upload.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path

from .model import FileEntry

_REGEX_SPECIALS = set(".+^$(){}[]|\\")
_LINE_BREAK = re.compile(r"\r\n|\r|\n")


class ExcludeFileError(ValueError):
    """An exclude file has a line that patterns cannot express."""

    def __init__(self, line_number: int, line: str, reason: str) -> None:
        super().__init__(f'Line {line_number} ("{line}"): {reason}')
        self.line_number = line_number
        self.line = line
        self.reason = reason


def pattern_to_regex(
    pattern: str, anchored: bool | None = None, *, ignore_case: bool = False
) -> re.Pattern[str]:
    """Regular expression that a path fully matches when the pattern excludes it.

    pattern has no leading or trailing "/". anchored says whether it is
    matched from the root, which a "/" inside it implies; a leading "/",
    which the caller removed, makes it anchored too. ignore_case makes the
    expression match without regard to case.
    """
    if anchored is None:
        anchored = "/" in pattern

    parts = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i) and (i == 0 or pattern[i - 1] == "/"):
            # "**/" at the start or "/**/" inside: zero or more folders
            parts.append("(.*/)?")
            i += 3
            continue
        if pattern.startswith("/**", i) and i + 3 == len(pattern):
            # "/**" at the end: anything below the folder
            parts.append("(/.*)?")
            i += 3
            continue
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
    return re.compile(expression, re.IGNORECASE if ignore_case else 0)


def exclude_files(
    files: Iterable[FileEntry], patterns: Sequence[str], *, ignore_case: bool = False
) -> list[FileEntry]:
    """The files whose path matches none of the patterns, in the same order.

    ignore_case matches the patterns without regard to case, for a folder
    whose file system ignores the case of names: there a pattern that
    excludes a local file must also exclude an object whose name differs
    from it only in case, or the object would be downloaded over the file.
    """
    expressions = []
    for pattern in patterns:
        trimmed = pattern.strip("/")
        if trimmed:
            anchored = pattern.startswith("/") or "/" in trimmed
            expressions.append(pattern_to_regex(trimmed, anchored, ignore_case=ignore_case))

    return [
        entry
        for entry in files
        if not any(expression.fullmatch(entry.path) for expression in expressions)
    ]


def parse_exclude_file(text: str) -> list[str]:
    """The patterns of an exclude file, in order.

    Lines end with LF, CRLF or CR. A byte order mark at the start of the text
    and spaces and tabs around a line are removed. Blank lines and lines
    that start with "#" are skipped. A line that starts with "!" raises
    ExcludeFileError.
    """
    # Some Windows editors start a file with a byte order mark, which would
    # otherwise become part of the first pattern.
    text = text.removeprefix("\ufeff")
    patterns = []
    for line_number, raw_line in enumerate(_LINE_BREAK.split(text), start=1):
        line = raw_line.strip(" \t")
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            raise ExcludeFileError(
                line_number,
                line,
                'a line that starts with "!" re-includes paths, which exclude patterns '
                "cannot do. Remove the line, or exclude the other paths more precisely.",
            )
        patterns.append(line)
    return patterns


def read_exclude_file(path: str | Path) -> list[str]:
    """The patterns of the UTF-8 exclude file at path. See parse_exclude_file."""
    return parse_exclude_file(Path(path).read_text(encoding="utf-8"))
