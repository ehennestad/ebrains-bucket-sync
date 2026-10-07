"""Write a plan and its outcome as JSON, in the format of spec/sync-plan.schema.json."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from .model import ActionResult, SyncOptions
from .paths import normalize_prefix

PLAN_FORMAT = "ebrains-bucket-sync-plan"
PLAN_VERSION = 1


def plan_to_dict(
    results: Sequence[ActionResult],
    *,
    bucket: str,
    local_folder: str | os.PathLike[str],
    options: SyncOptions,
    direction: str = "to_bucket",
    refusal: str = "",
) -> dict[str, Any]:
    return {
        "format": PLAN_FORMAT,
        "version": PLAN_VERSION,
        "direction": direction,
        "bucket": bucket,
        "prefix": normalize_prefix(options.prefix),
        "local_folder": os.fspath(local_folder),
        "policy": {
            "comparison": options.comparison,
            "delete": options.delete,
            "exclude": list(options.exclude),
            "max_delete": None if math.isinf(options.max_delete) else int(options.max_delete),
        },
        "dry_run": options.dry_run,
        "refusal": refusal,
        "actions": [asdict(result) for result in results],
    }


def write_plan_file(path: str | os.PathLike[str], plan: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as file:
        json.dump(plan, file, indent=2)
        file.write("\n")
