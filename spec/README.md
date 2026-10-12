# Sync contract

The rules that decide what a sync does, shared by this package and the `ebrains.bucket.sync` functions of the EBRAINS MATLAB toolbox. The Python planner in `src/ebrains_bucket_sync/plan.py` is the reference implementation. Both implementations must pass the fixtures in `fixtures/`, which is what keeps them in step: a change to the rules is a change to the fixtures first.

## File entries

Each side of a sync is a list of file entries:

| field | meaning |
|---|---|
| `path` | Path relative to the synced folder, with `/` separators. |
| `bytes` | Size of the file. |
| `modified_time` | Time of the last change, in UTC. Unknown where it cannot be read. For an object, the time it was uploaded. |
| `hash` | Lowercase MD5 checksum. Unknown (`""`) where it is not reported, or where it cannot be the checksum of the content: a multipart upload (`<md5>-<parts>`) or an object above 5 GiB. |

Folder placeholders are not entries: an object whose name ends with `/`, whose content type starts with `application/directory`, or below which other objects are named. An object whose name has an empty, `.` or `..` segment, or a backslash, is left out with a warning.

## Plan

`plan(source, target, comparison, delete)` gives one row per path on either side, sorted by path, with `action` (`copy`, `delete`, `none`), `reason` and `bytes`.

For a path of the source:

1. Not in the target: `copy`, reason `new`.
2. Sizes differ: `copy`, reason `size`.
3. `comparison` is `Checksum` and both checksums are known: `copy` with reason `checksum` when they differ, else `none` with reason `unchanged`.
4. `comparison` is `Size`: `none`, `unchanged`.
5. Either time unknown: `none`, `unchanged`.
6. Source time later than target time by more than 2 seconds: `copy`, reason `newer`.
7. Otherwise `none`, `unchanged`.

For a path only the target has: reason `extraneous`, action `delete` when `delete` is set and `none` otherwise. `bytes` is the size of the source file, or of the target file for an extraneous path.

The 2-second tolerance covers the time resolution of FAT file systems and the fraction of a second the listing times lose.

## Refusal

Before anything is changed, a run with planned deletions stops when:

- the source has no files (`EmptySource`): the sync would empty the target, which is more often a wrong folder or prefix than what is wanted;
- the plan deletes more than `max_delete` files (`TooManyDeletions`).

A dry run reports the refusal instead of stopping.

## Execution

Copies run first. Nothing is deleted after a copy failed, since a failure may mean the source was listed wrongly or the connection is lost. A failed copy does not stop the other copies, and is reported with its reason. Running the sync again retries it.

## Exclude patterns

Patterns are applied to both sides before planning, so excluded files are neither transferred nor deleted. `*` matches any characters except `/`, `**` any characters, `?` one character except `/`. A pattern without `/` matches a path segment at any depth, and a matched folder excludes everything below it. A pattern with `/` is matched from the root of the synced folder, and also excludes everything below a matched folder. Leading and trailing `/` are ignored.

## Virtual buckets

A virtual bucket is a local folder that stands for a bucket, or for a folder of one, and holds a file for each object: the data of the object once it is downloaded, or an empty placeholder until then. The folder `.ebrains` at its root says which bucket it stands for and what the bucket held when it was last listed.

### Paths

A virtual bucket is laid out as a sync folder with the same prefix: a path is relative to the root, with `/` separators, and names the object `prefix + path`. The prefix is `""` for the whole bucket, or a folder name with a trailing `/` and no leading `/`; `results`, `/results` and `results/` are all the prefix `results/`.

### The `.ebrains` folder

| file | content |
|---|---|
| `bucket.json` | The bucket and the prefix, as `virtual-bucket.schema.json` describes. `listed_at` is the time the manifest was last made from a listing, in UTC, or null if it never was. |
| `manifest.csv` | The objects below the prefix at the last listing, one file entry per row. Missing until the first listing. |
| `logs/` | Reserved. |

`manifest.csv` is UTF-8 text without a byte order mark. Lines end with LF. The first line is the header `path,bytes,modified_time,hash`, and each further line is a file entry with the fields of [File entries](#file-entries). `path` is relative to the prefix. `bytes` is a whole number. `modified_time` is written as `YYYY-MM-DDTHH:MM:SSZ`, or left empty where it is unknown, and `hash` is lowercase or empty. A field that holds a comma, a double quote or a line break is put in double quotes, with each double quote in it doubled; no other field is quoted. Rows are sorted by path, in Unicode code point order. Folder placeholders and unsafe names are left out, as for a listing.

A file is written in full to a temporary file in `.ebrains` and then renamed over the old one, so an interrupted write leaves the old file. Files in `.ebrains` that an implementation does not know are left as they are. An implementation refuses a virtual bucket whose `format_version` is greater than the one it knows.

### The `.ebrains` folder is not synced

Before exclude patterns are applied, the path `.ebrains` and every path below it are left out of both sides of every sync, whether or not the folder is a virtual bucket. So the folder is never uploaded, downloaded or deleted, and an object named `prefix + ".ebrains/..."` never replaces it.

### Syncing a virtual bucket

When the local folder of a sync has a `bucket.json`, the sync stops before it changes anything unless the bucket and the prefix of the sync are those of the file (`OtherBucket`). A dry run reports this instead of stopping.

A placeholder is a local file of 0 bytes whose manifest entry has more than 0 bytes. In a sync to the bucket, the plan row of a placeholder becomes action `none` with reason `placeholder`, whatever the plan gave: its object is neither replaced by the empty file nor deleted. Without a manifest no file is a placeholder.

After a sync the manifest is the listing the sync made, before exclude patterns, and `listed_at` is the time of that listing. A sync to the bucket then adds the entry of each file it uploaded, with the local `bytes` and `hash` and an unknown `modified_time`, and removes the entry of each object it deleted. A failed transfer leaves the entry as the listing gave it.

### File states

The state of each path of a virtual bucket, sorted by path:

- Without a manifest, a local file of 0 bytes is `online_only` and any other file is `downloaded`.
- With a manifest:
  1. A path only the manifest has: `online_only`.
  2. A path only the local folder has: `local_only`.
  3. A placeholder: `online_only`.
  4. Otherwise the file is compared as in a plan with the manifest entry as source and the local file as target, by `SizeAndTime`: `downloaded` where the plan gives `unchanged`, and `differs` where it gives `size` or `newer`.

## Fixtures

`fixtures/plan/*.json`: `policy` (`comparison`, `delete`), `source` and `target` entry lists, and the `expected` plan rows. `modified_time` is ISO 8601 UTC or null; `hash` may be omitted.

`fixtures/exclude/*.json`: `patterns`, `paths`, and `expected_kept`, in order.

`fixtures/refusal/*.json`: a `plan`, `n_source_files`, `max_delete` (null for no limit) and `expected_code` (null for no refusal).

`fixtures/manifest/*.json`: the `text` of a `manifest.csv` and its `entries`, in order. Reading the text gives the entries, and writing the entries gives the text.

`fixtures/file-state/*.json`: `local` entries, `manifest` entries (null for no manifest), and the `expected` rows of `path` and `state`.

`fixtures/placeholder/*.json`: a `plan` of a sync to the bucket, the `local` entries and the `manifest` entries, and the `expected` plan.

`fixtures/manifest-update/*.json`: the `listing` of a sync to the bucket, the entries of the files it `uploaded` and the paths it `deleted`, and the `expected` manifest entries.

## Plan file

`sync-plan.schema.json` describes the JSON a command writes with `--plan-file`: the policy, and each action with its outcome. `sync-policy.schema.json` describes the policy part on its own.

`virtual-bucket.schema.json` describes the `bucket.json` file of a virtual bucket.
