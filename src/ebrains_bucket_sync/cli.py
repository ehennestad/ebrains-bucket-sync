"""Command line of ebrains-bucket-sync."""

from __future__ import annotations

import math
import sys
import threading
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from .auth import AuthError, DeviceFlowAuthenticator
from .browser import ENV_PAGE, VirtualBucket, default_page_path, format_local_time, show_browser
from .engine import SyncEvent, SyncRefused, is_case_collision, sync_from_bucket, sync_to_bucket
from .exclude import ExcludeFileError, read_exclude_file
from .model import COMPARISONS, ActionResult, BucketStorage, SyncOptions, SyncWarning
from .paths import InvalidPrefixError, normalize_prefix
from .planfile import plan_to_dict, write_plan_file
from .registry import (
    ListedBucket,
    Registry,
    RegistryError,
    bucket_info_of,
    list_virtual_buckets,
    normalize_path,
    read_registry,
    root_children,
    update_registry,
)
from .storage import EbrainsDriveStorage, request_error_message
from .text import format_size
from .virtual_bucket import VirtualBucketError

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
    try:
        if force:
            authenticator.log_in_again()
        else:
            authenticator.access_token()
    except AuthError as error:
        raise click.ClickException(str(error)) from error
    click.echo("Logged in to EBRAINS.", err=True)


@main.command()
def logout() -> None:
    """Forget the stored login."""
    make_authenticator().logout()
    click.echo("Logged out.", err=True)


def _checked_prefix(
    context: click.Context, parameter: click.Parameter, value: str | None
) -> str | None:
    """Refuse a --prefix that does not name a folder of the bucket, before anything is done."""
    if value is not None:
        try:
            normalize_prefix(value)
        except InvalidPrefixError as error:
            raise click.BadParameter(str(error)) from error
    return value


def sync_options(direction: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """The options that push (direction "to_bucket") and pull ("from_bucket") share."""
    is_push = direction == "to_bucket"
    target_files = "objects" if is_push else "local files"
    transferred = "uploaded" if is_push else "downloaded"
    options = [
        click.option(
            "--prefix",
            default="",
            callback=_checked_prefix,
            help=f"Folder of the bucket to sync {'to' if is_push else 'from'}, such as "
            '"results". Default: the root of the bucket.',
        ),
        click.option(
            "--delete",
            is_flag=True,
            help="Also delete the objects that LOCAL_FOLDER does not have."
            if is_push
            else "Also delete the local files that the bucket does not have.",
        ),
        click.option(
            "--comparison",
            type=click.Choice(COMPARISONS),
            default="SizeAndTime",
            show_default=True,
            help=f"How a file {'the bucket' if is_push else 'LOCAL_FOLDER'} has is judged changed.",
        ),
        click.option(
            "--exclude",
            multiple=True,
            metavar="PATTERN",
            help='Wildcard pattern of paths to leave out, such as "*.tmp" or ".git". '
            "May be repeated.",
        ),
        click.option(
            "--exclude-from",
            multiple=True,
            type=click.Path(exists=True, dir_okay=False, path_type=Path),
            metavar="FILE",
            help="File of exclude patterns, one per line, in the layout of a .gitignore file. "
            "May be repeated.",
        ),
        click.option(
            "--dry-run",
            is_flag=True,
            help=f"Only show what would be {transferred} and deleted.",
        ),
        click.option(
            "--max-delete",
            type=click.IntRange(min=0),
            default=None,
            help=f"Most {target_files} the sync may delete. More stops it before it changes "
            "anything.",
        ),
        click.option(
            "--workers",
            type=click.IntRange(min=1),
            default=4,
            show_default=True,
            help=f"Files {transferred} at the same time.",
        ),
        click.option(
            "--plan-file",
            type=click.Path(dir_okay=False, path_type=Path),
            help="Write the plan and the outcome as JSON to this file.",
        ),
        click.option("--quiet", is_flag=True, help="Print only failures."),
    ]

    def decorate(command: Callable[..., Any]) -> Callable[..., Any]:
        for option in reversed(options):
            command = option(command)
        return command

    return decorate


@main.command()
@click.argument("local_folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.argument("bucket")
@sync_options("to_bucket")
def push(
    local_folder: Path,
    bucket: str,
    prefix: str,
    delete: bool,
    comparison: str,
    exclude: tuple[str, ...],
    exclude_from: tuple[Path, ...],
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
        exclude=tuple(_exclude_patterns(exclude, exclude_from)),
        dry_run=dry_run,
        max_delete=math.inf if max_delete is None else max_delete,
        workers=workers,
    )
    printer = ConsolePrinter(local_folder, bucket, options, quiet=quiet)
    _run_sync(
        lambda: sync_to_bucket(local_folder, bucket, make_storage(), options, on_event=printer),
        printer,
        plan_file,
        bucket=bucket,
        local_folder=local_folder,
        options=options,
        direction="to_bucket",
    )


@main.command()
@click.argument("bucket")
@click.argument("local_folder", type=click.Path(file_okay=False, path_type=Path))
@sync_options("from_bucket")
def pull(
    bucket: str,
    local_folder: Path,
    prefix: str,
    delete: bool,
    comparison: str,
    exclude: tuple[str, ...],
    exclude_from: tuple[Path, ...],
    dry_run: bool,
    max_delete: int | None,
    workers: int,
    plan_file: Path | None,
    quiet: bool,
) -> None:
    """Make LOCAL_FOLDER match BUCKET, or a folder of it.

    Only new and changed objects are downloaded, so a sync that is
    interrupted picks up where it stopped when run again. LOCAL_FOLDER is
    created if it does not exist. Each object is downloaded to a temporary
    file, which replaces the local file only once it is complete, and the
    file gets the time of its object.
    """
    options = SyncOptions(
        prefix=prefix,
        delete=delete,
        comparison=comparison,
        exclude=tuple(_exclude_patterns(exclude, exclude_from)),
        dry_run=dry_run,
        max_delete=math.inf if max_delete is None else max_delete,
        workers=workers,
    )
    printer = ConsolePrinter(local_folder, bucket, options, quiet=quiet, direction="from_bucket")
    _run_sync(
        lambda: sync_from_bucket(bucket, local_folder, make_storage(), options, on_event=printer),
        printer,
        plan_file,
        bucket=bucket,
        local_folder=local_folder,
        options=options,
        direction="from_bucket",
    )


def _exclude_patterns(exclude: tuple[str, ...], exclude_from: tuple[Path, ...]) -> list[str]:
    """The patterns of --exclude, then those of each --exclude-from file."""
    patterns = list(exclude)
    for exclude_file in exclude_from:
        try:
            patterns.extend(read_exclude_file(exclude_file))
        except ExcludeFileError as error:
            raise click.BadParameter(
                f'"{exclude_file}", line {error.line_number} ("{error.line}"): {error.reason}',
                param_hint="'--exclude-from'",
            ) from error
    return patterns


def _run_sync(
    sync: Callable[[], list[ActionResult]],
    printer: ConsolePrinter,
    plan_file: Path | None,
    *,
    bucket: str,
    local_folder: Path,
    options: SyncOptions,
    direction: str,
) -> None:
    """Run a sync, write its plan file if asked to, and exit with its exit code."""
    refusal = ""
    exit_code = 0
    try:
        results = sync()
    except SyncRefused as error:
        refusal = error.reason
        results = printer.results
        exit_code = EXIT_REFUSED
        click.echo(f"Stopped before changing anything: {error.reason}", err=True)
    except (AuthError, VirtualBucketError, OSError) as error:
        # An OSError is a local folder that cannot be created or read, or a
        # lost connection, since the errors of requests are OSErrors too
        raise click.ClickException(str(error)) from error
    except Exception as error:
        # A request the Data Proxy refused, such as one for a bucket that
        # does not exist, is an error of the command, not a traceback
        message = request_error_message(error)
        if message is None:
            raise
        raise click.ClickException(message) from error
    else:
        if any(result.status == "failed" for result in results):
            exit_code = EXIT_INCOMPLETE

    if plan_file is not None:
        write_plan_file(
            plan_file,
            plan_to_dict(
                results,
                bucket=bucket,
                local_folder=local_folder,
                options=options,
                direction=direction,
                refusal=refusal,
            ),
        )

    sys.exit(exit_code)


@main.command()
@click.argument("folder", required=False, type=click.Path(file_okay=False, path_type=Path))
@click.option(
    "--bucket",
    help="Name of the bucket. Needed for a folder that is not a virtual bucket yet; for one "
    "that is, it must be the bucket of the folder.",
)
@click.option(
    "--prefix",
    default=None,
    callback=_checked_prefix,
    help='Folder of the bucket that the virtual bucket holds, such as "results". Default: the '
    "folder of the virtual bucket, or the whole bucket for a folder that is not one yet.",
)
@click.option(
    "--page",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    envvar=ENV_PAGE,
    help="The virtualBucketBrowser.html page to show. Default: the copy in this package.",
)
def browse(folder: Path | None, bucket: str | None, prefix: str | None, page: Path | None) -> None:
    """Browse a virtual bucket, download files and make them online only.

    Without FOLDER the window opens on the list of your virtual buckets,
    from which one is opened or a new one made (see the buckets commands).
    With FOLDER it opens that virtual bucket. A folder that is not a
    virtual bucket becomes one of --bucket. A folder that does not exist
    is created, and filled with placeholders from a listing of the bucket.
    """
    if folder is None and (bucket is not None or prefix is not None):
        raise click.UsageError("--bucket and --prefix go with FOLDER.")
    is_new_folder = folder is not None and not folder.is_dir()
    try:
        virtual_bucket = None if folder is None else VirtualBucket.open(folder, bucket, prefix)
        if page is None:
            page = default_page_path()
    except (VirtualBucketError, OSError) as error:
        # An OSError is a folder that cannot be created, read or written
        raise click.ClickException(str(error)) from error
    show_browser(virtual_bucket, make_storage(), page, update_on_ready=is_new_folder)


@main.group()
def buckets() -> None:
    """Manage the list of your virtual buckets: labels, pins and the root folder.

    The list holds the folders directly below the root folder and the
    folders opened before, and is kept in virtual-buckets.json in the
    configuration folder, shared with the EBRAINS MATLAB toolbox.
    """


@buckets.command("list")
def buckets_list() -> None:
    """List the virtual buckets, pinned ones first, then from the last opened."""
    rows = _registry_rows()
    if not rows:
        click.echo("No virtual buckets yet. Make one with browse, or set a root with buckets root.")
        return
    for row in rows:
        marker = "*" if row.pinned else " "
        name = row.label or row.bucket
        where = f"{row.bucket}{' / ' + row.prefix if row.prefix else ''}"
        detail = where if row.label else (row.prefix or "")
        if row.exists:
            status = f"last opened {format_local_time(row.last_opened) or 'never'}"
        else:
            status = "not found"
        click.echo(f"{marker} {name}" + (f"  ({detail})" if detail else ""))
        click.echo(f"    {row.path}  {status}")


@buckets.command("label")
@click.argument("bucket")
@click.argument("label", required=False)
def buckets_label(bucket: str, label: str | None) -> None:
    """Give BUCKET the label LABEL, shown in place of its name; without LABEL, remove it."""
    _update_registry(lambda registry: registry.set_label(bucket, label or ""))


@buckets.command("pin")
@click.argument("folder", type=click.Path(path_type=Path))
def buckets_pin(folder: Path) -> None:
    """Pin the virtual bucket in FOLDER to the top of the list."""
    _set_pinned(folder, True)


@buckets.command("unpin")
@click.argument("folder", type=click.Path(path_type=Path))
def buckets_unpin(folder: Path) -> None:
    """Unpin the virtual bucket in FOLDER."""
    _set_pinned(folder, False)


@buckets.command("forget")
@click.argument("folder", type=click.Path(path_type=Path))
def buckets_forget(folder: Path) -> None:
    """Remove FOLDER from the list. The folder itself is left as it is."""
    path = normalize_path(folder)
    _update_registry(lambda registry: registry.remove_folder(path))


@buckets.command("root")
@click.argument(
    "folder", required=False, type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option("--clear", is_flag=True, help="Remove the root folder.")
def buckets_root(folder: Path | None, clear: bool) -> None:
    """Show, set or clear the folder where new virtual buckets are made.

    The virtual buckets directly below the root are listed whether or not
    they were opened before, and browse proposes a folder below it for a
    new virtual bucket.
    """
    if folder is not None and clear:
        raise click.UsageError("Give FOLDER or --clear, not both.")
    if folder is None and not clear:
        root = _read_registry().root
        click.echo(root if root else "No root folder is set.")
        return
    root = None if clear else normalize_path(folder)
    _update_registry(lambda registry: setattr(registry, "root", root))


def _read_registry() -> Registry:
    try:
        return read_registry()
    except RegistryError as error:
        raise click.ClickException(str(error)) from error


def _registry_rows() -> list[ListedBucket]:
    registry = _read_registry()
    return list_virtual_buckets(registry, root_children(registry.root), bucket_info_of)


def _update_registry(change: Callable[[Registry], object]) -> None:
    try:
        update_registry(change)
    except (RegistryError, OSError) as error:
        raise click.ClickException(str(error)) from error


def _set_pinned(folder: Path, pinned: bool) -> None:
    """Pin or unpin a folder of the list; one below the root that was never opened is recorded."""
    path = normalize_path(folder)
    row = next((row for row in _registry_rows() if row.path == path), None)
    if row is None:
        raise click.ClickException(
            f'"{path}" is not in the list of virtual buckets. Open it with browse first.'
        )
    _update_registry(
        lambda registry: registry.set_pinned(path, pinned, bucket=row.bucket, prefix=row.prefix)
    )


class ConsolePrinter:
    """Prints the events of a sync, one line per action, as the MATLAB toolbox does.

    direction is "to_bucket" for a push and "from_bucket" for a pull.
    """

    def __init__(
        self,
        local_folder: Path,
        bucket: str,
        options: SyncOptions,
        *,
        quiet: bool = False,
        direction: str = "to_bucket",
    ) -> None:
        self.results: list[ActionResult] = []
        self._local_folder = local_folder
        self._options = options
        self._quiet = quiet
        self._is_push = direction == "to_bucket"
        self._copy_action = "upload" if self._is_push else "download"
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
        copies = [r for r in event.results if r.action == self._copy_action]
        # A deletion kept for a case collision is explained by its warning
        deletions = [r for r in event.results if r.action == "delete" and not is_case_collision(r)]
        unchanged = sum(1 for r in event.results if r.reason == "unchanged")
        kept = sum(1 for r in event.results if r.reason == "extraneous" and r.action != "delete")
        placeholders = sum(1 for r in event.results if r.reason == "placeholder")

        if self._is_push:
            self._say(f'Syncing "{self._local_folder}" to {self._where}.')
        else:
            self._say(f'Syncing {self._where} to "{self._local_folder}".')
        self._say(
            f"{len(copies)} file(s) to {self._copy_action} "
            f"({format_size(sum(r.bytes for r in copies))}), "
            f"{len(deletions)} to delete, {unchanged} unchanged."
        )
        if kept and self._is_push:
            self._say(
                f"{kept} object(s) of the bucket are not in the folder and are kept. "
                "Use --delete to delete them."
            )
        elif kept:
            self._say(
                f"{kept} local file(s) are not in the bucket and are kept. "
                "Use --delete to delete them."
            )
        if placeholders and self._is_push:
            self._say(
                f"{placeholders} empty file(s) are placeholders of objects not yet downloaded "
                "and are left alone."
            )
        elif placeholders:
            self._say(
                f"{placeholders} empty file(s) are placeholders of objects removed from the "
                "bucket and are deleted."
            )
        if self._options.dry_run:
            for result in copies + deletions:
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

    def _on_download(self, event: SyncEvent) -> None:
        self._say(
            f"[{event.index}/{event.total}] Download {event.path} ({format_size(event.bytes)})"
        )

    def _on_downloaded(self, event: SyncEvent) -> None:
        pass

    def _on_download_failed(self, event: SyncEvent) -> None:
        click.echo(f"  Failed: {event.path}: {event.message}", err=True)

    def _on_delete(self, event: SyncEvent) -> None:
        self._say(f"[{event.index}/{event.total}] Delete {event.path}")

    def _on_deleted(self, event: SyncEvent) -> None:
        pass

    def _on_delete_failed(self, event: SyncEvent) -> None:
        click.echo(f"  Failed: {event.path}: {event.message}", err=True)

    def _on_finished(self, event: SyncEvent) -> None:
        copied = sum(
            1 for r in event.results if r.action == self._copy_action and r.status == "done"
        )
        deleted = sum(1 for r in event.results if r.action == "delete" and r.status == "done")
        failed = [r for r in event.results if r.status == "failed"]
        self._say(f"Done: {copied} {self._copy_action}ed, {deleted} deleted, {len(failed)} failed.")
        if failed:
            click.echo(
                f"The sync is incomplete: {len(failed)} file(s) failed, for example "
                f'"{failed[0].path}". Run the sync again to retry them.',
                err=True,
            )


def _show_warning(message, category, filename, lineno, file=None, line=None) -> None:  # noqa: B006
    if issubclass(category, SyncWarning):
        click.echo(f"Warning: {message}", err=True)
    else:
        click.echo(f"{category.__name__}: {message}", err=True)
