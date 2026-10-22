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

Patterns are applied to both sides before planning, so excluded files are neither transferred nor deleted. `*` matches any characters except `/`, and `?` one character except `/`. `**` has the meaning it has in a `.gitignore` file: `**/` at the start and `/**/` inside stand for any number of folders, none included, so `**/data` matches `data` at the root as well as `a/data`, and `logs/**/*.log` matches `logs/a.log`; `/**` at the end matches everything below a folder; elsewhere `**` matches any characters, `/` included. A pattern without `/` matches a path segment at any depth, and a matched folder excludes everything below it. A pattern with `/` inside it or in front of it is matched from the root of the synced folder, and also excludes everything below a matched folder, so `/build` excludes the `build` folder at the root but not `sub/build`. A trailing `/` is ignored.

The patterns look like those of a `.gitignore` file but differ from them in these ways:

- A trailing `/` does not limit a pattern to folders: `build/` also excludes a file named `build`.
- `[`, `]` and `\` match themselves, so `*.py[co]` matches only a name that ends in `.py[co]`.
- No pattern re-includes paths.

## Exclude files

An exclude file holds one pattern per line, as UTF-8 text. Lines end with LF, CRLF or CR. A byte order mark at the start of the file and spaces and tabs around a line are removed. Blank lines and lines that start with `#` are skipped; a `#` later in a line is part of the pattern. A line that starts with `!` re-includes paths in a `.gitignore` file, which patterns cannot do, so the file is refused, naming the line, rather than the line skipped. `templates/dataset.ignore` is an exclude file for the files that do not belong in a published dataset.

## Fixtures

`fixtures/plan/*.json`: `policy` (`comparison`, `delete`), `source` and `target` entry lists, and the `expected` plan rows. `modified_time` is ISO 8601 UTC or null; `hash` may be omitted.

`fixtures/exclude/*.json`: `patterns`, `paths`, and `expected_kept`, in order.

`fixtures/exclude-file/*.json`: the `text` of a file, and either the `expected_patterns`, in order, or the `expected_error_line` of a refused file, counted from 1.

`fixtures/refusal/*.json`: a `plan`, `n_source_files`, `max_delete` (null for no limit) and `expected_code` (null for no refusal).

## Plan file

`sync-plan.schema.json` describes the JSON a command writes with `--plan-file`: the policy, and each action with its outcome. `sync-policy.schema.json` describes the policy part on its own.
