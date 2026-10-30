# BuildFootprint

**Status: Completed MVP** (SonyDev GitHub Marathon, Day 4)

BuildFootprint reports the logical byte size of files in a local web build directory. It groups files by extension, lists the largest files, and can compare two builds by relative path and file size. It reads file metadata, not file contents.

## Requirements

Python 3.10 or later. No runtime dependencies.

## Install and run

From a checkout:

```sh
python -m buildfootprint dist/
```

Or install the command:

```sh
python -m pip install .
buildfootprint dist/ --top 15
```

Compare two build directories:

```sh
buildfootprint dist/ --compare previous-dist/ --json
```

Fail a size check if the total exceeds 5 MiB or any file exceeds 1 MiB:

```sh
buildfootprint dist/ --max-total 5MiB --max-file 1MiB
```

`B`, `KB`, `MB`, and `GB` use decimal units; `KiB`, `MiB`, and `GiB` use powers of 1024. A size without a suffix is in bytes. Budget checks fail only when the measured size is *greater* than the limit.

The CLI returns exit code `0` for a successful scan within budget, `1` when a size budget is exceeded, and `2` for invalid arguments or scan errors. JSON output includes `total_bytes`, `file_count`, `directory_count`, `by_extension`, `largest_files`, `budget_failures`, and, when comparing, `comparison`.

## Scope and limitations

- Counts logical file lengths (`st_size`), not allocated disk space or compressed transfer size. It does not calculate gzip or Brotli sizes.
- Includes hidden files. Skips symbolic links, sockets, and other non-regular entries; reports their counts. Hard-linked paths are counted separately.
- Compares paths and sizes only. Files with the same path and length are treated as unchanged even if their contents differ.
- Scans at most 200,000 directory entries in total per build, including skipped links and special files. The file and directory limits are also 200,000 each. Entries are processed incrementally rather than sorting each directory in memory. The scanner does not write to build directories.
- Does not guarantee consistent results if files are modified while scanning. It does not follow symlinks encountered during normal traversal, but it is not designed to defend against an adversary changing directory entries concurrently.
- Paths in reports are relative to the scanned directory. Text output escapes control characters in file names. **Reports include file names**, which may themselves contain sensitive information; review them before sharing.

## Tests

```sh
python -m unittest discover -s tests -v
```

Development check (2026-10-30, evening): 45 unit and CLI tests passed on Python 3.13.5 (Linux). Tests cover size budgets, comparison, invalid arguments and paths, skipped symlinks and FIFOs, entry limits, output escaping, metadata-only scanning, and read-only scanning of a sparse file. A wheel was built and the installed CLI was exercised on sample build directories. Other Python versions and Windows have not been tested.

MIT licensed. See [LICENSE](LICENSE).
