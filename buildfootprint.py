"""Inspect file sizes in a local build directory."""

from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any


MAX_ENTRIES = 200_000
MAX_TOP = 100
SIZE_PATTERN = re.compile(r"([0-9]+(?:\.[0-9]{1,2})?)(B|KiB|MiB|GiB|KB|MB|GB)?\Z", re.I)
UNITS = {"B": 1, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3,
         "KB": 1000, "MB": 1000**2, "GB": 1000**3}


class ScanError(Exception):
    """A directory cannot be scanned consistently."""


def parse_size(value: str) -> int:
    match = SIZE_PATTERN.fullmatch(value)
    if match is None:
        raise argparse.ArgumentTypeError("size must be a number followed by B, KB, MB, GB, KiB, MiB or GiB")
    try:
        size = Decimal(match.group(1)) * UNITS[(match.group(2) or "B").upper()]
    except (InvalidOperation, OverflowError):
        raise argparse.ArgumentTypeError("invalid size") from None
    if size != size.to_integral_value() or size > 2**63 - 1:
        raise argparse.ArgumentTypeError("size must be a whole number of bytes within 64-bit range")
    return int(size)


def parse_top(value: str) -> int:
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--top must be an integer from 1 to 100") from None
    if not 1 <= count <= MAX_TOP:
        raise argparse.ArgumentTypeError("--top must be an integer from 1 to 100")
    return count


def extension_for(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return suffix if suffix else "[none]"


def scan(root: Path, top: int) -> dict[str, Any]:
    """Collect metadata for regular files, without following known symlinks."""
    try:
        root_stat = root.lstat()
    except OSError:
        raise ScanError("cannot access build directory") from None
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ScanError("build path must be a directory, not a symlink or file")

    files: dict[str, int] = {}
    dirs = 0
    entries_seen = 0
    skipped_links = 0
    skipped_other = 0
    pending = [(root, "")]
    while pending:
        folder, prefix = pending.pop()
        dirs += 1
        if dirs > MAX_ENTRIES:
            raise ScanError("directory limit exceeded (200000)")
        try:
            # Recheck before opening; concurrent modifications remain unsupported.
            if not stat.S_ISDIR(folder.lstat().st_mode):
                raise ScanError("directory changed during scan")
            # Stream entries: sorting a directory first can allocate unbounded memory.
            with os.scandir(folder) as iterator:
                for entry in iterator:
                    entries_seen += 1
                    rel = prefix + entry.name
                    metadata = entry.stat(follow_symlinks=False)
                    mode = metadata.st_mode
                    if stat.S_ISLNK(mode):
                        skipped_links += 1
                    elif stat.S_ISDIR(mode):
                        pending.append((Path(entry.path), rel + "/"))
                    elif stat.S_ISREG(mode):
                        if metadata.st_size < 0:
                            raise ScanError("invalid file size")
                        files[rel] = metadata.st_size
                        if len(files) > MAX_ENTRIES:
                            raise ScanError("file limit exceeded (200000)")
                    else:
                        skipped_other += 1
                    if entries_seen > MAX_ENTRIES:
                        raise ScanError("entry limit exceeded (200000)")
        except ScanError:
            raise
        except (OSError, ValueError):
            raise ScanError("cannot scan directory (permissions or concurrent changes)") from None

    by_ext: dict[str, dict[str, int]] = defaultdict(lambda: {"files": 0, "bytes": 0})
    for rel, size in files.items():
        item = by_ext[extension_for(rel.rsplit("/", 1)[-1])]
        item["files"] += 1
        item["bytes"] += size
    sorted_ext = sorted(by_ext.items(), key=lambda item: (-item[1]["bytes"], item[0]))
    largest = sorted(files.items(), key=lambda item: (-item[1], item[0]))[:top]
    return {
        "total_bytes": sum(files.values()),
        "file_count": len(files),
        "directory_count": dirs,
        "symlinks_skipped": skipped_links,
        "other_skipped": skipped_other,
        "by_extension": [{"extension": ext, **data} for ext, data in sorted_ext],
        "largest_files": [{"path": path, "bytes": size} for path, size in largest],
        "_files": files,
    }


def compare(previous: dict[str, int], current: dict[str, int]) -> dict[str, Any]:
    old_names = previous.keys()
    new_names = current.keys()
    added = new_names - old_names
    removed = old_names - new_names
    changed = {name for name in old_names & new_names if previous[name] != current[name]}
    return {
        "added_files": len(added),
        "removed_files": len(removed),
        "resized_files": len(changed),
        "unchanged_size_files": len(old_names & new_names) - len(changed),
        "delta_bytes": sum(current.values()) - sum(previous.values()),
        "added_bytes": sum(current[name] for name in added),
        "removed_bytes": sum(previous[name] for name in removed),
    }


def printable(value: str) -> str:
    """Escape terminal control characters in names."""
    return json.dumps(value, ensure_ascii=True)[1:-1]


def human_size(value: int) -> str:
    for unit, divisor in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if abs(value) >= divisor:
            return f"{value / divisor:.2f} {unit}"
    return f"{value} B"


def render_text(result: dict[str, Any]) -> None:
    print(f"Total: {human_size(result['total_bytes'])} ({result['total_bytes']} bytes)")
    print(f"Files: {result['file_count']} | Directories: {result['directory_count']}")
    print(f"Skipped: {result['symlinks_skipped']} symlinks, {result['other_skipped']} other entries")
    print("By extension:")
    for item in result["by_extension"]:
        print(f"  {printable(item['extension'])}: {human_size(item['bytes'])} ({item['files']} {'file' if item['files'] == 1 else 'files'})")
    print("Largest files:")
    for item in result["largest_files"]:
        print(f"  {human_size(item['bytes'])}  {printable(item['path'])}")
    if "comparison" in result:
        c = result["comparison"]
        print(f"Compared with previous build: {c['delta_bytes']:+} bytes")
        print(f"  Added: {c['added_files']} | Removed: {c['removed_files']} | Resized: {c['resized_files']}")
    if result["budget_failures"]:
        print("Budget exceeded: " + ", ".join(result["budget_failures"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="buildfootprint",
        description="Report local web build sizes using file metadata only.",
    )
    parser.add_argument("directory", type=Path, help="build output directory")
    parser.add_argument("--compare", metavar="PREVIOUS", type=Path,
                        help="compare against a previous build directory by path and size")
    parser.add_argument("--top", type=parse_top, default=10, metavar="N",
                        help="show the N largest files (1-100; default: 10)")
    parser.add_argument("--max-total", type=parse_size, metavar="SIZE",
                        help="fail if total size exceeds this budget (e.g. 5MiB)")
    parser.add_argument("--max-file", type=parse_size, metavar="SIZE",
                        help="fail if any file exceeds this budget")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)

    try:
        current = scan(args.directory, args.top)
        previous = scan(args.compare, args.top) if args.compare is not None else None
    except ScanError as exc:
        print(f"buildfootprint: {exc}", file=sys.stderr)
        return 2

    failures = []
    if args.max_total is not None and current["total_bytes"] > args.max_total:
        failures.append("total")
    if args.max_file is not None and any(size > args.max_file for size in current["_files"].values()):
        failures.append("file")
    if previous is not None:
        current["comparison"] = compare(previous["_files"], current["_files"])
    current["budget_failures"] = failures
    del current["_files"]
    if args.json:
        print(json.dumps(current, ensure_ascii=True, indent=2))
    else:
        render_text(current)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
