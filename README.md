# ebrains-bucket-sync

Sync a local folder to an EBRAINS Data Proxy bucket, the way rsync does: only new and changed files are uploaded, so a sync that is interrupted picks up where it stopped when run again.

The sync rules are shared with the `ebrains.bucket.sync` functions of the [EBRAINS MATLAB toolbox](https://github.com/ehennestad/EBRAINS-MATLAB). Both follow the contract in [spec/README.md](https://github.com/ehennestad/ebrains-bucket-sync/blob/main/spec/README.md) and pass the same fixtures in `spec/fixtures`, so a plan is the same whichever tool makes it.

## Install

```bash
uv tool install ebrains-bucket-sync
```

or with pipx:

```bash
pipx install ebrains-bucket-sync
```

or, for development:

```bash
git clone https://github.com/ehennestad/ebrains-bucket-sync
cd ebrains-bucket-sync
uv sync --dev
uv run pytest
```

## Use

Log in once. A link opens the EBRAINS login in the browser, and the login is kept for later runs:

```bash
ebrains-bucket-sync login
```

See what a sync would do, then run it:

```bash
ebrains-bucket-sync push results my-bucket --prefix results --dry-run
ebrains-bucket-sync push results my-bucket --prefix results
```

Object names are the paths relative to the local folder. Files the bucket already has, with the same size and uploaded after the local file was last changed, are not sent again. `--comparison Size` ignores the times, and `--comparison Checksum` compares the MD5 of every file of the same size.

`--delete` also deletes the objects that the local folder does not have, which makes the bucket (or the folder of it) an exact mirror. A sync refuses to empty a bucket from an empty folder, and `--max-delete N` stops it before it deletes more than N objects. Nothing is deleted after an upload failed.

`--exclude PATTERN` leaves out paths that match a wildcard pattern, with the rules of a `.gitignore` file: `*.tmp` in every folder, `.git/` for that folder wherever it is, `raw/scratch` for that path from the root. `--plan-file plan.json` writes the plan and the outcome as JSON.

An upload that fails does not stop the sync. The other files are uploaded, nothing is deleted, the command exits with status 1, and running it again retries the failed files.

From Python:

```python
from ebrains_bucket_sync import (
    DeviceFlowAuthenticator,
    EbrainsDriveStorage,
    SyncOptions,
    sync_to_bucket,
)

storage = EbrainsDriveStorage(DeviceFlowAuthenticator())
results = sync_to_bucket(
    "results", "my-bucket", storage, SyncOptions(prefix="results", delete=True)
)
for result in results:
    print(result.path, result.action, result.reason, result.status)
```

## Authentication

The login uses the OAuth device flow with the same OIDC client as the [MATLAB toolbox](https://github.com/ehennestad/EBRAINS-MATLAB), so both tools show up as one application in your EBRAINS account. The tokens are kept in the user's configuration folder, in a file only the user can read, and the access token is renewed from the refresh token without a new login for as long as the refresh token lasts. In CI, set `EBRAINS_BUCKET_SYNC_TOKEN` to an access token instead.

## Development

The live tests in `tests/live` run against a real bucket and are skipped unless `EBRAINS_BUCKET_SYNC_TEST_BUCKET` names one. Everything else runs offline against an in-memory bucket.
