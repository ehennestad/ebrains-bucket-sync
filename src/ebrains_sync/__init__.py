"""Sync local folders with EBRAINS Data Proxy buckets."""

from importlib.metadata import PackageNotFoundError, version

from .auth import AuthError, DeviceFlowAuthenticator, TokenStore
from .engine import SyncEvent, SyncRefused, sync_to_bucket
from .model import (
    COMPARISONS,
    ActionResult,
    BucketStorage,
    FileEntry,
    PlanItem,
    RemoteObject,
    SyncOptions,
    SyncWarning,
)
from .plan import deletion_refusal, plan_sync
from .storage import EbrainsDriveStorage, StaticToken

try:
    __version__ = version("ebrains-sync")
except PackageNotFoundError:  # running from a checkout that is not installed
    __version__ = "0+unknown"

__all__ = [
    "COMPARISONS",
    "ActionResult",
    "AuthError",
    "BucketStorage",
    "DeviceFlowAuthenticator",
    "EbrainsDriveStorage",
    "FileEntry",
    "PlanItem",
    "RemoteObject",
    "StaticToken",
    "SyncEvent",
    "SyncOptions",
    "SyncRefused",
    "SyncWarning",
    "TokenStore",
    "deletion_refusal",
    "plan_sync",
    "sync_to_bucket",
]
