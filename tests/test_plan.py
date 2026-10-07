import math
from datetime import timedelta

import pytest

from ebrains_bucket_sync.model import PlanItem
from ebrains_bucket_sync.plan import deletion_refusal, plan_sync

from .conftest import entry, utc

T0 = utc(2024, 1, 1)


def test_copies_new_and_changed_files_and_keeps_extraneous_ones():
    source = [entry("new.txt", 1), entry("same.txt", 2), entry("grown.txt", 30)]
    target = [entry("same.txt", 2), entry("grown.txt", 3), entry("extra.txt", 4)]

    plan = plan_sync(source, target)

    assert plan == [
        PlanItem("extra.txt", "none", "extraneous", 4),
        PlanItem("grown.txt", "copy", "size", 30),
        PlanItem("new.txt", "copy", "new", 1),
        PlanItem("same.txt", "none", "unchanged", 2),
    ]


def test_deletes_extraneous_files_when_asked():
    plan = plan_sync([entry("a.txt")], [entry("a.txt"), entry("extra.txt", 4)], delete=True)

    assert [item.action for item in plan] == ["none", "delete"]


def test_copies_file_changed_after_target():
    source = [
        entry("a.txt", 1, T0 + timedelta(days=1)),
        entry("b.txt", 1, T0 + timedelta(seconds=1)),
    ]
    target = [entry("a.txt", 1, T0), entry("b.txt", 1, T0)]

    plan = plan_sync(source, target)

    assert [item.reason for item in plan] == ["newer", "unchanged"]


def test_target_newer_than_source_is_unchanged():
    plan = plan_sync([entry("a.txt", 1, T0)], [entry("a.txt", 1, T0 + timedelta(days=1))])

    assert plan[0].reason == "unchanged"


def test_by_size_ignores_time():
    plan = plan_sync(
        [entry("a.txt", 1, T0 + timedelta(days=1))], [entry("a.txt", 1, T0)], comparison="Size"
    )

    assert plan[0].action == "none"


def test_leaves_file_of_unknown_time():
    plan = plan_sync([entry("a.txt", 1, T0)], [entry("a.txt", 1)])

    assert plan[0].reason == "unchanged"


def test_by_checksum_compares_content_of_same_size():
    # The checksum decides where it is known on both sides, even against
    # the time, and the time decides where it is not.
    day = timedelta(days=1)
    source = [
        entry("edited.txt", 1, T0, "aaa"),
        entry("same.txt", 1, T0 + day, "bbb"),
        entry("unknown.txt", 1, T0 + day, ""),
    ]
    target = [
        entry("edited.txt", 1, T0 + day, "ccc"),
        entry("same.txt", 1, T0, "BBB"),
        entry("unknown.txt", 1, T0, "ddd"),
    ]

    plan = plan_sync(source, target, comparison="Checksum")

    assert [item.reason for item in plan] == ["checksum", "unchanged", "newer"]


def test_size_difference_is_reported_before_checksum_and_time():
    plan = plan_sync(
        [entry("a.txt", 1, T0 + timedelta(days=1), "aaa")],
        [entry("a.txt", 2, T0, "bbb")],
        comparison="Checksum",
    )

    assert plan[0].reason == "size"


def test_empty_sides_give_empty_plan():
    assert plan_sync([], [], delete=True) == []


def test_rejects_unknown_comparison():
    with pytest.raises(ValueError, match="comparison"):
        plan_sync([], [], comparison="Hash")


def test_time_tolerance_is_configurable():
    source = [entry("a.txt", 1, T0 + timedelta(seconds=30))]
    target = [entry("a.txt", 1, T0)]

    assert plan_sync(source, target)[0].reason == "newer"
    assert plan_sync(source, target, time_tolerance=timedelta(minutes=1))[0].reason == "unchanged"


class TestDeletionRefusal:
    def test_nothing_to_refuse_without_deletions(self):
        plan = [PlanItem("extra.txt", "none", "extraneous", 1)]

        assert deletion_refusal(plan, 0, math.inf) is None

    def test_refuses_to_empty_target_from_empty_source(self):
        plan = [PlanItem("a.txt", "delete", "extraneous", 1)]

        refusal = deletion_refusal(plan, 0, math.inf)

        assert refusal is not None
        assert refusal.code == "EmptySource"
        assert "Check the folder and the prefix" in refusal.reason

    def test_refuses_more_deletions_than_allowed(self):
        plan = [
            PlanItem("a.txt", "delete", "extraneous", 1),
            PlanItem("b.txt", "delete", "extraneous", 1),
        ]

        refusal = deletion_refusal(plan, 1, 1)

        assert refusal is not None
        assert refusal.code == "TooManyDeletions"
        assert "'a.txt'" in refusal.reason

    def test_allows_deletions_within_limit(self):
        plan = [PlanItem("a.txt", "delete", "extraneous", 1)]

        assert deletion_refusal(plan, 1, 1) is None
