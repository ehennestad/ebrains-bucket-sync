# ebrains-bucket-sync

Sync a local folder to an EBRAINS Data Proxy bucket, or a bucket to a local folder, the way rsync does: only new and changed files are transferred, so a sync that is interrupted picks up where it stopped when run again. A folder can also stand for a bucket with an empty placeholder for each file not downloaded yet, and be browsed in a window that fetches files one by one.

The sync rules are shared with the `ebrains.bucket.sync` functions of the [EBRAINS MATLAB toolbox](https://github.com/ehennestad/EBRAINS-MATLAB). Both follow the contract in [spec/README.md](https://github.com/ehennestad/ebrains-bucket-sync/blob/main/spec/README.md) and pass the same fixtures in `spec/fixtures`, so a plan is the same whichever tool makes it, and a folder synced or browsed with one can be synced or browsed with the other. This version of the package and version 0.12.0 of the toolbox follow the same contract.

## Install

```bash
pipx install ebrains-bucket-sync
```

No pipx, or using the package from Python? See [Installing ebrains-bucket-sync](https://github.com/ehennestad/ebrains-bucket-sync/blob/main/docs/install.md).

The changes of each release are in [CHANGELOG.md](https://github.com/ehennestad/ebrains-bucket-sync/blob/main/CHANGELOG.md).

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

`--exclude PATTERN` leaves out paths that match a wildcard pattern: `*.tmp` in every folder, `.git/` for that folder wherever it is, `raw/scratch` for that path from the root. Excluded paths are neither uploaded nor deleted, so excluding a path does not remove it from the bucket. The `.ebrains` folder of a virtual bucket and the files of interrupted transfers, whose names end in `.sync-part` or `.multipart_manifest.json`, are never synced. The patterns look like those of a `.gitignore` file but follow simpler rules, listed in [spec/README.md](https://github.com/ehennestad/ebrains-bucket-sync/blob/main/spec/README.md#exclude-patterns); for example, a pattern cannot re-include paths with `!`.

`--exclude-from FILE` reads patterns from a file, one per line, with `#` for comments. [templates/dataset.ignore](https://github.com/ehennestad/ebrains-bucket-sync/blob/main/templates/dataset.ignore) leaves out the files that do not belong in a published dataset, such as `.git/`, `.DS_Store` and `__pycache__/`. Copy it next to your data, add what else to leave out, and pass it to each sync:

```bash
ebrains-bucket-sync push my-dataset my-bucket --exclude-from dataset.ignore
```

`--plan-file plan.json` writes the plan and the outcome as JSON. `--workers N` transfers N files at the same time, 4 by default, and `--quiet` prints only failures.

An upload that fails does not stop the sync. The other files are uploaded, nothing is deleted, the command exits with status 1, and running it again retries the failed files. A sync that stops before changing anything, because the planned deletions look like a mistake or the folder stands for another bucket, exits with status 2.

### Pull

`pull` makes a local folder match a bucket, or a folder of it, with the same options as `push` read the other way: `--delete` deletes the local files the bucket does not have, and `--exclude` leaves local files and objects alike out of the sync.

```bash
ebrains-bucket-sync pull my-bucket data --prefix results --dry-run
ebrains-bucket-sync pull my-bucket data --prefix results
```

The folder is created if it does not exist. Each object is downloaded into a temporary file next to its target, which replaces the target only once the download is complete and has the size of the object, and the file gets the modification time of its object, so a later sync in either direction finds it unchanged. A local file changed after its object was uploaded is kept, as with `push` the other way.

On a file system that ignores the case of names, as the default ones of macOS and Windows do, an object and a local file whose names differ only in case are one file, and objects whose names differ from each other only in case are left out with a warning. A download is refused, as a failed row, when its local path is a folder, lies below a folder that is a symbolic link, or holds something the listing of the folder did not find. A folder that is a symbolic link, or that cannot be read, also holds back all deletions, as a failed download does.

### Virtual buckets

A virtual bucket is a local folder that stands for a bucket, or a folder of one, and holds an empty placeholder file for each object that is not downloaded. The `.ebrains` folder at its root says which bucket it stands for and records what the bucket held when it was last listed. `browse` opens one in a window, where files can be downloaded and made online only again:

```bash
ebrains-bucket-sync browse my-dataset --bucket my-bucket --prefix results
```

A folder that is not a virtual bucket becomes one of `--bucket`, and a folder that does not exist is created and filled with placeholders from a listing of the bucket. `createVirtualBucket` and the virtual bucket browser of the MATLAB toolbox make and open the same folders.

A push of a virtual bucket is refused unless the bucket and `--prefix` are the ones it stands for, leaves the placeholders alone, and never syncs the `.ebrains` folder. A pull deletes the placeholders of objects removed from the bucket. After either, the manifest in `.ebrains` records what the bucket holds.

### From Python

```python
from ebrains_bucket_sync import (
    DeviceFlowAuthenticator,
    EbrainsDriveStorage,
    SyncOptions,
    sync_from_bucket,
    sync_to_bucket,
)

storage = EbrainsDriveStorage(DeviceFlowAuthenticator())
results = sync_to_bucket(
    "results", "my-bucket", storage, SyncOptions(prefix="results", delete=True)
)
for result in results:
    print(result.path, result.action, result.reason, result.status)

results = sync_from_bucket("my-bucket", "data", storage, SyncOptions(prefix="results"))
```

## Authentication

`ebrains-bucket-sync login` logs in, `login --force` logs in again even when a valid login is stored, and `ebrains-bucket-sync logout` forgets the stored login.

The login uses the OAuth device flow with the same OIDC client as the [MATLAB toolbox](https://github.com/ehennestad/EBRAINS-MATLAB), so both tools show up as one application in your EBRAINS account. The tokens are kept in the user's configuration folder, in a file only the user can read, and the access token is renewed from the refresh token without a new login for as long as the refresh token lasts. In CI, set `EBRAINS_BUCKET_SYNC_TOKEN` to an access token instead.

## Development

```bash
git clone https://github.com/ehennestad/ebrains-bucket-sync
cd ebrains-bucket-sync
uv sync --dev
uv run pytest
```

The live tests in `tests/live` run against a real bucket and are skipped unless `EBRAINS_BUCKET_SYNC_TEST_BUCKET` names one. The [Live tests](https://github.com/ehennestad/ebrains-bucket-sync/actions/workflows/live-tests.yml) workflow runs them once a week as a service account, and on demand. Everything else runs offline against an in-memory bucket.
