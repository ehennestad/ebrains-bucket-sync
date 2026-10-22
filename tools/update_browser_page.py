"""Copy the virtual bucket browser page of the EBRAINS MATLAB toolbox into this package.

The page is the view of the browse command. It lives in the toolbox, which
shows it in MATLAB, and this package ships a copy taken at one commit of
the toolbox. The copy goes to src/ebrains_bucket_sync/resources, next to
browser-page-source.json, which records the commit and the SHA-256
checksum of the copy. tests/test_browser.py checks the copy against that
checksum, so a copy edited by hand fails the tests.

Usage:
    uv run python tools/update_browser_page.py <commit, branch or tag of EBRAINS-MATLAB>
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPOSITORY = "ehennestad/EBRAINS-MATLAB"
PAGE_IN_TOOLBOX = "src/ebrains/+ebrains/+bucket/+app/resources/virtualBucketBrowser.html"
RESOURCES = Path(__file__).resolve().parent.parent / "src" / "ebrains_bucket_sync" / "resources"
PAGE_FILE = RESOURCES / "virtualBucketBrowser.html"
SOURCE_FILE = RESOURCES / "browser-page-source.json"


def resolve_commit(ref: str) -> str:
    """The full commit id of a commit, branch or tag of the toolbox repository."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{REPOSITORY}/commits/{urllib.parse.quote(ref, safe='')}",
        headers={"Accept": "application/vnd.github.sha"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("ascii").strip()


def fetch_page(commit: str) -> bytes:
    """The bytes of the page at a commit of the toolbox repository."""
    url = (
        f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/"
        f"{urllib.parse.quote(PAGE_IN_TOOLBOX)}"
    )
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read()


def main(ref: str) -> None:
    commit = resolve_commit(ref)
    page = fetch_page(commit)
    PAGE_FILE.write_bytes(page)
    source = {
        "repository": f"https://github.com/{REPOSITORY}",
        "path": PAGE_IN_TOOLBOX,
        "commit": commit,
        "sha256": hashlib.sha256(page).hexdigest(),
    }
    SOURCE_FILE.write_text(json.dumps(source, indent=2) + "\n", encoding="utf-8")
    print(f"Copied the page of {REPOSITORY} at {commit} ({len(page)} bytes).")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
