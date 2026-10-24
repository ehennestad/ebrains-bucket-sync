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

Folder placeholders are not entries: an object whose name ends with `/`, whose content type starts with `application/directory`, or below which other objects are named. An object whose name has an empty, `.` or `..` segment, a backslash, or a Windows drive such as `C:` as its first segment, which makes the name absolute on Windows, is left out with a warning.

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

Patterns are applied to both sides before planning, so excluded files are neither transferred nor deleted. `*` matches any characters except `/`, and `?` one character except `/`. `**` has the meaning it has in a `.gitignore` file where it is next to a `/`: `**/` at the start and `/**/` inside stand for any number of folders, none included, so `**/data` matches `data` at the root as well as `a/data`, and `logs/**/*.log` matches `logs/a.log`; `/**` at the end matches everything below a folder; elsewhere `**` matches any characters, `/` included. A pattern without `/` matches a path segment at any depth, and a matched folder excludes everything below it. A pattern with `/` inside it or in front of it is matched from the root of the synced folder, and also excludes everything below a matched folder, so `/build` excludes the `build` folder at the root but not `sub/build`. A trailing `/` is ignored.

The patterns look like those of a `.gitignore` file but differ from them in these ways:

- A trailing `/` does not limit a pattern to folders: `build/` also excludes a file named `build`.
- `[`, `]` and `\` match themselves, so `*.py[co]` matches only a name that ends in `.py[co]`.
- A `**` that is not next to a `/`, as in `data**.csv`, matches any characters, `/` included, where a `.gitignore` file reads it as `*`.
- No pattern re-includes paths.

## Exclude files

An exclude file holds one pattern per line, as UTF-8 text. Lines end with LF, CRLF or CR. A byte order mark at the start of the file and spaces and tabs around a line are removed. Blank lines and lines that start with `#` are skipped; a `#` later in a line is part of the pattern. A line that starts with `!` re-includes paths in a `.gitignore` file, which patterns cannot do, so the file is refused, naming the line, rather than the line skipped. `templates/dataset.ignore` is an exclude file for the files that do not belong in a published dataset.

## Virtual buckets

A virtual bucket is a local folder that stands for a bucket, or for a folder of one, and holds a file for each object: the data of the object once it is downloaded, or an empty placeholder until then. The folder `.ebrains` at its root says which bucket it stands for and what the bucket held when it was last listed.

### Paths

A virtual bucket is laid out as a sync folder with the same prefix: a path is relative to the root, with `/` separators, and names the object `prefix + path`. The prefix is `""` for the whole bucket, or a folder name with a trailing `/` and no leading `/`; `results`, `/results` and `results/` are all the prefix `results/`. A prefix whose folder name is not a safe relative path, as [File entries](#file-entries) defines it, such as `../results` or `C:/results`, is refused before anything is listed or written, by a sync as well: it could not be recorded in `bucket.json`.

### The `.ebrains` folder

| file | content |
|---|---|
| `bucket.json` | The bucket and the prefix, as `virtual-bucket.schema.json` describes. `listed_at` is the time the manifest was last made from a listing, in UTC, or null if it never was. |
| `manifest.csv` | The objects below the prefix at the last listing, one file entry per row. Missing until the first listing. |
| `logs/` | Reserved. |

`manifest.csv` is UTF-8 text without a byte order mark. Lines end with LF. The first line is the header `path,bytes,modified_time,hash`, and each further line is a file entry with the fields of [File entries](#file-entries). `path` is relative to the prefix. `bytes` is a whole number. `modified_time` is written as `YYYY-MM-DDTHH:MM:SSZ`, or left empty where it is unknown, and `hash` is lowercase or empty. A field that holds a comma, a double quote or a line break is put in double quotes, with each double quote in it doubled; no other field is quoted. Rows are sorted by path, in Unicode code point order. Folder placeholders and unsafe names are left out, as for a listing.

An implementation refuses a virtual bucket whose `bucket.json` does not follow `virtual-bucket.schema.json`, or whose `manifest.csv` does not follow this layout: a file that is not UTF-8 text, a first line other than the header, a row without four fields, an empty line, a path that is empty, unsafe or on an earlier row, a `bytes` that is not a whole number, a `modified_time` that is neither empty nor a time in the form above, or a `hash` that is neither empty nor 32 lowercase hexadecimal digits. A reader also takes CR LF as a line end. A row is refused rather than skipped, since a missing entry can let a placeholder replace its object. The error names the file and, for the manifest, the line, counted from 1, where a row that spans lines starts.

A file is written in full to a temporary file in `.ebrains` and then renamed over the old one, so an interrupted write leaves the old file. Files in `.ebrains` that an implementation does not know are left as they are. An implementation refuses a virtual bucket whose `format_version` is greater than the one it knows.

### The `.ebrains` folder is not synced

Before exclude patterns are applied, the path `.ebrains` and every path below it are left out of both sides of every sync, whether or not the folder is a virtual bucket, with the name matched without regard to case. So the folder is never uploaded, downloaded or deleted, and an object named `prefix + ".ebrains/..."` never replaces it, also as `.EBRAINS/...` on a file system that ignores case.

### The files of interrupted transfers are not synced

Before exclude patterns are applied, a path that ends in `.sync-part` or `.multipart_manifest.json` is left out of both sides of every sync, as `.ebrains` is. `<name>.<random>.sync-part` is the file an implementation receives a download in, next to its target, and renames over the target once the download is complete. `<name>.multipart_manifest.json` is the manifest the uploader of the Python package keeps next to a file during a multipart upload. Neither holds data of its own, and a process killed during the transfer leaves it behind. The suffixes are matched as written, in lowercase, on the path itself rather than on its folders: `report.sync-part` is left out even where a user named it so, while `Report.SYNC-PART` and `a.sync-part/b.txt` are synced. Such a path is never uploaded, downloaded or deleted, the manifest of a virtual bucket, made from a listing, does not record it, and a browser of a virtual bucket does not show it, nor an entry of it in a manifest written before this rule.

### Syncing a virtual bucket

When the local folder of a sync has a `bucket.json`, the sync is refused (`OtherBucket`) unless the bucket and the prefix of the sync are those of the file. A refused sync stops before it changes anything. A dry run, which changes nothing, still plans the sync and reports the refusal with the plan.

A placeholder is a local file of 0 bytes whose object has more than 0 bytes in the listing the sync made, or had in the manifest. In a sync to the bucket, the plan row of a placeholder becomes action `none` with reason `placeholder`, whatever the plan gave: its object is neither replaced by the empty file, nor recreated as an empty object where it was removed since the manifest was made. A virtual bucket that was never listed has no manifest, and the listing alone decides.

A placeholder of a removed object is a placeholder that the listing the sync made does not have, so only its manifest entry says it stands for an object. In a sync from the bucket, its plan row becomes action `delete` with reason `placeholder`, whether or not `delete` is set: the folder stands for the bucket, and the file holds no data. These rows count as deletions for the refusal rules. An implementation that makes the manifest from a new listing outside a sync, as a browser of the virtual bucket does, deletes these placeholders too. A sync to the bucket leaves them, as above.

A file downloaded into a virtual bucket gets the `modified_time` of its object, so that the file states and a later sync in either direction find it unchanged.

After a sync the manifest is the listing the sync made, before exclude patterns, and `listed_at` is the time of that listing. A sync to the bucket then adds the entry of each file it uploaded, with the local `bytes`, the MD5 checksum of the uploaded file as `hash` and an unknown `modified_time`, and removes the entry of each object it deleted. It also keeps the manifest entry of each placeholder of a removed object, so that the next sync to the bucket still knows the file for a placeholder. A sync from the bucket keeps the entry of each such placeholder that it did not delete, because the deletions were held back or the deletion failed, as long as the file is still empty. A failed transfer leaves the entry as the listing gave it.

### File states

The state of each path of a virtual bucket, sorted by path:

- Without a manifest, a local file of 0 bytes is `online_only` and any other file is `downloaded`.
- With a manifest:
  1. A path only the manifest has: `online_only`.
  2. A path only the local folder has: `local_only`.
  3. A placeholder: `online_only`.
  4. Otherwise the file is compared with its manifest entry:
     1. Sizes that differ: `differs`.
     2. Times that are both known and differ by no more than 2 seconds: `downloaded`, since a download gets the time of its object.
     3. Otherwise, where the manifest entry has a `hash`, the MD5 checksum of the local file decides: `downloaded` where the two are equal, and `differs` where not. The checksum is computed only for these files. A pushed file has an unknown time in the manifest until the next listing, and the time of its upload after it, which is later than the local file, so its checksum tells whether it was changed since.
     4. Otherwise, times that are both known: `differs`. A local file changed after it was downloaded is later than its object, and its changes exist only there. An unknown time leaves the file `downloaded`, as in a plan.

## Fixtures

`fixtures/plan/*.json`: `policy` (`comparison`, `delete`), `source` and `target` entry lists, and the `expected` plan rows. `modified_time` is ISO 8601 UTC or null; `hash` may be omitted.

`fixtures/exclude/*.json`: `patterns`, `paths`, and `expected_kept`, in order.

`fixtures/exclude-file/*.json`: the `text` of a file, and either the `expected_patterns`, in order, or the `expected_error_line` of a refused file, counted from 1.

`fixtures/refusal/*.json`: a `plan`, `n_source_files`, `max_delete` (null for no limit) and `expected_code` (null for no refusal).

`fixtures/manifest/*.json`: the `text` of a `manifest.csv` and either its `entries`, in order, or the `expected_error_line` of a refused text. Reading the text gives the entries, and writing the entries gives the text.

`fixtures/file-state/*.json`: `local` entries, `manifest` entries (null for no manifest), and the `expected` rows of `path` and `state`. The `hash` of a local entry is the MD5 checksum of the file, where the rules need it.

`fixtures/placeholder/*.json`: the `direction` of a sync (`to_bucket` or `from_bucket`), its `plan`, the `local` entries, the `listing` the sync made and the `manifest` entries (null for no manifest), and the `expected` plan.

`fixtures/manifest-update/*.json`: the `listing` of a sync to the bucket, the entries of the files it `uploaded` and the paths it `deleted`, the `local` entries and the `manifest` entries before the sync (null for no manifest), and the `expected` manifest entries.

`fixtures/never-synced/*.json`: `paths`, and the `expected_kept` paths that a sync may touch, in order: those outside `.ebrains` that are not the files of interrupted transfers.

## Plan file

`sync-plan.schema.json` describes the JSON a command writes with `--plan-file`: the policy, and each action with its outcome. `sync-policy.schema.json` describes the policy part on its own.

`virtual-bucket.schema.json` describes the `bucket.json` file of a virtual bucket.
