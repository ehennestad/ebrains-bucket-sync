"""Run the fixtures of spec/fixtures, which the MATLAB toolbox runs too."""

import json
import math
from pathlib import Path

import pytest

from ebrains_bucket_sync.exclude import ExcludeFileError, exclude_files, parse_exclude_file
from ebrains_bucket_sync.model import FileEntry, PlanItem
from ebrains_bucket_sync.plan import deletion_refusal, plan_sync
from ebrains_bucket_sync.remote import parse_listing_time
from ebrains_bucket_sync.virtual_bucket import (
    file_states,
    format_manifest,
    manifest_after_push,
    parse_manifest,
    protect_placeholders,
)

FIXTURES = Path(__file__).resolve().parents[1] / "spec" / "fixtures"


def load_cases(kind: str):
    paths = sorted((FIXTURES / kind).glob("*.json"))
    assert paths, f"no fixtures in {FIXTURES / kind}"
    return [pytest.param(json.loads(path.read_text()), id=path.stem) for path in paths]


def to_entry(data: dict) -> FileEntry:
    return FileEntry(
        data["path"],
        data["bytes"],
        parse_listing_time(data.get("modified_time")),
        data.get("hash", ""),
    )


@pytest.mark.parametrize("case", load_cases("plan"))
def test_plan_fixture(case):
    plan = plan_sync(
        [to_entry(item) for item in case["source"]],
        [to_entry(item) for item in case["target"]],
        comparison=case["policy"]["comparison"],
        delete=case["policy"]["delete"],
    )

    assert plan == [PlanItem(**item) for item in case["expected"]]


@pytest.mark.parametrize("case", load_cases("exclude"))
def test_exclude_fixture(case):
    kept = exclude_files([FileEntry(path, 1) for path in case["paths"]], case["patterns"])

    assert [item.path for item in kept] == case["expected_kept"]


@pytest.mark.parametrize("case", load_cases("exclude-file"))
def test_exclude_file_fixture(case):
    if "expected_error_line" in case:
        with pytest.raises(ExcludeFileError) as error:
            parse_exclude_file(case["text"])
        assert error.value.line_number == case["expected_error_line"]
    else:
        assert parse_exclude_file(case["text"]) == case["expected_patterns"]


@pytest.mark.parametrize("case", load_cases("refusal"))
def test_refusal_fixture(case):
    plan = [PlanItem(**item) for item in case["plan"]]
    max_delete = math.inf if case["max_delete"] is None else case["max_delete"]

    refusal = deletion_refusal(plan, case["n_source_files"], max_delete)

    assert (refusal.code if refusal else None) == case["expected_code"]


def to_entries(items):
    return None if items is None else [to_entry(item) for item in items]


@pytest.mark.parametrize("case", load_cases("manifest"))
def test_manifest_fixture(case):
    entries = to_entries(case["entries"])

    assert parse_manifest(case["text"]) == entries
    assert format_manifest(entries) == case["text"]


@pytest.mark.parametrize("case", load_cases("file-state"))
def test_file_state_fixture(case):
    states = file_states(to_entries(case["local"]), to_entries(case["manifest"]))

    assert states == [(item["path"], item["state"]) for item in case["expected"]]


@pytest.mark.parametrize("case", load_cases("placeholder"))
def test_placeholder_fixture(case):
    plan = protect_placeholders(
        [PlanItem(**item) for item in case["plan"]],
        to_entries(case["local"]),
        to_entries(case["listing"]),
        to_entries(case["manifest"]),
    )

    assert plan == [PlanItem(**item) for item in case["expected"]]


@pytest.mark.parametrize("case", load_cases("manifest-update"))
def test_manifest_update_fixture(case):
    manifest = manifest_after_push(
        to_entries(case["listing"]), to_entries(case["uploaded"]), case["deleted"]
    )

    assert manifest == to_entries(case["expected"])
