"""Loading the actions of plugins from entry points."""

from __future__ import annotations

from importlib.metadata import EntryPoint

from ebrains_bucket_sync import actions as actions_module
from ebrains_bucket_sync.actions import ENTRY_POINT_GROUP, load_actions


class PluginAction:
    scope = "bucket"
    title = ""

    def __init__(self, id: str) -> None:
        self.id = id
        self.label = id.title()

    def required_paths(self, files, selected):
        return []

    def run(self, context, selected):
        return "Done."


def make_a():
    return PluginAction("a")


def make_b():
    return PluginAction("b")


def make_another_a():
    return PluginAction("a")


def make_nothing():
    return object()


def install(monkeypatch, *points: tuple[str, str]) -> None:
    """Make the entry points of the group those given, as (name, value) pairs."""
    entries = [EntryPoint(name, value, ENTRY_POINT_GROUP) for name, value in points]

    def entry_points(*, group):
        assert group == ENTRY_POINT_GROUP
        return entries

    monkeypatch.setattr(actions_module, "entry_points", entry_points)


def test_actions_are_loaded_in_the_order_of_the_names_of_their_entry_points(monkeypatch):
    install(monkeypatch, ("second", f"{__name__}:make_b"), ("first", f"{__name__}:make_a"))

    loaded, failures = load_actions()

    assert [action.id for action in loaded] == ["a", "b"]
    assert failures == []


def test_a_plugin_that_cannot_be_loaded_is_reported_and_the_others_are_kept(monkeypatch):
    install(monkeypatch, ("broken", f"{__name__}:no_such_function"), ("ok", f"{__name__}:make_a"))

    loaded, failures = load_actions()

    assert [action.id for action in loaded] == ["a"]
    assert [name for name, _ in failures] == ["broken"]
    assert isinstance(failures[0][1], AttributeError)


def test_an_entry_point_that_gives_no_action_is_reported(monkeypatch):
    install(monkeypatch, ("odd", f"{__name__}:make_nothing"))

    loaded, failures = load_actions()

    assert loaded == []
    assert [name for name, _ in failures] == ["odd"]
    assert "not an action" in str(failures[0][1])


def test_a_second_action_with_the_same_id_is_reported(monkeypatch):
    install(monkeypatch, ("one", f"{__name__}:make_a"), ("two", f"{__name__}:make_another_a"))

    loaded, failures = load_actions()

    assert [action.id for action in loaded] == ["a"]
    assert [name for name, _ in failures] == ["two"]
    assert 'Two actions have the id "a"' in str(failures[0][1])
