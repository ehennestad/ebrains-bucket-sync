"""Command line of ebrains-bucket-sync."""

from __future__ import annotations

import math
import sys
import threading
import warnings
from pathlib import Path

import click

from .auth import AuthError, DeviceFlowAuthenticator
from .engine import SyncEvent, SyncRefused, sync_to_bucket
from .model import COMPARISONS, ActionResult, BucketStorage, SyncOptions, SyncWarning
from .paths import normalize_prefix
from .planfile import plan_to_dict, write_plan_file
from .storage import EbrainsDriveStorage

EXIT_INCOMPLETE = 1
EXIT_REFUSED = 2


def make_authenticator() -> DeviceFlowAuthenticator:
    return DeviceFlowAuthenticator()


def make_storage() -> BucketStorage:
    return EbrainsDriveStorage(make_authenticator())


@click.group()
@click.version_option(package_name="ebrains-bucket-sync")
def main() -> None:
    """Sync local folders with EBRAINS Data Proxy buckets."""
    warnings.simplefilter("always", SyncWarning)
    warnings.showwarning = _show_warning


@main.command()
@click.option("--force", is_flag=True, help="Log in again even when a valid login is stored.")
def login(force: bool) -> None:
    """Log in to EBRAINS. A link opens the login in the browser."""
    authenticator = make_authenticator()
    if force:
        authenticator.logout()
    try:
        authenticator.access_token()
    except AuthError as error:
        raise click.ClickException(str(error)) from error
    click.echo("Logged in to EBRAINS.", err=True)


@main.command()
def logout() -> None:
    """Forget the stored login."""
    make_authenticator().logout()
    click.echo("Logged out.", err=True)


@main.command()
@click.argument("local_folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.argument("bucket")
@click.option(
    "--prefix",
    default="",
    help='Folder of the bucket to sync to, such as "results". Default: the root of the bucket.',
)
@click.option(
    "--delete", is_flag=True, help="Also delete the objects that LOCAL_FOLDER does not have."
)
@click.option(
    "--comparison",
    type=click.Choice(COMPARISONS),
    default="SizeAndTime",
    show_default=True,
    help="How a file the bucket has is judged changed.",
)
@click.option(
    "--exclude",
    multiple=True,
    metavar="PATTERN",
    help='Wildcard pattern of paths to leave out, such as "*.tmp" or ".git". May be repeated.',
)
@click.option("--dry-run", is_flag=True, help="Only show what would be uploaded and deleted.")
@click.option(
    "--max-delete",
    type=click.IntRange(min=0),
    default=None,
    help="Most objects the sync may delete. More stops it before it changes anything.",
)
@click.option(
    "--workers",
    type=click.IntRange(min=1),
    default=4,
    show_default=True,
    help="Files uploaded at the same time.",
)
@click.option(
    "--plan-file",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write the plan and the outcome as JSON to this file.",
)
@click.option("--quiet", is_flag=True, help="Print only failures.")
def push(
    local_folder: Path,
    bucket: str,
    prefix: str,
    delete: bool,
    comparison: str,
    exclude: tuple[str, ...],
    dry_run: bool,
    max_delete: int | None,
    workers: int,
    plan_file: Path | None,
    quiet: bool,
) -> None:
    """Make BUCKET, or a folder of it, match LOCAL_FOLDER.

    Only new and changed files are uploaded, so a sync that is interrupted
    picks up where it stopped when run again. Object names are the paths
    relative to LOCAL_FOLDER.
    """
    options = SyncOptions(
        prefix=prefix,
        delete=delete,
        comparison=comparison,
        exclude=tuple(exclude),
        dry_run=dry_run,
        max_delete=math.inf if max_delete is None else max_delete,
        workers=workers,
    )
    printer = ConsolePrinter(local_folder, bucket, options, quiet=quiet)

    refusal = ""
    exit_code = 0
    try:
        results = sync_to_bucket(local_folder, bucket, make_storage(), options, on_event=printer)
    except SyncRefused as error:
        refusal = error.reason
        results = printer.results
        exit_code = EXIT_REFUSED
        click.echo(f"Stopped before changing anything: {error.reason}", err=True)
    except AuthError as error:
        raise click.ClickException(str(error)) from error
    else:
        if any(result.status == "failed" for result in results):
            exit_code = EXIT_INCOMPLETE

    if plan_file is not None:
        write_plan_file(
            plan_file,
            plan_to_dict(
                results, bucket=bucket, local_folder=local_folder, options=options, refusal=refusal
            ),
        )

    sys.exit(exit_code)


class ConsolePrinter:
    """Prints the events of a sync, one line per action, as the MATLAB toolbox does."""

    def __init__(
        self, local_folder: Path, bucket: str, options: SyncOptions, *, quiet: bool = False
    ) -> None:
        self.results: list[ActionResult] = []
        self._local_folder = local_folder
        self._options = options
        self._quiet = quiet
        self._lock = threading.Lock()
        prefix = normalize_prefix(options.prefix)
        self._where = f'bucket "{bucket}"' + (f', folder "{prefix}"' if prefix else "")

    def __call__(self, event: SyncEvent) -> None:
        with self._lock:
            getattr(self, "_on_" + event.kind)(event)

    def _say(self, message: str) -> None:
        if not self._quiet:
            click.echo(message)

    def _on_planned(self, event: SyncEvent) -> None:
        self.results = list(event.results)
        uploads = [r for r in event.results if r.action == "upload"]
        deletions = [r for r in event.results if r.action == "delete"]
        unchanged = sum(1 for r in event.results if r.reason == "unchanged")
        kept = sum(1 for r in event.results if r.reason == "extraneous" and r.action != "delete")

        self._say(f'Syncing "{self._local_folder}" to {self._where}.')
        self._say(
            f"{len(uploads)} file(s) to upload ({format_size(sum(r.bytes for r in uploads))}), "
            f"{len(deletions)} to delete, {unchanged} unchanged."
        )
        if kept:
            self._say(
                f"{kept} object(s) of the bucket are not in the folder and are kept. "
                "Use --delete to delete them."
            )
        if self._options.dry_run:
            for result in uploads + deletions:
                self._say(
                    f"  [dry-run] {result.action.capitalize()} {result.path} ({result.reason})"
                )
            if event.message:
                self._say(
                    f"[dry-run] A real run would stop before changing anything: {event.message}"
                )

    def _on_upload(self, event: SyncEvent) -> None:
        self._say(f"[{event.index}/{event.total}] Upload {event.path} ({format_size(event.bytes)})")

    def _on_uploaded(self, event: SyncEvent) -> None:
        pass

    def _on_upload_failed(self, event: SyncEvent) -> None:
        click.echo(f"  Failed: {event.path}: {event.message}", err=True)

    def _on_delete(self, event: SyncEvent) -> None:
        self._say(f"[{event.index}/{event.total}] Delete {event.path}")

    def _on_deleted(self, event: SyncEvent) -> None:
        pass

    def _on_delete_failed(self, event: SyncEvent) -> None:
        click.echo(f"  Failed: {event.path}: {event.message}", err=True)

    def _on_finished(self, event: SyncEvent) -> None:
        uploaded = sum(1 for r in event.results if r.action == "upload" and r.status == "done")
        deleted = sum(1 for r in event.results if r.action == "delete" and r.status == "done")
        failed = [r for r in event.results if r.status == "failed"]
        self._say(f"Done: {uploaded} uploaded, {deleted} deleted, {len(failed)} failed.")
        if failed:
            click.echo(
                f"The sync is incomplete: {len(failed)} file(s) failed, for example "
                f'"{failed[0].path}". Run the sync again to retry them.',
                err=True,
            )


def format_size(n_bytes: float) -> str:
    """A size such as "1.5 MB", in units of 1024."""
    value = float(n_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"


def _show_warning(message, category, filename, lineno, file=None, line=None) -> None:  # noqa: B006
    if issubclass(category, SyncWarning):
        click.echo(f"Warning: {message}", err=True)
    else:
        click.echo(f"{category.__name__}: {message}", err=True)
