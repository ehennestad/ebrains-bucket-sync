"""The registry of virtual buckets, spec/README.md#virtual-bucket-registry."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import pytest

from ebrains_bucket_sync.config import CONFIG_DIR_VARIABLE
from ebrains_bucket_sync.registry import (
    FolderEntry,
    ListedBucket,
    Registry,
    RegistryError,
    bucket_info_of,
    format_registry,
    list_virtual_buckets,
    normalize_path,
    parse_registry,
    read_registry,
    registry_path,
    root_children,
    write_registry,
)
from ebrains_bucket_sync.virtual_bucket import BucketInfo, write_bucket_info

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "spec" / "bucket-registry.schema.json").read_text()
)
OPENED_AT = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
LATER = datetime(2024, 5, 6, 7, 8, 9, tzinfo=timezone.utc)


def folder_data(path, bucket, prefix="", pinned=False, last_opened=None):
    return {
        "path": path,
        "bucket": bucket,
        "prefix": prefix,
        "pinned": pinned,
        "last_opened": last_opened,
    }


def full_registry() -> Registry:
    registry = Registry(root="/root")
    registry.set_label("d-1", "Mouse V1")
    registry.record_open("/root/d-1", "d-1", "", OPENED_AT)
    registry.set_pinned("/data/results", True, bucket="my-collab", prefix="results/")
    return registry


def test_a_missing_file_is_an_empty_registry(tmp_path):
    registry = read_registry(tmp_path / "virtual-buckets.json")

    assert registry == Registry()


def test_the_registry_file_is_in_the_configuration_folder(tmp_path, monkeypatch):
    monkeypatch.setenv(CONFIG_DIR_VARIABLE, str(tmp_path))

    assert registry_path() == tmp_path / "virtual-buckets.json"


def test_a_written_registry_follows_the_schema_and_reads_back(tmp_path):
    path = tmp_path / "config" / "virtual-buckets.json"
    registry = full_registry()

    write_registry(registry, path)

    data = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.validate(data, SCHEMA)
    assert read_registry(path) == registry


def test_the_file_is_written_sorted_without_fields_the_module_does_not_know(tmp_path):
    path = tmp_path / "virtual-buckets.json"
    path.write_text(
        json.dumps(
            {
                "format_version": 1,
                "root": None,
                "theme": "dark",
                "buckets": [{"bucket": "z", "label": "Z"}, {"bucket": "a", "label": "A"}],
                "folders": [folder_data("/z", "z"), folder_data("/a", "a", pinned=True)],
            }
        )
    )

    write_registry(read_registry(path), path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert "theme" not in data
    assert [item["bucket"] for item in data["buckets"]] == ["a", "z"]
    assert [item["path"] for item in data["folders"]] == ["/a", "/z"]


def test_an_interrupted_write_leaves_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / "virtual-buckets.json"
    write_registry(Registry(root="/old"), path)

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("ebrains_bucket_sync.virtual_bucket.os.replace", fail)
    with pytest.raises(OSError):
        write_registry(Registry(root="/new"), path)

    assert read_registry(path).root == "/old"
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "text, problem",
    [
        ("not json", "it is not JSON text in UTF-8"),
        ("[]", "it does not hold a JSON object"),
        ('{"root": null, "buckets": [], "folders": []}', "format_version None is not"),
        ('{"format_version": 1, "root": null, "buckets": []}', "it has no folders"),
        ('{"format_version": 1, "root": 3, "buckets": [], "folders": []}', "root 3 is neither"),
        (
            '{"format_version": 1, "root": null, "buckets": {}, "folders": []}',
            "buckets is not a list",
        ),
    ],
)
def test_a_file_that_does_not_follow_the_schema_is_refused_by_name(text, problem):
    with pytest.raises(RegistryError) as error:
        parse_registry(text, name="/config/virtual-buckets.json")

    assert str(error.value).startswith("/config/virtual-buckets.json cannot be read: " + problem)
    assert "corrected or deleted" in str(error.value)


def test_a_newer_format_version_is_refused_with_advice_to_update():
    text = '{"format_version": 2, "root": null, "buckets": [], "folders": []}'

    with pytest.raises(RegistryError, match="format_version 2.*Update the package"):
        parse_registry(text)


def test_a_file_that_is_not_utf8_is_refused(tmp_path):
    path = tmp_path / "virtual-buckets.json"
    path.write_bytes(b'{"format_version": 1, "root": "\xff", "buckets": [], "folders": []}')

    with pytest.raises(RegistryError, match="not JSON text in UTF-8"):
        read_registry(path)


def test_opening_records_the_folder_and_keeps_its_pin():
    registry = Registry()
    registry.set_pinned("/a", True, bucket="old", prefix="old/")

    registry.record_open("/a", "new", "", OPENED_AT)
    registry.record_open("/b", "b", "sub/", LATER)

    assert registry.entry_of("/a") == FolderEntry("/a", "new", "", True, OPENED_AT)
    assert registry.entry_of("/b") == FolderEntry("/b", "b", "sub/", False, LATER)


def test_opening_records_the_time_in_utc_to_the_second():
    registry = Registry()
    oslo = timezone(timedelta(hours=1))

    registry.record_open("/a", "a", "", datetime(2024, 1, 2, 4, 4, 5, 678000, tzinfo=oslo))

    assert registry.entry_of("/a").last_opened == OPENED_AT
    assert parse_registry(format_registry(registry)) == registry


def test_opening_needs_an_aware_time():
    with pytest.raises(ValueError, match="aware"):
        Registry().record_open("/a", "a", "", datetime(2024, 1, 2, 3, 4, 5))


@pytest.mark.parametrize("bucket, prefix", [("", ""), ("a", "results"), ("a", "../x/")])
def test_a_bucket_or_prefix_the_file_cannot_hold_is_refused_before_it_is_recorded(bucket, prefix):
    registry = Registry()

    with pytest.raises(ValueError):
        registry.record_open("/a", bucket, prefix, OPENED_AT)
    with pytest.raises(ValueError):
        registry.set_pinned("/a", True, bucket=bucket, prefix=prefix)

    assert registry.folders == {}


def test_unpinning_an_unrecorded_folder_changes_nothing():
    registry = Registry()

    registry.set_pinned("/a", False)
    registry.set_pinned("/b", False, bucket="b", prefix="")

    assert registry.folders == {}


def test_a_file_that_cannot_be_read_is_refused_by_name(tmp_path):
    path = tmp_path / "virtual-buckets.json"
    path.mkdir()

    with pytest.raises(RegistryError, match="cannot be read"):
        read_registry(path)


def test_normalize_path_gives_the_absolute_path_without_a_trailing_separator(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert normalize_path("sub/./x/") == str(tmp_path / "sub" / "x")
    assert normalize_path(str(tmp_path) + os.sep) == str(tmp_path)
    assert normalize_path(tmp_path / "a" / ".." / "b") == str(tmp_path / "b")


def test_root_children_are_written_as_recorded_paths(tmp_path):
    (tmp_path / "a").mkdir()

    assert root_children(str(tmp_path) + os.sep + "." + os.sep) == [str(tmp_path / "a")]


def test_opening_again_replaces_the_time():
    registry = Registry()
    registry.record_open("/a", "a", "", OPENED_AT)

    registry.record_open("/a", "a", "", LATER)

    assert registry.entry_of("/a").last_opened == LATER


def test_pinning_an_unrecorded_folder_records_it_without_a_time():
    registry = Registry()

    registry.set_pinned("/a", True, bucket="a", prefix="")

    assert registry.entry_of("/a") == FolderEntry("/a", "a", "", True, None)


def test_pinning_an_unrecorded_folder_needs_its_bucket_and_prefix():
    with pytest.raises(ValueError, match="not recorded"):
        Registry().set_pinned("/a", True)


def test_unpinning_keeps_the_entry():
    registry = Registry()
    registry.record_open("/a", "a", "", OPENED_AT)
    registry.set_pinned("/a", True)

    registry.set_pinned("/a", False)

    assert registry.entry_of("/a") == FolderEntry("/a", "a", "", False, OPENED_AT)


def test_paths_are_recorded_as_given():
    registry = Registry()

    registry.record_open(Path("/a"), "a", "", OPENED_AT)

    assert registry.entry_of("/a") is not None
    assert registry.entry_of(Path("/a")) is not None


def test_an_empty_label_removes_the_label():
    registry = Registry()
    registry.set_label("d-1", "Mouse V1")
    registry.set_label("d-2", "  kept as given ")

    registry.set_label("d-1", "")
    registry.set_label("never", "")

    assert registry.labels == {"d-2": "  kept as given "}
    assert registry.label_of("d-1") is None


def test_removing_a_folder_forgets_its_entry_and_tolerates_an_unknown_one():
    registry = Registry()
    registry.record_open("/a", "a", "", OPENED_AT)

    registry.remove_folder("/a")
    registry.remove_folder("/never")

    assert registry.folders == {}


def test_root_children_are_the_folders_directly_below_the_root(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "nested").mkdir()
    (tmp_path / "file.txt").write_text("")

    assert root_children(tmp_path) == [str(tmp_path / "a"), str(tmp_path / "b")]


def test_no_root_or_an_unreadable_root_gives_no_children(tmp_path):
    assert root_children(None) == []
    assert root_children("") == []
    assert root_children(tmp_path / "missing") == []


def test_bucket_info_of_reads_the_bucket_json_of_a_folder(tmp_path):
    folder = tmp_path / "vb"
    folder.mkdir()
    write_bucket_info(folder, BucketInfo("d-1", "sub/", None))
    plain = tmp_path / "plain"
    plain.mkdir()
    broken = tmp_path / "broken"
    (broken / ".ebrains").mkdir(parents=True)
    (broken / ".ebrains" / "bucket.json").write_text("{")

    assert bucket_info_of(folder) == ("d-1", "sub/")
    assert bucket_info_of(plain) is None
    assert bucket_info_of(broken) is None
    assert bucket_info_of(tmp_path / "missing") is None


def test_the_list_merges_the_root_scan_with_the_recorded_folders(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    write_bucket_info(root / "d-1", BucketInfo("d-1", "", None))
    (root / "notes").mkdir()
    elsewhere = tmp_path / "elsewhere"
    write_bucket_info(elsewhere, BucketInfo("other", "", None))
    registry = Registry(root=str(root))
    registry.set_label("d-1", "Mouse V1")
    registry.record_open(elsewhere, "other", "", OPENED_AT)
    registry.record_open(tmp_path / "gone", "gone", "x/", LATER)

    rows = list_virtual_buckets(registry, root_children(registry.root), bucket_info_of)

    assert rows == [
        ListedBucket(str(tmp_path / "gone"), "gone", "x/", None, False, LATER, False),
        ListedBucket(str(elsewhere), "other", "", None, False, OPENED_AT, True),
        ListedBucket(str(root / "d-1"), "d-1", "", "Mouse V1", False, None, True),
    ]


def test_format_registry_writes_times_in_utc():
    registry = Registry()
    registry.record_open("/a", "a", "", OPENED_AT)

    data = json.loads(format_registry(registry))

    assert data["folders"][0]["last_opened"] == "2024-01-02T03:04:05Z"
