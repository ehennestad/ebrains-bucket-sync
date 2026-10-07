from ebrains_bucket_sync.exclude import exclude_files, pattern_to_regex

from .conftest import entry


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
