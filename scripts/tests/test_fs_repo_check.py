from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
CHECKER = SCRIPTS / "fs_repo_check.py"
sys.path.insert(0, str(SCRIPTS))

import fs_repo_check  # noqa: E402


class RepositoryFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        self.git("init", "-q")

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def write(self, path: str, content: str) -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def write_bytes(self, path: str, content: bytes) -> Path:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def track(self, *paths: str) -> None:
        self.git("add", "--", *paths)

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CHECKER), *args],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


class BoundaryCheckTests(RepositoryFixture):
    def test_clean_repository_and_kernel_prose_are_not_scanned(self) -> None:
        self.write(
            "kernel/CURRENT.md",
            "A private fs-ledger-state/1 may describe hidden NPC stats in prose.\n",
        )
        self.write("campaigns/_template/PRIVATE_STATE_BOUNDARY.md", "Keep private state elsewhere.\n")
        self.write(
            "examples/public.json",
            json.dumps({"schema": fs_repo_check.STATE_SCHEMA, "visibility": "public"}),
        )
        self.track("kernel", "campaigns", "examples")

        report = fs_repo_check.check_repository(self.root / "kernel")

        self.assertTrue(report.ok)
        self.assertEqual(report.checked_paths, 3)

    def test_checker_reads_staged_blob_not_worktree_file(self) -> None:
        path = self.write(
            "arbitrary/data.json",
            json.dumps({"schema": fs_repo_check.STATE_SCHEMA, "visibility": "private"}),
        )
        self.track("arbitrary/data.json")
        path.write_text(
            json.dumps({"schema": fs_repo_check.STATE_SCHEMA, "visibility": "public"}),
            encoding="utf-8",
        )

        report = fs_repo_check.check_repository(self.root)

        self.assertEqual([finding.code for finding in report.findings], ["PRIVATE_LEDGER_STATE"])

    def test_unstaged_private_replacement_does_not_change_staged_result(self) -> None:
        path = self.write(
            "arbitrary/data.json",
            json.dumps({"schema": fs_repo_check.STATE_SCHEMA, "visibility": "public"}),
        )
        self.track("arbitrary/data.json")
        path.write_text(
            json.dumps({"schema": fs_repo_check.STATE_SCHEMA, "visibility": "private"}),
            encoding="utf-8",
        )

        self.assertTrue(fs_repo_check.check_repository(self.root).ok)

    def test_private_manifest_with_nested_genesis_is_found_anywhere(self) -> None:
        sentinel = "SENTINEL-PRIVATE-CONTENT"
        self.write(
            "innocent-looking/data.json",
            json.dumps(
                {
                    "schema": fs_repo_check.MANIFEST_SCHEMA,
                    "genesis": {
                        "schema": fs_repo_check.GENESIS_SCHEMA,
                        "visibility": "PRIVATE",
                        "private_note": sentinel,
                    },
                }
            ),
        )
        self.track("innocent-looking/data.json")

        completed = self.run_cli(str(self.root), "--json")

        self.assertEqual(completed.returncode, fs_repo_check.EXIT_FINDINGS)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["findings"][0]["code"], "PRIVATE_LEDGER_MANIFEST")
        self.assertNotIn(sentinel, completed.stdout)
        self.assertEqual(completed.stderr, "")

    def test_private_state_json_is_found_at_arbitrary_path(self) -> None:
        self.write(
            "ordinary/cache.json",
            json.dumps({"schema": fs_repo_check.STATE_SCHEMA, "visibility": "private"}),
        )
        self.track("ordinary/cache.json")

        report = fs_repo_check.check_repository(self.root)

        self.assertEqual(
            [(finding.code, finding.path) for finding in report.findings],
            [("PRIVATE_LEDGER_STATE", "ordinary/cache.json")],
        )

    def test_private_event_jsonl_uses_literal_lf_records(self) -> None:
        event = {
            "schema": fs_repo_check.EVENT_SCHEMA,
            "visibility": "private",
            "summary": "text with a Unicode separator \u2028 inside it",
        }
        public = {"schema": fs_repo_check.EVENT_SCHEMA, "visibility": "public"}
        blob = (
            json.dumps(public).encode()
            + b"\n"
            + json.dumps(event, ensure_ascii=False).encode()
            + b"\r\n"
        )
        self.write_bytes("logs/activity.jsonl", blob)
        self.track("logs/activity.jsonl")

        report = fs_repo_check.check_repository(self.root)

        self.assertEqual(
            [(finding.code, finding.path) for finding in report.findings],
            [("PRIVATE_LEDGER_EVENT", "logs/activity.jsonl")],
        )

    def test_private_event_with_nested_state_is_found(self) -> None:
        event = {
            "schema": fs_repo_check.EVENT_SCHEMA,
            "state_after": {"schema": fs_repo_check.STATE_SCHEMA, "visibility": "private"},
        }
        self.write("records/deltas.jsonl", json.dumps(event) + "\n")
        self.track("records/deltas.jsonl")

        report = fs_repo_check.check_repository(self.root)

        self.assertEqual([finding.code for finding in report.findings], ["PRIVATE_LEDGER_EVENT"])

    def test_private_event_reference_is_found_without_exposing_it(self) -> None:
        sentinel = "private:SENTINEL-do-not-echo"
        event = {"schema": fs_repo_check.EVENT_SCHEMA, "private_ref": sentinel}
        self.write("records/events.jsonl", json.dumps(event) + "\n")
        self.track("records/events.jsonl")

        completed = self.run_cli(str(self.root), "--json")

        self.assertEqual(completed.returncode, fs_repo_check.EXIT_FINDINGS)
        self.assertEqual(
            json.loads(completed.stdout)["findings"][0]["code"],
            "PRIVATE_LEDGER_EVENT",
        )
        self.assertNotIn(sentinel, completed.stdout)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_obvious_private_paths_include_symlink_candidates(self) -> None:
        outside = Path(self.temporary.name) / "SENTINEL-target"
        outside.write_text("do not print this content", encoding="utf-8")
        link = self.root / "campaigns" / "branch" / "private" / "state.json"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)
        self.track("campaigns/branch/private/state.json")

        completed = self.run_cli(str(self.root), "--json")

        self.assertEqual(completed.returncode, fs_repo_check.EXIT_FINDINGS)
        findings = json.loads(completed.stdout)["findings"]
        self.assertEqual(
            [(finding["code"], finding["path"]) for finding in findings],
            [
                ("CAMPAIGN_SYMLINK", "campaigns/branch/private/state.json"),
                ("PRIVATE_LEDGER_PATH", "campaigns/branch/private/state.json"),
            ],
        )
        self.assertNotIn("SENTINEL", completed.stdout)
        self.assertNotIn("do not print", completed.stdout)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks are unavailable")
    def test_all_tracked_campaign_symlinks_are_found_without_reading_target(self) -> None:
        outside = Path(self.temporary.name) / "ordinary-target"
        outside.write_text("private content stays unread", encoding="utf-8")
        link = self.root / "campaigns" / "branch" / "notes"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)
        self.track("campaigns/branch/notes")

        report = fs_repo_check.check_repository(self.root)

        self.assertEqual([finding.code for finding in report.findings], ["CAMPAIGN_SYMLINK"])

    def test_boundary_policy_document_and_untracked_material_are_allowed(self) -> None:
        self.write("docs/PRIVATE_STATE_BOUNDARY.md", "Policy only.\n")
        self.track("docs/PRIVATE_STATE_BOUNDARY.md")
        self.write(
            "untracked.json",
            json.dumps({"schema": fs_repo_check.STATE_SCHEMA, "visibility": "private"}),
        )

        report = fs_repo_check.check_repository(self.root)

        self.assertTrue(report.ok)
        self.assertEqual(report.checked_paths, 1)

    def test_json_output_is_deterministic_and_sorted(self) -> None:
        self.write("z/private_state.json", "{}")
        self.write("a/npc-ledger/state.json", "{}")
        self.track("z", "a")

        first = self.run_cli(str(self.root), "--json")
        second = self.run_cli(str(self.root), "--json")

        self.assertEqual(first.returncode, fs_repo_check.EXIT_FINDINGS)
        self.assertEqual(first.stdout, second.stdout)
        payload = json.loads(first.stdout)
        self.assertEqual(
            [finding["path"] for finding in payload["findings"]],
            ["a/npc-ledger/state.json", "z/private_state.json"],
        )

    def test_clean_exit_and_generic_inspection_error_are_distinct(self) -> None:
        self.write("README.md", "public\n")
        self.track("README.md")
        clean = self.run_cli(str(self.root))
        absent = Path(self.temporary.name) / "SENTINEL-not-a-repository"
        failed = self.run_cli(str(absent), "--json")

        self.assertEqual(clean.returncode, fs_repo_check.EXIT_CLEAN)
        self.assertEqual(clean.stdout, "BOUNDARY_CHECK_CLEAN: 1 tracked path(s) checked\n")
        self.assertEqual(failed.returncode, fs_repo_check.EXIT_INSPECTION_ERROR)
        self.assertNotIn("SENTINEL", failed.stdout)
        self.assertEqual(
            json.loads(failed.stdout),
            {
                "ok": False,
                "checked_paths": 0,
                "findings": [],
                "error": "repository inspection failed",
            },
        )


if __name__ == "__main__":
    unittest.main()
