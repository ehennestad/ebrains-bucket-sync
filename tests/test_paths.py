"""The rules for object names and prefixes, shared with the MATLAB toolbox."""

import pytest

from ebrains_bucket_sync.paths import (
    InvalidPrefixError,
    is_safe_relative_path,
    is_transfer_leftover,
    normalize_prefix,
)


@pytest.mark.parametrize(
    "path, safe",
    [
        ("a.txt", True),
        ("sub/a.txt", True),
        ("a/C:/b.txt", True),
        ("ab:c.txt", True),
        ("log 10:30.txt", True),
        ("C:/x", False),
        ("c:x", False),
        ("C:", False),
        ("../a.txt", False),
        ("a/./b", False),
        ("/a.txt", False),
        ("a//b", False),
        ("a/", False),
        ("a\\b", False),
    ],
)
def test_safe_relative_paths_stay_below_the_folder(path, safe):
    assert is_safe_relative_path(path) is safe


@pytest.mark.parametrize(
    "prefix, normalized",
    [("", ""), ("/", ""), ("results", "results/"), ("/results", "results/")],
)
def test_normalize_prefix_gives_a_folder_name_with_a_trailing_slash(prefix, normalized):
    assert normalize_prefix(prefix) == normalized
    assert normalize_prefix("results/sub/") == "results/sub/"


@pytest.mark.parametrize("prefix", ["../set", "a/../b", "a//b", "./a", "C:/set", "c:set", "a\\b"])
def test_normalize_prefix_refuses_a_name_that_is_not_a_folder(prefix):
    with pytest.raises(InvalidPrefixError, match="does not name a folder of the bucket"):
        normalize_prefix(prefix)


@pytest.mark.parametrize(
    "path, leftover",
    [
        ("a.dat.k3x9.sync-part", True),
        ("sub/a.dat.k3x9.sync-part", True),
        ("report.sync-part", True),
        ("big.bin.multipart_manifest.json", True),
        # Matched in lowercase, on the path itself
        ("Report.SYNC-PART", False),
        ("a.sync-part/b.txt", False),
        ("multipart_manifest.json", False),
        ("a.dat", False),
    ],
)
def test_transfer_leftovers_are_the_files_a_transfer_keeps_next_to_its_target(path, leftover):
    assert is_transfer_leftover(path) is leftover
