import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from ebrains_bucket_sync.model import FileEntry
from ebrains_bucket_sync.virtual_bucket import (
    BucketInfo,
    ManifestError,
    VirtualBucketError,
    file_states,
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
MD5_OF_A = "0cc175b9c0f1b6a831c399e269772661"
VALID_BUCKET_JSON = {"format_version": 1, "bucket": "b", "prefix": "", "listed_at": None}


def write_metadata_file(root, name, content):
    metadata = root / ".ebrains"
    metadata.mkdir(exist_ok=True)
    path = metadata / name
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


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


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"format_version": True}, "format_version True"),
        ({"format_version": 0}, "format_version 0"),
        ({"format_version": None}, "format_version None"),
        ({"bucket": ""}, "bucket ''"),
        ({"bucket": 5}, "bucket 5"),
        ({"prefix": None}, "prefix None"),
        ({"prefix": "results"}, "prefix 'results'"),
        ({"prefix": "/results/"}, "prefix '/results/'"),
        ({"prefix": "../results/"}, "prefix '../results/'"),
        ({"listed_at": "2024-01-02 03:04:05"}, "listed_at '2024-01-02 03:04:05'"),
        ({"listed_at": "2024-01-02T03:04:05.5Z"}, "listed_at '2024-01-02T03:04:05.5Z'"),
        ({"listed_at": 5}, "listed_at 5"),
    ],
)
def test_bucket_json_with_a_bad_field_is_refused(tmp_path, change, problem):
    data = VALID_BUCKET_JSON | change
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(data, SCHEMA)
    path = write_metadata_file(tmp_path, "bucket.json", json.dumps(data))

    with pytest.raises(VirtualBucketError) as error:
        read_bucket_info(tmp_path)

    assert str(error.value).startswith(f"{path} cannot be read: {problem} ")
    assert str(error.value).endswith(
        "The virtual bucket cannot be used until the file is corrected."
    )


@pytest.mark.parametrize(
    ("content", "problem"),
    [
        ("{", "it is not JSON text in UTF-8"),
        (b'{"bucket": "\xff"}', "it is not JSON text in UTF-8"),
        ("[]", "it does not hold a JSON object"),
        (json.dumps({"format_version": 1, "bucket": "b", "prefix": ""}), "it has no listed_at"),
        (json.dumps({"bucket": "b", "prefix": "", "listed_at": None}), "format_version None"),
    ],
)
def test_bucket_json_that_cannot_be_read_is_refused(tmp_path, content, problem):
    write_metadata_file(tmp_path, "bucket.json", content)

    with pytest.raises(VirtualBucketError, match=problem):
        read_bucket_info(tmp_path)


def test_manifest_error_names_the_file_and_the_line(tmp_path):
    path = write_metadata_file(
        tmp_path, "manifest.csv", "path,bytes,modified_time,hash\na.txt,1,,\nb.txt,x,,\n"
    )

    with pytest.raises(ManifestError) as error:
        read_manifest(tmp_path)

    assert (error.value.path, error.value.line_number) == (path, 3)
    assert str(error.value) == (
        f"{path}, line 3: bytes 'x' is not a whole number. "
        "The virtual bucket cannot be used until the line is corrected."
    )


def test_manifest_line_numbers_count_the_lines_of_quoted_line_breaks(tmp_path):
    write_metadata_file(
        tmp_path,
        "manifest.csv",
        'path,bytes,modified_time,hash\n"two\nlines.txt",1,,\nb.txt,-1,,\n',
    )

    with pytest.raises(ManifestError) as error:
        read_manifest(tmp_path)

    assert error.value.line_number == 4


def test_manifest_with_crlf_line_ends_is_read(tmp_path):
    write_metadata_file(tmp_path, "manifest.csv", "path,bytes,modified_time,hash\r\na.txt,1,,\r\n")

    assert read_manifest(tmp_path) == [FileEntry("a.txt", 1, None, "")]


def test_manifest_that_is_not_utf8_is_refused(tmp_path):
    write_metadata_file(tmp_path, "manifest.csv", b"path,bytes,modified_time,hash\n\xff,1,,\n")

    with pytest.raises(VirtualBucketError, match="is not UTF-8 text"):
        read_manifest(tmp_path)


def test_file_states_compute_a_local_checksum_only_where_it_decides():
    local = [
        FileEntry("same-time.dat", 1, LISTED_AT),
        FileEntry("pushed.dat", 1, LISTED_AT),
        FileEntry("resized.dat", 2, LISTED_AT),
    ]
    manifest = [
        FileEntry("same-time.dat", 1, LISTED_AT, MD5_OF_A),
        FileEntry("pushed.dat", 1, None, MD5_OF_A),
        FileEntry("resized.dat", 1, None, MD5_OF_A),
    ]
    hashed = []

    def local_hash(entry):
        hashed.append(entry.path)
        return MD5_OF_A

    states = file_states(local, manifest, local_hash=local_hash)

    assert states == [
        ("pushed.dat", "downloaded"),
        ("resized.dat", "differs"),
        ("same-time.dat", "downloaded"),
    ]
    assert hashed == ["pushed.dat"]


def test_manifest_round_trip_leaves_no_temporary_file(tmp_path):
    entries = [FileEntry("a.txt", 3, LISTED_AT, MD5_OF_A)]

    write_manifest(tmp_path, entries)

    assert read_manifest(tmp_path) == entries
    assert [path.name for path in (tmp_path / ".ebrains").iterdir()] == ["manifest.csv"]


def test_metadata_paths():
    assert is_metadata_path(".ebrains")
    assert is_metadata_path(".ebrains/manifest.csv")
    assert is_metadata_path(".EBRAINS/manifest.csv")
    assert is_metadata_path(".Ebrains")
    assert not is_metadata_path(".ebrainsx/a")
    assert not is_metadata_path("sub/.ebrains/a")


def test_sync_of_other_bucket_or_prefix_is_refused():
    info = BucketInfo("my-bucket", "results/")

    assert virtual_bucket_refusal(None, "any", "") is None
    assert virtual_bucket_refusal(info, "my-bucket", "/results") is None
    assert virtual_bucket_refusal(info, "other", "results/").code == "OtherBucket"
    assert virtual_bucket_refusal(info, "my-bucket", "").code == "OtherBucket"
