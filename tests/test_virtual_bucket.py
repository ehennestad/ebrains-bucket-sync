import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from ebrains_bucket_sync.model import FileEntry
from ebrains_bucket_sync.virtual_bucket import (
    BucketInfo,
    VirtualBucketError,
    is_metadata_path,
    read_bucket_info,
    read_manifest,
    virtual_bucket_refusal,
    write_bucket_info,
    write_manifest,
)

SCHEMA = json.loads(
    (Path(__file__).resolve().parents[1] / "spec" / "virtual-bucket.schema.json").read_text()
)
LISTED_AT = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


def test_bucket_info_round_trip_matches_schema(tmp_path):
    write_bucket_info(tmp_path, BucketInfo("my-bucket", "/results", LISTED_AT))

    data = json.loads((tmp_path / ".ebrains" / "bucket.json").read_text())
    jsonschema.validate(data, SCHEMA)
    assert data["prefix"] == "results/"
    assert data["listed_at"] == "2024-01-02T03:04:05Z"
    assert read_bucket_info(tmp_path) == BucketInfo("my-bucket", "results/", LISTED_AT)


def test_never_listed_bucket_matches_schema(tmp_path):
    write_bucket_info(tmp_path, BucketInfo("my-bucket"))

    data = json.loads((tmp_path / ".ebrains" / "bucket.json").read_text())
    jsonschema.validate(data, SCHEMA)
    assert data["listed_at"] is None


def test_folder_without_metadata_has_no_info_or_manifest(tmp_path):
    assert read_bucket_info(tmp_path) is None
    assert read_manifest(tmp_path) is None


def test_writing_keeps_fields_it_does_not_know(tmp_path):
    metadata = tmp_path / ".ebrains"
    metadata.mkdir()
    (metadata / "bucket.json").write_text(
        json.dumps({"format_version": 1, "bucket": "b", "prefix": "", "listed_at": None, "x": 1})
    )

    write_bucket_info(tmp_path, BucketInfo("b", "", LISTED_AT))

    assert json.loads((metadata / "bucket.json").read_text())["x"] == 1


def test_newer_format_version_is_refused(tmp_path):
    metadata = tmp_path / ".ebrains"
    metadata.mkdir()
    (metadata / "bucket.json").write_text(
        json.dumps({"format_version": 2, "bucket": "b", "prefix": "", "listed_at": None})
    )

    with pytest.raises(VirtualBucketError, match="format_version 2"):
        read_bucket_info(tmp_path)


def test_manifest_round_trip_leaves_no_temporary_file(tmp_path):
    entries = [FileEntry("a.txt", 3, LISTED_AT, "abc")]

    write_manifest(tmp_path, entries)

    assert read_manifest(tmp_path) == entries
    assert [path.name for path in (tmp_path / ".ebrains").iterdir()] == ["manifest.csv"]


def test_metadata_paths():
    assert is_metadata_path(".ebrains")
    assert is_metadata_path(".ebrains/manifest.csv")
    assert not is_metadata_path(".ebrainsx/a")
    assert not is_metadata_path("sub/.ebrains/a")


def test_sync_of_other_bucket_or_prefix_is_refused():
    info = BucketInfo("my-bucket", "results/")

    assert virtual_bucket_refusal(None, "any", "") is None
    assert virtual_bucket_refusal(info, "my-bucket", "/results") is None
    assert virtual_bucket_refusal(info, "other", "results/").code == "OtherBucket"
    assert virtual_bucket_refusal(info, "my-bucket", "").code == "OtherBucket"
