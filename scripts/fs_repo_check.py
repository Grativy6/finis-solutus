#!/usr/bin/env python3
"""Check staged Git content for private Finis Solutus ledger artifacts.

This is deliberately narrower than a secret scanner. It examines tracked path
metadata plus JSON/JSONL ledger structure, never ordinary prose. Blob content is
read from the Git index by object ID, so unstaged working-tree changes cannot
alter the publication check.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence


EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_INSPECTION_ERROR = 2

MANIFEST_SCHEMA = "fs-ledger-manifest/1"
GENESIS_SCHEMA = "fs-ledger-genesis/1"
STATE_SCHEMA = "fs-ledger-state/1"
EVENT_SCHEMA = "fs-ledger-event/1"

PRIVATE_LEDGER_DIRECTORIES = {
    ".private-ledger",
    ".private_ledger",
    "dm-ledger",
    "dm-private",
    "dm_ledger",
    "dm_private",
    "npc-ledger",
    "npc-private",
    "npc_ledger",
    "npc_private",
    "private-ledger",
    "private_ledger",
}
GENERIC_PRIVATE_DIRECTORIES = {"private"}
LEDGER_ARTIFACT_NAMES = {
    "events.jsonl",
    "ledger.json",
    "ledger.jsonl",
    "manifest.json",
    "receipts.jsonl",
    "state.json",
    "state.md",
}
PRIVATE_FILE_PREFIXES = ("dm", "npc", "private")
PRIVATE_FILE_STEMS = ("events", "ledger", "manifest", "receipts", "state")
PRIVATE_FILE_SUFFIXES = (".db", ".json", ".jsonl", ".md", ".sqlite", ".sqlite3", ".txt")
OBJECT_ID_PATTERN = re.compile(r"^[0-9a-fA-F]{40,64}$")


class InspectionError(Exception):
    """The staged tree could not be inspected safely."""


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    code: str
    message: str


@dataclass(frozen=True)
class Report:
    checked_paths: int
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_json_object(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "checked_paths": self.checked_paths,
            "findings": [asdict(finding) for finding in self.findings],
        }


@dataclass(frozen=True)
class IndexEntry:
    path: str
    mode: str
    object_id: str


def _run_git(root: Path, *arguments: str, input_bytes: bytes | None = None) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", os.fspath(root), *arguments],
            input=input_bytes,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as error:
        raise InspectionError("Git is unavailable") from error
    if completed.returncode != 0:
        raise InspectionError("Git inspection failed")
    return completed.stdout


def _repository_root(candidate: Path) -> Path:
    output = _run_git(candidate, "rev-parse", "--show-toplevel")
    text = output.decode("utf-8", errors="surrogateescape").strip()
    if not text:
        raise InspectionError("Git returned an empty repository root")
    try:
        return Path(text).resolve(strict=True)
    except OSError as error:
        raise InspectionError("Git returned an unavailable repository root") from error


def _tracked_entries(root: Path) -> list[IndexEntry]:
    raw = _run_git(root, "ls-files", "--cached", "--stage", "-z")
    entries: dict[str, IndexEntry] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            header, encoded_path = record.split(b"\t", 1)
            encoded_mode, encoded_object_id, encoded_stage = header.split(b" ", 2)
            path = encoded_path.decode("utf-8", errors="surrogateescape")
            mode = encoded_mode.decode("ascii")
            object_id = encoded_object_id.decode("ascii")
            stage = encoded_stage.decode("ascii")
        except (ValueError, UnicodeError) as error:
            raise InspectionError("Git returned malformed index metadata") from error

        if stage != "0":
            raise InspectionError("the Git index contains unresolved entries")
        if mode not in {"100644", "100755", "120000", "160000"}:
            raise InspectionError("the Git index contains an unsupported object mode")
        if not OBJECT_ID_PATTERN.fullmatch(object_id):
            raise InspectionError("Git returned an invalid object identifier")
        if path.startswith("/") or ".." in PurePosixPath(path).parts:
            raise InspectionError("Git returned an unsafe tracked path")

        entry = IndexEntry(path=path, mode=mode, object_id=object_id.lower())
        previous = entries.setdefault(path, entry)
        if previous != entry:
            raise InspectionError("the Git index contains conflicting entries")
    return [entries[path] for path in sorted(entries)]


def _read_staged_blob(root: Path, entry: IndexEntry) -> bytes:
    if entry.mode not in {"100644", "100755"}:
        raise InspectionError("a non-file index object was requested as a blob")
    return _run_git(root, "cat-file", "blob", entry.object_id)


def _is_under_campaigns(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(parts) and parts[0].casefold() == "campaigns"


def _looks_like_private_ledger_path(path: str) -> bool:
    parts = tuple(part.casefold() for part in PurePosixPath(path).parts)
    if not parts:
        return False
    directories = parts[:-1]
    filename = parts[-1]

    if any(part in PRIVATE_LEDGER_DIRECTORIES for part in directories):
        return True
    if any(part in GENERIC_PRIVATE_DIRECTORIES for part in directories):
        if filename in LEDGER_ARTIFACT_NAMES:
            return True

    for prefix in PRIVATE_FILE_PREFIXES:
        for stem in PRIVATE_FILE_STEMS:
            bases = (f"{prefix}-{stem}", f"{prefix}_{stem}")
            if filename in bases:
                return True
            if any(filename == base + suffix for base in bases for suffix in PRIVATE_FILE_SUFFIXES):
                return True
    return False


def _private_visibility(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("visibility"), str)
        and value["visibility"].casefold() == "private"
    )


def _private_nested_state(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for key in ("genesis", "genesis_state", "initial_state", "state", "state_after", "state_before"):
        nested = value.get(key)
        if _private_visibility(nested):
            return True
        if isinstance(nested, dict) and nested.get("schema") in {GENESIS_SCHEMA, STATE_SCHEMA}:
            if _private_visibility(nested):
                return True
    return False


def _ledger_kind(value: Any) -> str | None:
    """Return the private ledger artifact kind represented by one JSON object."""

    if not isinstance(value, dict):
        return None
    schema = value.get("schema")
    if schema == MANIFEST_SCHEMA:
        if _private_visibility(value) or _private_nested_state(value):
            return "manifest"
    elif schema == STATE_SCHEMA:
        if _private_visibility(value):
            return "state"
    elif schema == EVENT_SCHEMA:
        if (
            _private_visibility(value)
            or _private_nested_state(value)
            or isinstance(value.get("private_ref"), str)
        ):
            return "event"
    return None


def _parse_json(blob: bytes) -> object | None:
    try:
        return json.loads(blob.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None


def _private_json_kinds(blob: bytes, *, jsonl: bool) -> set[str]:
    kinds: set[str] = set()
    if not jsonl:
        value = _parse_json(blob)
        kind = _ledger_kind(value)
        if kind is not None:
            kinds.add(kind)
        return kinds

    # JSON Lines is defined by literal LF records. Do not treat Unicode line
    # separators, form feeds, or other characters inside JSON strings as rows.
    for raw_line in blob.split(b"\n"):
        if not raw_line.strip():
            continue
        value = _parse_json(raw_line)
        kind = _ledger_kind(value)
        if kind is not None:
            kinds.add(kind)
    return kinds


FINDING_DETAILS = {
    "manifest": ("PRIVATE_LEDGER_MANIFEST", "private fs-ledger manifests must not be tracked"),
    "state": ("PRIVATE_LEDGER_STATE", "private fs-ledger state must not be tracked"),
    "event": ("PRIVATE_LEDGER_EVENT", "private fs-ledger events must not be tracked"),
}


def check_repository(candidate: str | os.PathLike[str] = ".") -> Report:
    """Return deterministic findings for the staged tree containing *candidate*."""

    root = _repository_root(Path(candidate))
    entries = _tracked_entries(root)
    findings: set[Finding] = set()

    for entry in entries:
        if _looks_like_private_ledger_path(entry.path):
            findings.add(
                Finding(
                    path=entry.path,
                    code="PRIVATE_LEDGER_PATH",
                    message="tracked path looks like a private ledger artifact",
                )
            )

        if entry.mode == "120000":
            if _is_under_campaigns(entry.path):
                findings.add(
                    Finding(
                        path=entry.path,
                        code="CAMPAIGN_SYMLINK",
                        message="tracked symlinks are not allowed under campaigns/",
                    )
                )
            continue

        suffix = PurePosixPath(entry.path).suffix.casefold()
        if entry.mode in {"100644", "100755"} and suffix in {".json", ".jsonl"}:
            blob = _read_staged_blob(root, entry)
            for kind in _private_json_kinds(blob, jsonl=suffix == ".jsonl"):
                code, message = FINDING_DETAILS[kind]
                findings.add(Finding(path=entry.path, code=code, message=message))

    return Report(checked_paths=len(entries), findings=tuple(sorted(findings)))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check staged paths and blobs for private Finis Solutus ledger artifacts."
    )
    parser.add_argument("repository", nargs="?", default=".", help="path inside the Git worktree")
    parser.add_argument("--json", action="store_true", help="emit one deterministic JSON object")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = check_repository(args.repository)
    except InspectionError:
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "checked_paths": 0,
                        "findings": [],
                        "error": "repository inspection failed",
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            print("REFUSED: repository inspection failed", file=sys.stderr)
        return EXIT_INSPECTION_ERROR

    if args.json:
        print(json.dumps(report.to_json_object(), sort_keys=True, separators=(",", ":")))
    elif report.ok:
        print(f"BOUNDARY_CHECK_CLEAN: {report.checked_paths} tracked path(s) checked")
    else:
        print(
            f"BOUNDARY_CHECK_FAILED: {len(report.findings)} finding(s) "
            f"across {report.checked_paths} tracked path(s)"
        )
        for finding in report.findings:
            safe_path = json.dumps(finding.path, ensure_ascii=True)
            print(f"{finding.code}: {safe_path}")
    return EXIT_CLEAN if report.ok else EXIT_FINDINGS


if __name__ == "__main__":
    raise SystemExit(main())
