"""Text for messages to people: sizes and counts."""

from __future__ import annotations


def format_size(n_bytes: float) -> str:
    """A size such as "1.5 MB", in units of 1024."""
    value = float(n_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"


def count_text(count: int, noun: str) -> str:
    """A count with its noun, in the plural unless the count is one: "1 file", "3 files"."""
    return f"1 {noun}" if count == 1 else f"{count} {noun}s"
