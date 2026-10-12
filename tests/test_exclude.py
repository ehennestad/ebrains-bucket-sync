from pathlib import Path

import pytest

from ebrains_bucket_sync.exclude import (
    ExcludeFileError,
    exclude_files,
    pattern_to_regex,
    read_exclude_file,
)

from .conftest import entry

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "dataset.ignore"


def kept_paths(paths, patterns):
    return [item.path for item in exclude_files([entry(path) for path in paths], patterns)]


def test_matches_names_at_any_depth_and_anchored_paths():
    paths = [
        "a.tmp",
        "sub/b.tmp",
        "sub/b.tmpx",
        ".git/config",
        "src/.git/HEAD",
        "raw/scratch/x.dat",
        "other/raw/scratch/y.dat",
        "keep.txt",
    ]

    kept = kept_paths(paths, ["*.tmp", ".git", "/raw/scratch"])

    assert kept == ["sub/b.tmpx", "other/raw/scratch/y.dat", "keep.txt"]


def test_ignores_trailing_slash_like_gitignore():
    paths = [
        ".git/config",
        "src/.git/HEAD",
        "build/a.o",
        "raw/scratch/x.dat",
        "other/raw/scratch/y.dat",
        "keep.txt",
    ]

    kept = kept_paths(paths, [".git/", "build/", "/raw/scratch/"])

    assert kept == ["other/raw/scratch/y.dat", "keep.txt"]


def test_regular_expression_characters_are_literal():
    kept = kept_paths(["a(1).txt", "a1.txt", "sub/x.y", "sub/xzy"], ["a(?).txt", "sub/x.y"])

    assert kept == ["a1.txt", "sub/xzy"]


def test_double_star_crosses_folders_and_single_star_does_not():
    assert pattern_to_regex("logs/**/*.log").fullmatch("logs/x/y/b.log")
    assert pattern_to_regex("logs/**/*.log").fullmatch("logs/a.log") is None
    assert pattern_to_regex("logs/*.log").fullmatch("logs/x/b.log") is None


def test_empty_and_slash_only_patterns_exclude_nothing():
    assert kept_paths(["a.txt"], ["", "/", "//"]) == ["a.txt"]


def test_reads_the_patterns_of_a_file(tmp_path):
    exclude_file = tmp_path / "dataset.ignore"
    exclude_file.write_bytes("\ufeff# comment\r\n*.tmp\r\n\r\n.git/\r\n".encode())

    assert read_exclude_file(exclude_file) == ["*.tmp", ".git/"]


def test_refuses_a_file_that_re_includes_paths(tmp_path):
    exclude_file = tmp_path / "dataset.ignore"
    exclude_file.write_text("*.txt\n!keep.txt\n")

    with pytest.raises(ExcludeFileError, match='Line 2 \\("!keep.txt"\\)'):
        read_exclude_file(exclude_file)


def test_dataset_template_leaves_out_clutter_and_keeps_data():
    paths = [
        ".git/config",
        ".gitignore",
        "code/.DS_Store",
        "code/._analysis.py",
        "code/__pycache__/analysis.cpython-312.pyc",
        "code/analysis.asv",
        "notes/~$report.docx",
        ".env",
        "code/analysis.py",
        "code/analysis.m",
        "data/session-1.nwb",
        "environment/xenium.yml",
        "README.md",
    ]

    kept = kept_paths(paths, read_exclude_file(TEMPLATE))

    assert kept == [
        "code/analysis.py",
        "code/analysis.m",
        "data/session-1.nwb",
        "environment/xenium.yml",
        "README.md",
    ]
