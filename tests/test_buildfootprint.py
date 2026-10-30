import contextlib
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import buildfootprint as bf


class BuildFootprintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.new = self.root / "new"
        self.new.mkdir()

    def write(self, relative, content=b"a", folder=None):
        path = (folder or self.new) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def cli(self, *args):
        stdout, stderr = StringIO(), StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = bf.main([str(self.new), *map(str, args)])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_empty_directory(self):
        result = bf.scan(self.new, 10)
        self.assertEqual((result["total_bytes"], result["file_count"], result["directory_count"]), (0, 0, 1))
        self.assertEqual(result["by_extension"], [])

    def test_regular_files_and_nested_directories(self):
        self.write("a.js", b"a" * 5)
        self.write("nested/b.CSS", b"a" * 3)
        result = bf.scan(self.new, 10)
        self.assertEqual(result["total_bytes"], 8)
        self.assertEqual(result["directory_count"], 2)
        self.assertEqual(result["by_extension"][0]["extension"], ".js")
        self.assertEqual(result["by_extension"][1]["extension"], ".css")

    def test_hidden_and_extensionless(self):
        self.write(".hidden", b"a")
        self.write("LICENSE", b"abc")
        result = bf.scan(self.new, 10)
        self.assertEqual(result["by_extension"], [{"extension": "[none]", "files": 2, "bytes": 4}])

    def test_largest_sort_tie(self):
        self.write("z.js", b"a" * 5)
        self.write("a.js", b"a" * 5)
        result = bf.scan(self.new, 1)
        self.assertEqual(result["largest_files"], [{"path": "a.js", "bytes": 5}])

    def test_skip_symlink_file(self):
        outside = self.root / "outside"
        outside.write_bytes(b"secret" * 100)
        try:
            (self.new / "link.js").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        result = bf.scan(self.new, 10)
        self.assertEqual(result["file_count"], 0)
        self.assertEqual(result["symlinks_skipped"], 1)

    def test_skip_symlink_directory(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.js").write_bytes(b"secret")
        try:
            (self.new / "linked").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        result = bf.scan(self.new, 10)
        self.assertEqual(result["file_count"], 0)
        self.assertEqual(result["symlinks_skipped"], 1)

    def test_reject_symlink_root(self):
        link = self.root / "link"
        try:
            link.symlink_to(self.new, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        with self.assertRaisesRegex(bf.ScanError, "not a symlink"):
            bf.scan(link, 10)

    def test_reject_file_root(self):
        path = self.write("index.html")
        with self.assertRaises(bf.ScanError):
            bf.scan(path, 10)

    def test_reject_missing_root(self):
        with self.assertRaisesRegex(bf.ScanError, "cannot access"):
            bf.scan(self.root / "missing", 10)

    def test_other_special_entry(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO unavailable")
        os.mkfifo(self.new / "pipe")
        result = bf.scan(self.new, 10)
        self.assertEqual(result["other_skipped"], 1)

    def test_compare_add_remove_resize(self):
        a = {"a.js": 3, "b.js": 2, "same.css": 4}
        b = {"a.js": 8, "new.js": 10, "same.css": 4}
        result = bf.compare(a, b)
        self.assertEqual(result, {"added_files": 1, "removed_files": 1,
                                  "resized_files": 1, "unchanged_size_files": 1,
                                  "delta_bytes": 13, "added_bytes": 10, "removed_bytes": 2})

    def test_compare_same_size_is_unchanged(self):
        self.assertEqual(bf.compare({"a.js": 3}, {"a.js": 3})["unchanged_size_files"], 1)

    def test_parse_sizes(self):
        self.assertEqual(bf.parse_size("1.5KiB"), 1536)
        self.assertEqual(bf.parse_size("2MB"), 2_000_000)
        self.assertEqual(bf.parse_size("0"), 0)
        self.assertEqual(bf.parse_size("1gib"), 1024**3)

    def test_invalid_sizes(self):
        for value in ("-1", "1.3B", "1.001MiB", "1e10", "1TB", "nan", "1GiBextra", "99999999999999999999999"):
            with self.subTest(value=value), self.assertRaises(Exception):
                bf.parse_size(value)

    def test_invalid_top(self):
        for value in ("0", "101", "foo", "1.5"):
            with self.subTest(value=value), self.assertRaises(Exception):
                bf.parse_top(value)

    def test_json_output(self):
        self.write("app.js", b"123")
        code, output, error = self.cli("--json")
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        result = json.loads(output)
        self.assertEqual(result["total_bytes"], 3)
        self.assertEqual(result["largest_files"][0]["path"], "app.js")
        self.assertNotIn("_files", result)

    def test_text_output(self):
        self.write("app.js", b"123")
        code, output, error = self.cli()
        self.assertEqual(code, 0)
        self.assertIn("Total: 3 B", output)
        self.assertEqual(error, "")

    def test_total_budget_exceeded(self):
        self.write("app.js", b"123")
        code, output, _ = self.cli("--max-total", "2B", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["budget_failures"], ["total"])

    def test_file_budget_exceeded(self):
        self.write("app.js", b"123")
        code, output, _ = self.cli("--max-file", "2")
        self.assertEqual(code, 1)
        self.assertIn("file", output)

    def test_equal_budget_passes(self):
        self.write("app.js", b"123")
        self.assertEqual(self.cli("--max-total", "3B", "--max-file", "3B")[0], 0)

    def test_compare_cli(self):
        previous = self.root / "previous"
        previous.mkdir()
        self.write("old.js", b"123", previous)
        self.write("new.js", b"12345")
        code, output, _ = self.cli("--compare", previous, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["comparison"]["delta_bytes"], 2)

    def test_invalid_directory_exit_2(self):
        self.new.rmdir()
        code, output, error = self.cli("--json")
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("cannot access", error)

    def test_content_not_read_or_emitted(self):
        secret = b"very-private-value"
        path = self.write("app.js", secret)
        original = path.read_bytes()
        code, output, error = self.cli()
        self.assertEqual(code, 0)
        self.assertNotIn(secret.decode(), output + error)
        self.assertEqual(path.read_bytes(), original)

    def test_control_characters_escaped(self):
        self.write("\x1b[31m.js", b"abc")
        code, output, _ = self.cli()
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", output)
        self.assertIn("\\u001b", output)

    def test_limit_files(self):
        old = bf.MAX_ENTRIES
        bf.MAX_ENTRIES = 1
        self.addCleanup(setattr, bf, "MAX_ENTRIES", old)
        self.write("one.js")
        self.write("two.js")
        with self.assertRaisesRegex(bf.ScanError, "file limit"):
            bf.scan(self.new, 10)

    def test_limit_directories(self):
        old = bf.MAX_ENTRIES
        bf.MAX_ENTRIES = 1
        self.addCleanup(setattr, bf, "MAX_ENTRIES", old)
        (self.new / "nested").mkdir()
        with self.assertRaisesRegex(bf.ScanError, "directory limit"):
            bf.scan(self.new, 10)


    def test_limit_skipped_symlinks(self):
        target = self.write("target.js", b"secret")
        try:
            for i in range(3):
                (self.new / f"link-{i}").symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        old = bf.MAX_ENTRIES
        bf.MAX_ENTRIES = 3
        self.addCleanup(setattr, bf, "MAX_ENTRIES", old)
        with self.assertRaisesRegex(bf.ScanError, "entry limit"):
            bf.scan(self.new, 10)
        self.assertEqual(target.read_bytes(), b"secret")

    def test_limit_skipped_special_files(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO unavailable")
        for i in range(3):
            os.mkfifo(self.new / f"pipe-{i}")
        old = bf.MAX_ENTRIES
        bf.MAX_ENTRIES = 2
        self.addCleanup(setattr, bf, "MAX_ENTRIES", old)
        with self.assertRaisesRegex(bf.ScanError, "entry limit"):
            bf.scan(self.new, 10)

    def test_entry_limit_across_nested_directories(self):
        self.write("sub/a.js")
        self.write("sub/b.js")
        old = bf.MAX_ENTRIES
        bf.MAX_ENTRIES = 2
        self.addCleanup(setattr, bf, "MAX_ENTRIES", old)
        with self.assertRaisesRegex(bf.ScanError, "entry limit"):
            bf.scan(self.new, 10)

    def test_sorted_reports_do_not_depend_on_creation_order(self):
        self.write("z.js", b"a" * 4)
        self.write("a.js", b"a" * 4)
        self.write("b.css", b"a" * 4)
        report = bf.scan(self.new, 10)
        self.assertEqual([x["path"] for x in report["largest_files"]],
                         ["a.js", "b.css", "z.js"])
        self.assertEqual([x["extension"] for x in report["by_extension"]],
                         [".js", ".css"])

    def test_comparison_budget_checks_current_only(self):
        previous = self.root / "previous"
        previous.mkdir()
        self.write("huge.js", b"x" * 100, previous)
        self.write("tiny.js", b"x")
        code, output, error = self.cli("--compare", previous, "--max-total", "2B", "--json")
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["comparison"]["delta_bytes"], -99)

    def test_missing_comparison_returns_error_without_report(self):
        code, output, error = self.cli("--compare", self.root / "missing", "--json")
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertNotIn(str(self.root), error)

    def test_sparse_file_uses_metadata_not_contents(self):
        path = self.new / "large.js"
        with path.open("wb") as file:
            file.truncate(2 * 1024**3)
        result = bf.scan(self.new, 10)
        self.assertEqual(result["total_bytes"], 2 * 1024**3)
        self.assertEqual(path.stat().st_size, 2 * 1024**3)

    def test_cli_invalid_arguments_exit_2(self):
        import subprocess
        import sys
        for option in (("--top", "0"), ("--max-total", "1.5B"),
                       ("--max-file", "1TB")):
            with self.subTest(option=option):
                proc = subprocess.run(
                    [sys.executable, "-m", "buildfootprint", str(self.new), *option],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, "")
                self.assertIn("error:", proc.stderr)

    def test_filename_newline_is_escaped(self):
        self.write("name\nsecret.js", b"x")
        code, output, error = self.cli()
        self.assertEqual((code, error), (0, ""))
        self.assertNotIn("name\nsecret", output)
        self.assertIn(r"name\nsecret", output)

    def test_json_is_deterministic(self):
        self.write("z.js", b"a" * 3)
        self.write("a.js", b"a" * 3)
        first = self.cli("--json")
        second = self.cli("--json")
        self.assertEqual(first, second)


    def test_nul_in_root_path_is_a_sanitized_error(self):
        secret = "hidden-token"
        with contextlib.redirect_stdout(StringIO()) as output, contextlib.redirect_stderr(StringIO()) as error:
            code = bf.main([secret + "\0suffix", "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("cannot access build directory", error.getvalue())
        self.assertNotIn(secret, error.getvalue())

    def test_nul_in_previous_path_is_a_sanitized_error(self):
        code, output, error = self.cli("--compare", "private\0value", "--json")
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertNotIn("private", error)

    def test_os_error_does_not_disclose_directory_name(self):
        secret = "sensitive-folder"
        with mock.patch.object(Path, "lstat", side_effect=PermissionError(secret)):
            code, output, error = self.cli("--json")
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertNotIn(secret, error)

    def test_scandir_error_does_not_disclose_path(self):
        with mock.patch.object(bf.os, "scandir", side_effect=PermissionError("secret-path")):
            code, output, error = self.cli("--json")
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertNotIn("secret-path", error)

    def test_zero_byte_budget(self):
        self.write("empty.js", b"")
        self.assertEqual(self.cli("--max-total", "0", "--max-file", "0")[0], 0)
        self.write("nonempty.css", b"x")
        code, output, error = self.cli("--max-total", "0", "--max-file", "0", "--json")
        self.assertEqual((code, error), (1, ""))
        self.assertEqual(json.loads(output)["budget_failures"], ["total", "file"])

    def test_scan_does_not_open_file_contents(self):
        self.write("secret.txt", b"hidden-data")
        with mock.patch("builtins.open", side_effect=AssertionError("content opened")):
            result = bf.scan(self.new, 10)
        self.assertEqual(result["total_bytes"], 11)

    def test_comparison_symlink_root_rejected(self):
        link = self.root / "previous-link"
        try:
            link.symlink_to(self.new, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable")
        code, output, error = self.cli("--compare", link, "--json")
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("not a symlink", error)

    def test_json_escapes_terminal_control_in_path(self):
        self.write("private\x1b[31m.js", b"x")
        code, output, error = self.cli("--json")
        self.assertEqual((code, error), (0, ""))
        self.assertNotIn("\x1b", output)
        self.assertEqual(json.loads(output)["largest_files"][0]["path"], "private\x1b[31m.js")

    def test_size_unit_boundaries(self):
        self.assertEqual(bf.parse_size("0.01KB"), 10)
        self.assertEqual(bf.parse_size("9223372036854775807B"), 2**63 - 1)
        with self.assertRaises(Exception):
            bf.parse_size("9223372036854775808B")
        with self.assertRaises(Exception):
            bf.parse_size("0.01KiB")

if __name__ == "__main__":
    unittest.main()
