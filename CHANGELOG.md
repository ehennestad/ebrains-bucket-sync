# Changelog

The notable changes of each release. The GitHub release of a version carries the same text.

## 0.2.0 (unreleased)

`ebrains-bucket-sync` now syncs in both directions and understands virtual buckets: `pull` makes a local folder match a bucket, `browse` opens a folder that stands for a bucket in a window and fetches files one by one, and `push` leaves the placeholders of such a folder alone. The contract in `spec/README.md` describes virtual buckets, and the EBRAINS MATLAB toolbox 0.12.0 follows the same contract, so a folder made with one tool can be synced or browsed with the other. Existing `push` commands keep working; the exclude patterns `**` and a leading `/` now have their `.gitignore` meaning, listed under **Changed**.

### Added

- `pull BUCKET LOCAL_FOLDER` makes a local folder match a bucket, or a folder of it with `--prefix`, with the options of `push` read the other way. Each object is downloaded into a temporary file next to its target, which replaces the target only once the download is complete and has the size of the object, and the file gets the modification time of its object. A local file changed after its object was uploaded is kept. A download is refused, as a failed row, when its local path is a folder, lies below a symbolic link, or holds something the listing did not find, and a linked or unreadable folder holds back all deletions (6b13540, 2a40a0a).
- On a file system that ignores the case of names, a local file and an object whose names differ only in case are matched as one, and objects whose names differ from each other only in case are left out with a warning. A `push --delete` from such a folder does not delete an object whose name differs only in case from a local file, with a warning, since a pull keeps only one of the two (2a40a0a, https://github.com/ehennestad/ebrains-bucket-sync/pull/33).
- `browse FOLDER` opens a virtual bucket in a window, with a copy of the page shipped in the package. Files can be downloaded, made online only again and opened in the Collaboratory. A folder that is not a virtual bucket becomes one of `--bucket`, and a folder that does not exist is created and filled with placeholders from a listing of the bucket. The browser reads the selected files again before it downloads or empties them, and leaves a file that changed since it was read (0d88ff6, 2c9ab8d, 13bb35c, https://github.com/ehennestad/ebrains-bucket-sync/pull/24).
- `push` of a virtual bucket is refused unless the bucket and `--prefix` are the ones it stands for, leaves the placeholders alone, never uploads an empty placeholder as a new object, and records the listing it made, with the MD5 checksum of each uploaded file, as the manifest in `.ebrains` (8877c9e, 0b9664f, dc9e701, b9d3bb3).
- `--exclude-from FILE` reads exclude patterns from a file in the layout of a `.gitignore` file, and `templates/dataset.ignore` lists the files that do not belong in a published dataset (0e7e130).
- The storage reports the progress of a listing, which the browser shows for buckets of many objects (78f34d3).

### Changed

- `**` next to a `/` and a leading `/` in an exclude pattern have their `.gitignore` meaning: `**/data` matches `data` at any depth, `logs/**/*.log` crosses folders, and `/build` is anchored to the root. The remaining differences from `.gitignore` are listed in `spec/README.md` (aabe3b0, c6a4f4e).
- The `.ebrains` folder of a virtual bucket is never synced, in any case of its name, and neither are the files of interrupted transfers, whose names end in `.sync-part` or `.multipart_manifest.json` (910eaad, 57fb6d2).
- The recommended install is `pipx install ebrains-bucket-sync`; the other ways are in `docs/install.md` (https://github.com/ehennestad/ebrains-bucket-sync/pull/13).

### Fixed

- A downloaded file, and the files of `.ebrains`, get the mode of a new file, or of the file they replace. They were created readable to their owner only, so a folder pulled for a group was unreadable to everyone else in it (https://github.com/ehennestad/ebrains-bucket-sync/pull/28).
- The listing of a bucket, the lookup of a bucket and the requests for signed URLs wait at most the storage's timeout, 120 seconds, instead of forever on a stalled connection (https://github.com/ehennestad/ebrains-bucket-sync/pull/28).
- A multipart upload whose manifest the store no longer knows, after a push of the same file to another bucket or prefix, is sent anew instead of failing on every run until the manifest is deleted by hand (https://github.com/ehennestad/ebrains-bucket-sync/pull/29).
- A pull keeps the manifest entry of a placeholder it did not delete, because the deletions were held back or the deletion failed, so the next push still knows the file for a placeholder (8853a81).
- A pull also keeps the entry of a placeholder that an exclude pattern left out of the sync, as the contract now says, so a later push without the pattern does not upload the empty file as a new object (https://github.com/ehennestad/ebrains-bucket-sync/pull/31).
- The virtual bucket browser no longer writes through symbolic links: "Make online only" refuses a link instead of emptying the file it points to, and an update creates no placeholder through a dangling link. An update whose placeholder a local file is in the way of records the listing all the same and names the path, where it stopped with a raw error and lost the listing. A request the Data Proxy refuses, such as one for a bucket that does not exist, is reported by `push` and `pull` as an error with the status and a hint, and `browse` reports a folder it cannot create or write as an error, both instead of a traceback (https://github.com/ehennestad/ebrains-bucket-sync/pull/32).
- A forced login that is refused, expires or is interrupted leaves the stored login in place (https://github.com/ehennestad/ebrains-bucket-sync/pull/14).
- Object names with a Windows drive as their first segment are left out of a sync, as names that leave the folder are (a74d7f2).
- An error of the local file system during a sync is reported as an error instead of a traceback (0a13c4a).
- The two implementations of the contract had drifted apart in a few rules; the package follows the toolbox where they had (222a789).

### Known limits

- The package needs `ebrains-drive` 0.7.0 exactly, and pip replaces another version in the same environment; pipx and uv avoid this.
- The part uploads of a multipart upload have no timeout, since the library sends them through a session of its own.
- `browse` needs `pywebview`, which is installed with the package.

### Spec

- `spec/README.md` describes virtual buckets: the `.ebrains` folder, placeholders, the manifest, and the file states a browser shows. Both tools pass new fixtures for them in `spec/fixtures` (f60471d, 9ef7f64, ba3cedd, 70d498f, 7be819a).

### Internal

- Live tests of push, pull, virtual buckets and the browser host run against a real bucket once a week as a service account, and on demand (09e43dc, 3202e29, https://github.com/ehennestad/ebrains-bucket-sync/pull/26).

## 0.1.0

The first release: `push` syncs a local folder to a bucket or a folder of one, with `--delete`, `--max-delete`, `--comparison`, `--exclude`, `--dry-run` and `--plan-file`; `login` and `logout` use the OAuth device flow with the OIDC client of the EBRAINS MATLAB toolbox; the plan rules are shared with the toolbox through `spec/README.md` and its fixtures.
