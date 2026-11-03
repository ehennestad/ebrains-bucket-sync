"""Actions that plugins add to the virtual bucket browser.

An action is a tool that runs on the files of a virtual bucket from the
browser, such as a validator of the dataset. The browser shows each
action as a button after its own, and an action on files also in the
menus of files and folders, as the header of the page describes under
"actions". When the button is pressed the host downloads the files the
action asks for, runs it, and shows the message it returns below the
file list.

A package adds its actions by registering them in the entry point group
ENTRY_POINT_GROUP of its project metadata, one entry point per action,
whose value names a callable that takes no arguments and returns an
Action:

    [project.entry-points."ebrains_bucket_sync.actions"]
    validate-bids = "my_package.actions:ValidateBids"

The browse command loads the entry points of every installed package
(load_actions), so the actions appear once the package is installed in
the same environment as ebrains-bucket-sync. Host also takes actions
directly, which is how tests and other programs give it some.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .virtual_bucket import METADATA_FOLDER

if TYPE_CHECKING:
    from .browser import FileRow

ENTRY_POINT_GROUP = "ebrains_bucket_sync.actions"
"""The entry point group in which a package registers its actions."""

SCOPES = ("selection", "bucket")
"""What an action runs on: the selected files, or the whole virtual bucket."""

REPORTS_FOLDER = "reports"
"""Folder below .ebrains where actions keep their reports, which no sync touches."""


@runtime_checkable
class Action(Protocol):
    """A tool the browser runs on a virtual bucket.

    Attributes:
        id: Names the action in the requests of the page. Unique among the
            actions of a host.
        label: Text of the button of the action.
        scope: "selection" to run on the selected files, which the page
            sends with the request, or "bucket" to run on the whole
            virtual bucket, with no paths.
        title: Tooltip of the button, "" for none.
    """

    id: str
    label: str
    scope: str
    title: str

    def required_paths(self, files: Sequence[FileRow], selected: Sequence[str]) -> list[str]:
        """The paths that must be local before run is called.

        The host downloads those that are online only, in this order,
        with its progress dialog, and does not run the action when a
        download fails or is cancelled. files is every file of the
        virtual bucket with its state, and selected the paths the page
        sent: the selected files for the "selection" scope, empty for
        the "bucket" scope. A path that is not in files is ignored.
        """
        ...

    def run(self, context: ActionContext, selected: Sequence[str]) -> str:
        """Run the tool, and return a one-line message for the browser to show.

        selected is as for required_paths. The host shows the action's
        progress dialog with a Cancel button while run is called, so the
        action reports its progress through context.progress and raises
        BrowserCancelled from context.check_cancelled once the user
        pressed Cancel. An exception is shown to the user as an alert,
        with the message of the exception.
        """
        ...


@dataclass(frozen=True)
class ActionContext:
    """What an action gets to run with.

    Attributes:
        folder: Absolute path of the virtual bucket.
        bucket: Name of the bucket it stands for.
        prefix: Folder of the bucket it holds, "" for the whole bucket.
        files: Every file of the virtual bucket with its state, after the
            downloads the action asked for.
        progress: Updates the progress dialog of the action: called with
            a message and the fraction done, from 0 to 1, or None for a
            task of unknown length.
        check_cancelled: Raises BrowserCancelled once the user pressed
            Cancel in the progress dialog.
        confirm: Asks the user to confirm something, called with a
            message, a title and the text of the confirm button, and
            returns whether they did.
        alert: Tells the user something, called with a title, a message
            and the icon "error", "warning", "info" or "success".
        open_file: Opens a file with the default application of the
            system, such as the report of the action.
    """

    folder: Path
    bucket: str
    prefix: str
    files: list[FileRow]
    progress: Callable[[str, float | None], None]
    check_cancelled: Callable[[], None]
    confirm: Callable[[str, str, str], bool]
    alert: Callable[[str, str, str], None]
    open_file: Callable[[Path], None]

    def report_path(self, name: str) -> Path:
        """The path of a report file of the action, below the reports folder of .ebrains.

        The folder is created if it does not exist. The .ebrains folder
        is never synced and the browser does not show it, so a report
        kept there is neither uploaded nor mistaken for data.
        """
        folder = self.folder / METADATA_FOLDER / REPORTS_FOLDER
        folder.mkdir(parents=True, exist_ok=True)
        return folder / name


def check_actions(actions: Sequence[Action]) -> None:
    """Refuse actions whose ids repeat or whose scope is unknown, with a ValueError."""
    ids: set[str] = set()
    for action in actions:
        if action.scope not in SCOPES:
            raise ValueError(
                f'The scope of the action "{action.id}" is "{action.scope}"; '
                f"it must be one of {', '.join(SCOPES)}."
            )
        if action.id in ids:
            raise ValueError(f'Two actions have the id "{action.id}".')
        ids.add(action.id)


def action_data(action: Action) -> dict[str, Any]:
    """The entry of an action in the actions list of the state event."""
    return {
        "id": action.id,
        "label": action.label,
        "scope": action.scope,
        "title": getattr(action, "title", "") or "",
    }


def load_actions(
    group: str = ENTRY_POINT_GROUP,
) -> tuple[list[Action], list[tuple[str, Exception]]]:
    """The actions of the installed packages, in the order of the names of their entry points.

    Returns the actions, and for each entry point that could not be
    loaded or does not give an action its name and the error, so that a
    broken plugin does not keep the browser from opening.
    """
    actions: list[Action] = []
    failures: list[tuple[str, Exception]] = []
    for entry_point in sorted(entry_points(group=group), key=lambda point: point.name):
        try:
            action = entry_point.load()()
            if not isinstance(action, Action):
                raise TypeError(
                    f"{entry_point.value} returned {type(action).__name__}, not an action "
                    "with id, label, scope, title, required_paths and run."
                )
            check_actions([*actions, action])
        except Exception as error:  # a plugin may fail in any way
            failures.append((entry_point.name, error))
            continue
        actions.append(action)
    return actions, failures
