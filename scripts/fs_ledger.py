#!/usr/bin/env python3
"""Deterministic receipt-ledger helper for Finis Solutus v0.16.

This program applies already-resolved state changes. It does not decide outcomes,
awards, evidence, permission, canon, or certification. Within this helper,
events.jsonl is the replay authority and state.json is a rebuildable projection.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
from contextlib import contextmanager
from decimal import Decimal, Inexact, Rounded, localcontext
from pathlib import Path
from typing import Any, Iterator, Sequence

from fs_math import (
    EXACT_CONTEXT_PRECISION,
    advance_exp,
    apply_fatigue_cost,
    calculate_maxima,
    format_campaign_time,
    level_threshold,
    recover_fatigue,
    to_decimal,
)


TOOL_VERSION = "0.1.0"
GENESIS_SCHEMA = "fs-ledger-genesis/1"
MANIFEST_SCHEMA = "fs-ledger-manifest/1"
PROPOSAL_SCHEMA = "fs-ledger-proposal/1"
EVENT_SCHEMA = "fs-ledger-event/1"
STATE_SCHEMA = "fs-ledger-state/1"
REDUCER_SCHEMA = "fs-ledger-reducer/1"

STAT_KEYS = ("STR", "AGI", "STA", "WIS")
RECORD_LEDGERS = ("conditions", "inventory", "achievements", "open_items")
INACTIVE_STATUSES = {"closed", "resolved", "ended", "inactive", "consumed"}
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PRIVATE_REF_PATTERN = re.compile(r"^private:[0-9a-f]{32}$")

PUBLIC_KERNEL_FIELDS = {"version", "source", "sha256", "commit", "rules_lock"}
PUBLIC_METADATA_FIELDS = {"fixture", "note", "source", "provenance", "authority_note"}
PUBLIC_REF_FIELDS = {
    "scene_id", "sources", "evidence", "correction_of", "migration_from",
    "private_companion_ref",
}
PUBLIC_RECORD_FIELDS = {
    "conditions": {
        "label", "name", "status", "source", "start_time", "expiration_time",
        "modifiers", "effects", "timer", "notes", "severity",
    },
    "inventory": {
        "name", "label", "quantity", "owner", "custodian", "location", "state",
        "condition", "description", "familiarity", "modifications", "quirks", "mass",
        "capacity", "occupied_capacity", "available_capacity", "container", "access",
        "source", "notes", "status",
    },
    "achievements": {
        "label", "name", "accomplishment", "result", "acceptance", "account", "evidence",
        "limits", "time", "place", "related_job", "related_project", "related_thread",
        "linked_receipts", "linked_exp", "linked_title", "linked_standing",
        "linked_competence", "open_consequences", "source", "status", "next_review",
        "notes",
    },
    "open_items": {
        "label", "name", "status", "type", "responsible", "parties", "basis",
        "opened_time", "amount", "object", "scope", "due", "review_condition",
        "dependencies", "partial", "settlement", "evidence", "source", "next_review",
        "notes",
    },
}
PRIVATE_MECHANICS_KEYS = {
    "str", "agi", "sta", "wis", "hp", "rp", "fatigue", "overexertion", "gold",
    "level", "exp", "progression", "unallocated_points", "earned_points", "npc_stats",
    "private_nonce", "state_hash", "head_hash", "maximum_hp", "maximum_rp",
    "maximum_fatigue",
}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}\b"),
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|password)\b\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
)


class LedgerError(Exception):
    exit_code = 2
    label = "SCHEMA"


class StaleHeadError(LedgerError):
    exit_code = 3
    label = "STALE_HEAD"


class InvariantError(LedgerError):
    exit_code = 4
    label = "INVARIANT"


class CorruptionError(LedgerError):
    exit_code = 5
    label = "CORRUPTION"


class LockError(LedgerError):
    exit_code = 6
    label = "LOCKED"


class PrivacyError(LedgerError):
    exit_code = 7
    label = "PRIVACY"


def _checked_math(function: Any, *args: Any, field: str, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except LedgerError:
        raise
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise LedgerError(f"{field} failed numeric validation") from exc


def _canonical_decimal(value: Any, *, field: str) -> str:
    number = _checked_math(to_decimal, value, name=field, field=field)
    if number == 0:
        return "0"
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _decimal(value: Any, *, field: str, nonnegative: bool = False) -> Decimal:
    if isinstance(value, (float, bool)):
        raise LedgerError(f"{field} must be an integer or decimal string")
    number = _checked_math(to_decimal, value, name=field, field=field)
    if nonnegative and number < 0:
        raise InvariantError(f"{field} must be nonnegative")
    return number


def _exact_add(left: Decimal, right: Decimal, *, field: str) -> Decimal:
    try:
        with localcontext() as context:
            context.prec = EXACT_CONTEXT_PRECISION
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            return left + right
    except (ArithmeticError, ValueError) as exc:
        raise LedgerError(f"{field} failed exact numeric validation") from exc


def _integer(value: Any, *, field: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LedgerError(f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise InvariantError(f"{field} must be at least {minimum}")
    return value


def _nonempty(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{field} must be a non-empty string")
    return value.strip()


def _identifier(value: Any, *, field: str) -> str:
    text = _nonempty(value, field=field)
    if not ID_PATTERN.fullmatch(text):
        raise LedgerError(f"{field} contains unsupported characters")
    return text


def _mapping(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LedgerError(f"{field} must be an object")
    return value


def _list(value: Any, *, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise LedgerError(f"{field} must be an array")
    return value


def _keys(obj: dict[str, Any], allowed: set[str], *, field: str) -> None:
    if set(obj) - allowed:
        raise LedgerError(f"{field} has unknown field(s)")


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LedgerError("duplicate JSON key")
        result[key] = value
    return result


def _assert_json_safe(value: Any, *, path: str = "input") -> None:
    if isinstance(value, float):
        raise LedgerError(f"{path} contains a float; use a decimal string")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise LedgerError(f"{path} contains a non-string object key")
            _assert_json_safe(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_json_safe(item, path=f"{path}[{index}]")
    elif not isinstance(value, (str, int, bool, type(None))):
        raise LedgerError(f"{path} contains a value that JSON cannot preserve")


def _reject_float(raw: str) -> Any:
    raise LedgerError("JSON floats are not allowed; use decimal strings")


def _reject_constant(raw: str) -> Any:
    raise LedgerError("non-finite JSON number is not allowed")


def _walk_public_extension(value: Any, *, field: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in PRIVATE_MECHANICS_KEYS:
                raise PrivacyError(f"{field} cannot contain private-mechanics fields")
            _walk_public_extension(item, field=field)
    elif isinstance(value, list):
        for item in value:
            _walk_public_extension(item, field=field)
    elif isinstance(value, str) and any(pattern.search(value) for pattern in SECRET_PATTERNS):
        raise PrivacyError(f"{field} appears to contain credential material")


def _validate_public_extensions(
    *,
    kernel: Any | None = None,
    metadata: Any | None = None,
    refs: Any | None = None,
    records: dict[str, Any] | None = None,
    full_input: Any | None = None,
    allow_record_provenance: bool = True,
) -> None:
    if kernel is not None:
        kernel_map = _mapping(kernel, field="kernel")
        _keys(kernel_map, PUBLIC_KERNEL_FIELDS, field="kernel")
        _walk_public_extension(kernel_map, field="kernel")
    if metadata is not None:
        metadata_map = _mapping(metadata, field="metadata")
        _keys(metadata_map, PUBLIC_METADATA_FIELDS, field="metadata")
        for key, value in metadata_map.items():
            if key == "fixture" and isinstance(value, bool):
                continue
            if not isinstance(value, str) and not (
                isinstance(value, list) and all(isinstance(item, str) for item in value)
            ):
                raise LedgerError("public metadata values must be text, text arrays, or fixture boolean")
        _walk_public_extension(metadata_map, field="metadata")
    if refs is not None:
        refs_map = _mapping(refs, field="refs")
        _keys(refs_map, PUBLIC_REF_FIELDS, field="refs")
        for value in refs_map.values():
            if not isinstance(value, str) and not (
                isinstance(value, list) and all(isinstance(item, str) for item in value)
            ):
                raise LedgerError("public refs values must be strings or arrays of strings")
        _walk_public_extension(refs_map, field="refs")
    if records is not None:
        for ledger, ledger_records in records.items():
            allowed = PUBLIC_RECORD_FIELDS[ledger]
            for record in ledger_records.values():
                record_allowed = set(allowed)
                if allow_record_provenance:
                    record_allowed |= {
                        "_last_receipt", "_opened_revision", "closure_receipt", "closure_reason",
                    }
                _keys(record, record_allowed, field=f"{ledger} record")
                _walk_public_extension(record, field=f"{ledger} record")
    if full_input is not None:
        def scan(value: Any) -> None:
            if isinstance(value, dict):
                for item in value.values():
                    scan(item)
            elif isinstance(value, list):
                for item in value:
                    scan(item)
            elif isinstance(value, str) and any(pattern.search(value) for pattern in SECRET_PATTERNS):
                raise PrivacyError("public input appears to contain credential material")

        scan(full_input)


def load_json_text(text: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_pairs,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except LedgerError:
        raise
    except json.JSONDecodeError as exc:
        raise LedgerError(f"invalid JSON at line {exc.lineno}, column {exc.colno}") from exc


def load_json(path_or_dash: str | Path) -> Any:
    if str(path_or_dash) == "-":
        return load_json_text(sys.stdin.read())
    try:
        return load_json_text(Path(path_or_dash).read_text(encoding="utf-8"))
    except OSError as exc:
        raise LedgerError("cannot read JSON input") from exc


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def content_hash(value: Any) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_fd = os.open(path, flags)
    except OSError:
        return
    try:
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
    finally:
        try:
            os.close(directory_fd)
        except OSError:
            pass


def _atomic_write(path: Path, text: str, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600 if private else 0o644
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        fchmod = getattr(os, "fchmod", None)
        if callable(fchmod):
            fchmod(fd, mode)
        else:
            try:
                os.chmod(temporary, mode)
            except OSError:
                pass
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _inside_git_worktree(path: Path) -> bool:
    lexical = Path(os.path.abspath(os.fspath(path)))
    resolved = path.resolve(strict=False)
    for base in {lexical, resolved}:
        for candidate in (base, *base.parents):
            if (candidate / ".git").exists():
                return True
    return False


@contextmanager
def ledger_lock(root: Path) -> Iterator[None]:
    lock_path = root / "LOCK"
    lock_path.touch(exist_ok=True)
    try:
        os.chmod(lock_path, 0o600)
    except OSError:
        pass
    handle = lock_path.open("r+b")
    try:
        if os.name == "nt":
            import msvcrt

            if lock_path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise LockError("another writer holds this ledger") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise LockError("another writer holds this ledger") from exc
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            handle.close()
        except OSError:
            pass


def _state_for_hash(state: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(state)
    value.pop("state_hash", None)
    value.pop("head_hash", None)
    return value


def state_hash(state: dict[str, Any]) -> str:
    return content_hash(_state_for_hash(state))


def _forbid_npc_progression(value: Any, *, path: str = "state") -> None:
    forbidden = {"level", "exp", "unallocated_points", "earned_points"}
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in forbidden:
                raise InvariantError("NPC ledger cannot contain player progression fields")
            _forbid_npc_progression(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _forbid_npc_progression(item, path=f"{path}[{index}]")


def _validate_stats(value: Any) -> dict[str, int]:
    stats = _mapping(value, field="stats")
    if set(stats) != set(STAT_KEYS):
        raise LedgerError("stats must contain exactly STR, AGI, STA, and WIS")
    return {key: _integer(stats[key], field=f"stats.{key}", minimum=0) for key in STAT_KEYS}


def _normalize_store(store_id: str, value: Any) -> dict[str, Any]:
    store = _mapping(value, field=f"gold.stored.{store_id}")
    allowed = {"amount", "owner", "custodian", "location", "access"}
    _keys(store, allowed, field=f"gold.stored.{store_id}")
    return {
        "amount": _integer(store.get("amount"), field=f"gold.stored.{store_id}.amount", minimum=0),
        "owner": _nonempty(store.get("owner"), field=f"gold.stored.{store_id}.owner"),
        "custodian": _nonempty(store.get("custodian"), field=f"gold.stored.{store_id}.custodian"),
        "location": _nonempty(store.get("location"), field=f"gold.stored.{store_id}.location"),
        "access": _nonempty(store.get("access"), field=f"gold.stored.{store_id}.access"),
    }


def _normalize_gold(value: Any) -> dict[str, Any]:
    gold = _mapping(value, field="gold")
    _keys(gold, {"carried", "stored"}, field="gold")
    stored_raw = _mapping(gold.get("stored", {}), field="gold.stored")
    stored: dict[str, Any] = {}
    for raw_id, raw_store in stored_raw.items():
        store_id = _identifier(raw_id, field="gold store ID")
        stored[store_id] = _normalize_store(store_id, raw_store)
    return {
        "carried": _integer(gold.get("carried"), field="gold.carried", minimum=0),
        "stored": stored,
    }


def _normalize_resources(value: Any) -> dict[str, str]:
    resources = _mapping(value, field="resources")
    required = {"hp", "rp", "fatigue", "overexertion"}
    if set(resources) != required:
        raise LedgerError("resources must contain exactly hp, rp, fatigue, and overexertion")
    normalized = {
        key: _canonical_decimal(resources[key], field=f"resources.{key}")
        for key in required
    }
    for key, rendered in normalized.items():
        if Decimal(rendered) < 0:
            raise InvariantError(f"resources.{key} must be nonnegative")
    if Decimal(normalized["overexertion"]) >= 4:
        raise InvariantError("overexertion remainder must be below 4")
    return normalized


def _normalize_progression(value: Any) -> dict[str, Any]:
    progression = _mapping(value, field="progression")
    allowed = {"level", "exp", "unallocated_points", "earned_points"}
    _keys(progression, allowed, field="progression")
    level = _integer(progression.get("level"), field="progression.level", minimum=1)
    exp = _integer(progression.get("exp"), field="progression.exp", minimum=0)
    threshold = int(_checked_math(level_threshold, level, field="EXP threshold"))
    if exp >= threshold:
        raise InvariantError("starting EXP must be below the current threshold")
    earned = _list(progression.get("earned_points", []), field="progression.earned_points")
    return {
        "level": level,
        "exp": exp,
        "unallocated_points": _integer(
            progression.get("unallocated_points", 0),
            field="progression.unallocated_points",
            minimum=0,
        ),
        "earned_points": copy.deepcopy(earned),
    }


def _normalize_records(value: Any, *, field: str) -> dict[str, Any]:
    records = _mapping(value, field=field)
    normalized: dict[str, Any] = {}
    for raw_id, record in records.items():
        record_id = _identifier(raw_id, field=f"{field} record ID")
        normalized[record_id] = copy.deepcopy(_mapping(record, field=f"{field}.{record_id}"))
    return normalized


def refresh_derived(state: dict[str, Any]) -> None:
    stats = state["stats"]
    maxima = _checked_math(
        calculate_maxima,
        stats["STR"], stats["AGI"], stats["STA"], stats["WIS"],
        field="derived maxima",
    )
    max_values = {
        "hp": _canonical_decimal(maxima.maximum_hp, field="maximum HP"),
        "rp": _canonical_decimal(maxima.maximum_rp, field="maximum RP"),
        "fatigue": _canonical_decimal(maxima.maximum_fatigue, field="maximum Fatigue"),
    }
    over_cap = [
        key for key in ("hp", "rp", "fatigue")
        if Decimal(state["resources"][key]) > Decimal(max_values[key])
    ]
    derived: dict[str, Any] = {
        "maximum": max_values,
        "over_cap": over_cap,
        "stored_gold_total": sum(store["amount"] for store in state["gold"]["stored"].values()),
        "time_display": _checked_math(
            format_campaign_time, state["time_seconds"], field="campaign time"
        ),
    }
    if state["subject"]["kind"] == "player":
        derived["exp_threshold"] = int(_checked_math(
            level_threshold, state["progression"]["level"], field="EXP threshold"
        ))
    state["derived"] = derived


def validate_state(state: dict[str, Any]) -> None:
    if state.get("schema") != STATE_SCHEMA:
        raise CorruptionError("state schema does not match this tool")
    _integer(state.get("revision"), field="state.revision", minimum=0)
    _identifier(state.get("ledger_id"), field="state.ledger_id")
    _identifier(state.get("campaign_id"), field="state.campaign_id")
    _identifier(state.get("branch_id"), field="state.branch_id")
    if state.get("visibility") not in {"public", "private"}:
        raise CorruptionError("state visibility must be public or private")
    if state["visibility"] == "private":
        nonce = state.get("private_nonce")
        if not isinstance(nonce, str) or len(nonce) < 32:
            raise CorruptionError("private state is missing its hash nonce")
        private_ref = state.get("last_private_ref")
        if not isinstance(private_ref, str) or not PRIVATE_REF_PATTERN.fullmatch(private_ref):
            raise CorruptionError("private state is missing its opaque reference")
    elif "private_nonce" in state:
        raise PrivacyError("public state cannot contain a private nonce")
    elif "last_private_ref" in state:
        raise PrivacyError("public state cannot contain a private reference")
    _nonempty(state.get("risk"), field="state.risk")
    subject = _mapping(state.get("subject"), field="state.subject")
    if subject.get("kind") not in {"player", "npc"}:
        raise InvariantError("subject.kind must be player or npc")
    _identifier(subject.get("id"), field="state.subject.id")
    _integer(state.get("time_seconds"), field="state.time_seconds", minimum=0)
    _nonempty(state.get("location"), field="state.location")
    _validate_stats(state.get("stats"))
    resources = _normalize_resources(state.get("resources"))
    if resources != state["resources"]:
        raise CorruptionError("resource decimals are not canonical")
    gold = _normalize_gold(state.get("gold"))
    if gold != state["gold"]:
        raise CorruptionError("Gold state is not canonical")
    for ledger in RECORD_LEDGERS:
        _normalize_records(state.get(ledger), field=ledger)
    if not isinstance(state.get("audit_flags"), list) or not all(
        isinstance(item, str) for item in state["audit_flags"]
    ):
        raise CorruptionError("audit_flags must be an array of strings")
    if not isinstance(state.get("stat_history"), list):
        raise CorruptionError("stat_history must be an array")
    _mapping(state.get("metadata"), field="state.metadata")
    _mapping(state.get("kernel"), field="state.kernel")
    if state["visibility"] == "public":
        _validate_public_extensions(
            kernel=state["kernel"],
            metadata=state["metadata"],
            records={ledger: state[ledger] for ledger in RECORD_LEDGERS},
        )
    if subject["kind"] == "player":
        progression = _normalize_progression(state.get("progression"))
        if progression != state["progression"]:
            raise CorruptionError("progression state is not canonical")
    else:
        if "progression" in state:
            raise InvariantError("NPC state cannot contain progression")
        _forbid_npc_progression(state)
    expected = copy.deepcopy(state)
    refresh_derived(expected)
    if canonical_json(expected.get("derived")) != canonical_json(state.get("derived")):
        raise CorruptionError("derived state does not match authoritative fields")
    if state.get("state_hash") != state_hash(state):
        raise CorruptionError("state hash does not match state content")


def normalize_genesis(raw: Any) -> dict[str, Any]:
    genesis = _mapping(raw, field="genesis")
    _assert_json_safe(genesis, path="genesis")
    allowed = {
        "schema", "ledger_id", "visibility", "campaign_id", "branch_id", "subject",
        "kernel", "mechanics_profile", "time_seconds", "location", "risk", "stats",
        "resources", "progression", "gold", "conditions", "inventory", "achievements",
        "open_items", "audit_flags", "metadata",
    }
    _keys(genesis, allowed, field="genesis")
    if genesis.get("schema") != GENESIS_SCHEMA:
        raise LedgerError(f"genesis.schema must be {GENESIS_SCHEMA}")
    visibility = genesis.get("visibility")
    if visibility not in {"public", "private"}:
        raise LedgerError("visibility must be public or private")
    subject = _mapping(genesis.get("subject"), field="subject")
    _keys(subject, {"kind", "id", "name"}, field="subject")
    kind = subject.get("kind")
    if kind not in {"player", "npc"}:
        raise LedgerError("subject.kind must be player or npc")
    if kind == "npc" and visibility != "private":
        raise PrivacyError("exact NPC mechanics require a private ledger")
    normalized_subject = {
        "kind": kind,
        "id": _identifier(subject.get("id"), field="subject.id"),
        "name": _nonempty(subject.get("name"), field="subject.name"),
    }
    raw_flags = _list(genesis.get("audit_flags", []), field="audit_flags")
    if not all(isinstance(flag, str) and flag.strip() for flag in raw_flags):
        raise LedgerError("audit_flags must contain non-empty strings")
    state: dict[str, Any] = {
        "schema": STATE_SCHEMA,
        "tool_version": TOOL_VERSION,
        "revision": 0,
        "last_receipt": None,
        "ledger_id": _identifier(genesis.get("ledger_id"), field="ledger_id"),
        "visibility": visibility,
        "campaign_id": _identifier(genesis.get("campaign_id"), field="campaign_id"),
        "branch_id": _identifier(genesis.get("branch_id"), field="branch_id"),
        "subject": normalized_subject,
        "kernel": copy.deepcopy(_mapping(genesis.get("kernel", {}), field="kernel")),
        "mechanics_profile": _nonempty(
            genesis.get("mechanics_profile", "fs-v0.16-default"),
            field="mechanics_profile",
        ),
        "time_seconds": _integer(genesis.get("time_seconds"), field="time_seconds", minimum=0),
        "location": _nonempty(genesis.get("location"), field="location"),
        "risk": _nonempty(genesis.get("risk", "Normal"), field="risk"),
        "stats": _validate_stats(genesis.get("stats")),
        "resources": _normalize_resources(genesis.get("resources")),
        "gold": _normalize_gold(genesis.get("gold")),
        "conditions": _normalize_records(genesis.get("conditions", {}), field="conditions"),
        "inventory": _normalize_records(genesis.get("inventory", {}), field="inventory"),
        "achievements": _normalize_records(genesis.get("achievements", {}), field="achievements"),
        "open_items": _normalize_records(genesis.get("open_items", {}), field="open_items"),
        "audit_flags": sorted(set(flag.strip() for flag in raw_flags)),
        "stat_history": [],
        "metadata": copy.deepcopy(_mapping(genesis.get("metadata", {}), field="metadata")),
    }
    if kind == "player":
        state["progression"] = _normalize_progression(genesis.get("progression"))
    elif "progression" in genesis:
        raise InvariantError("NPC genesis cannot contain progression")
    if visibility == "private":
        state["private_nonce"] = secrets.token_hex(32)
        state["last_private_ref"] = f"private:{secrets.token_hex(16)}"
    else:
        _validate_public_extensions(
            kernel=state["kernel"],
            metadata=state["metadata"],
            records={ledger: state[ledger] for ledger in RECORD_LEDGERS},
            full_input=genesis,
            allow_record_provenance=False,
        )
    refresh_derived(state)
    genesis_hash = state_hash(state)
    state["head_hash"] = genesis_hash
    state["state_hash"] = genesis_hash
    if kind == "npc":
        _forbid_npc_progression(state)
    validate_state(state)
    return state


def _manifest_path(root: Path) -> Path:
    return root / "manifest.json"


def _events_path(root: Path) -> Path:
    return root / "events.jsonl"


def _state_path(root: Path) -> Path:
    return root / "state.json"


def load_manifest(root: Path) -> dict[str, Any]:
    try:
        manifest = load_json_text(_manifest_path(root).read_text(encoding="utf-8"))
    except OSError as exc:
        raise LedgerError("ledger manifest is missing") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise CorruptionError("manifest schema does not match this tool")
    return manifest


def load_events(root: Path) -> list[dict[str, Any]]:
    try:
        text = _events_path(root).read_text(encoding="utf-8")
    except OSError as exc:
        raise LedgerError("events.jsonl is missing") from exc
    if text and not text.endswith("\n"):
        raise CorruptionError("events.jsonl is missing its terminal newline")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    events: list[dict[str, Any]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            raise CorruptionError(f"blank event line at {number}")
        event = load_json_text(line)
        if not isinstance(event, dict):
            raise CorruptionError(f"event line {number} is not an object")
        events.append(event)
    return events


def _initial_state(manifest: dict[str, Any]) -> dict[str, Any]:
    state = copy.deepcopy(_mapping(manifest.get("genesis_state"), field="manifest.genesis_state"))
    validate_state(state)
    if manifest.get("genesis_hash") != state["state_hash"]:
        raise CorruptionError("manifest genesis hash does not match genesis state")
    if (
        state["revision"] != 0
        or state["last_receipt"] is not None
        or state["head_hash"] != state["state_hash"]
    ):
        raise CorruptionError("genesis revision, receipt, or head anchor is invalid")
    for field in ("ledger_id", "visibility", "campaign_id", "branch_id", "subject"):
        if manifest.get(field) != state.get(field):
            raise CorruptionError(f"manifest {field} conflicts with genesis state")
    return state


def _validate_proposal(raw: Any, state: dict[str, Any]) -> dict[str, Any]:
    proposal = _mapping(raw, field="proposal")
    _assert_json_safe(proposal, path="proposal")
    allowed = {
        "schema", "receipt_id", "request_id", "kind", "campaign_id", "branch_id",
        "subject_id", "expected_revision", "expected_head", "summary", "elapsed_seconds",
        "location", "ops", "refs",
    }
    _keys(proposal, allowed, field="proposal")
    if proposal.get("schema") != PROPOSAL_SCHEMA:
        raise LedgerError(f"proposal.schema must be {PROPOSAL_SCHEMA}")
    normalized: dict[str, Any] = {
        "schema": PROPOSAL_SCHEMA,
        "receipt_id": _identifier(proposal.get("receipt_id"), field="receipt_id"),
        "request_id": _identifier(proposal.get("request_id"), field="request_id"),
        "kind": _nonempty(proposal.get("kind"), field="kind"),
        "campaign_id": _identifier(proposal.get("campaign_id"), field="campaign_id"),
        "branch_id": _identifier(proposal.get("branch_id"), field="branch_id"),
        "subject_id": _identifier(proposal.get("subject_id"), field="subject_id"),
        "expected_revision": _integer(
            proposal.get("expected_revision"), field="expected_revision", minimum=0
        ),
        "expected_head": _nonempty(proposal.get("expected_head"), field="expected_head"),
        "summary": _nonempty(proposal.get("summary"), field="summary"),
        "elapsed_seconds": _integer(
            proposal.get("elapsed_seconds", 0), field="elapsed_seconds", minimum=0
        ),
        "ops": copy.deepcopy(_list(proposal.get("ops", []), field="ops")),
        "refs": copy.deepcopy(_mapping(proposal.get("refs", {}), field="refs")),
    }
    if normalized["kind"] not in {
        "resolved_block", "allocation", "correction", "migration", "npc_update",
    }:
        raise LedgerError("unsupported receipt kind")
    if normalized["kind"] == "correction" and normalized["elapsed_seconds"] != 0:
        raise InvariantError("corrections consume no campaign time")
    if state["subject"]["kind"] == "npc" and normalized["kind"] not in {
        "npc_update", "correction", "migration",
    }:
        raise InvariantError("private NPC ledger requires npc_update, correction, or migration")
    if state["subject"]["kind"] == "player" and normalized["kind"] == "npc_update":
        raise InvariantError("npc_update cannot target a player ledger")
    if "location" in proposal:
        location = _mapping(proposal["location"], field="location")
        _keys(location, {"value", "path"}, field="location")
        normalized["location"] = {
            "value": _nonempty(location.get("value"), field="location.value"),
            "path": _nonempty(location.get("path"), field="location.path"),
        }
    if state["visibility"] == "public":
        _validate_public_extensions(refs=normalized["refs"], full_input=proposal)
        private_ref = normalized["refs"].get("private_companion_ref")
        if private_ref is not None and (
            not isinstance(private_ref, str) or not PRIVATE_REF_PATTERN.fullmatch(private_ref)
        ):
            raise PrivacyError("private companion references must be opaque private: tokens")
        for index, raw_op in enumerate(normalized["ops"]):
            op = _mapping(raw_op, field=f"ops[{index}]")
            op_name = op.get("op")
            if isinstance(op_name, str) and op_name.endswith(".upsert"):
                prefix = op_name.split(".", 1)[0]
                ledger = {
                    "condition": "conditions",
                    "inventory": "inventory",
                    "achievement": "achievements",
                    "open_item": "open_items",
                }.get(prefix)
                if ledger is not None:
                    value = _mapping(op.get("value"), field=f"ops[{index}].value")
                    if set(value) & {
                        "_last_receipt", "_opened_revision", "closure_receipt", "closure_reason",
                    }:
                        raise InvariantError("record provenance fields are written only by the ledger")
                    _keys(value, PUBLIC_RECORD_FIELDS[ledger], field=f"ops[{index}].value")
                    _walk_public_extension(value, field=f"ops[{index}].value")
    else:
        _forbid_npc_progression(normalized, path="proposal")
    return normalized


def _deep_merge(existing: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(existing)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _op_reason(op: dict[str, Any], *, index: int) -> str:
    return _nonempty(op.get("reason"), field=f"ops[{index}].reason")


def _gold_balance(state: dict[str, Any], account: str) -> int:
    if account == "carried":
        return state["gold"]["carried"]
    if account not in state["gold"]["stored"]:
        raise InvariantError("Gold account does not exist")
    return state["gold"]["stored"][account]["amount"]


def _set_gold_balance(state: dict[str, Any], account: str, amount: int) -> None:
    if amount < 0:
        raise InvariantError("Gold balance cannot fall below zero")
    if account == "carried":
        state["gold"]["carried"] = amount
    else:
        state["gold"]["stored"][account]["amount"] = amount


def _apply_record_op(
    state: dict[str, Any],
    op: dict[str, Any],
    *,
    ledger: str,
    action: str,
    receipt_id: str,
    revision: int,
    index: int,
) -> None:
    record_id = _identifier(op.get("id"), field=f"ops[{index}].id")
    records = state[ledger]
    if action == "upsert":
        value = _mapping(op.get("value"), field=f"ops[{index}].value")
        reserved = {"_last_receipt", "_opened_revision", "closure_receipt", "closure_reason"}
        if set(value) & reserved:
            raise InvariantError("record provenance fields are written only by the ledger")
        if str(value.get("status", "")).lower() in INACTIVE_STATUSES:
            raise InvariantError("use the explicit close operation to close a record")
        existing = records.get(record_id, {})
        if str(existing.get("status", "")).lower() in INACTIVE_STATUSES:
            raise InvariantError("a closed record cannot be silently reopened or overwritten")
        merged = _deep_merge(existing, value)
        merged["_last_receipt"] = receipt_id
        if record_id not in records:
            merged["_opened_revision"] = revision
        records[record_id] = merged
    elif action == "close":
        reason = _op_reason(op, index=index)
        if record_id not in records:
            raise InvariantError(f"cannot close missing {ledger} record")
        if str(records[record_id].get("status", "")).lower() in INACTIVE_STATUSES:
            raise InvariantError("record is already inactive")
        records[record_id]["status"] = "closed"
        records[record_id]["closure_receipt"] = receipt_id
        records[record_id]["closure_reason"] = reason
        records[record_id]["_last_receipt"] = receipt_id
    else:
        raise LedgerError("unsupported record operation")


def apply_proposal(
    state: dict[str, Any], proposal: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if proposal["campaign_id"] != state["campaign_id"]:
        raise StaleHeadError("campaign ID does not match ledger")
    if proposal["branch_id"] != state["branch_id"]:
        raise StaleHeadError("branch ID does not match ledger")
    if proposal["subject_id"] != state["subject"]["id"]:
        raise StaleHeadError("subject ID does not match ledger")
    if proposal["expected_revision"] != state["revision"]:
        raise StaleHeadError("expected revision does not match current revision")
    if proposal["expected_head"] != state["head_hash"]:
        raise StaleHeadError("expected head does not match current head")

    new_state = copy.deepcopy(state)
    next_revision = state["revision"] + 1
    receipt_id = proposal["receipt_id"]
    outcome: dict[str, Any] = {
        "resource_changes": [],
        "gold_changes": [],
        "records_changed": [],
    }
    if state["subject"]["kind"] == "player":
        outcome.update({"levels_gained": 0, "player_points_granted": 0, "exp_awards": []})

    resource_max = state["derived"]["maximum"]
    deferred_stat_changes: list[tuple[str, int, str, str]] = []
    for index, raw_op in enumerate(proposal["ops"]):
        op = _mapping(raw_op, field=f"ops[{index}]")
        op_name = _nonempty(op.get("op"), field=f"ops[{index}].op")

        if op_name in {"hp.change", "rp.change"}:
            _keys(op, {"op", "amount", "reason"}, field=f"ops[{index}]")
            reason = _op_reason(op, index=index)
            resource = "hp" if op_name.startswith("hp") else "rp"
            delta = _decimal(op.get("amount"), field=f"ops[{index}].amount")
            before = Decimal(new_state["resources"][resource])
            after = _exact_add(before, delta, field=f"ops[{index}].amount")
            if after < 0:
                raise InvariantError(f"{resource.upper()} cannot fall below zero")
            if (
                delta > 0
                and after > Decimal(resource_max[resource])
                and proposal["kind"] not in {"correction", "migration"}
            ):
                raise InvariantError(f"{resource.upper()} recovery exceeds its maximum")
            new_state["resources"][resource] = _canonical_decimal(after, field=resource)
            outcome["resource_changes"].append({
                "resource": resource,
                "before": _canonical_decimal(before, field=resource),
                "after": _canonical_decimal(after, field=resource),
                "reason": reason,
            })

        elif op_name == "fatigue.exert":
            _keys(op, {"op", "amount", "reason"}, field=f"ops[{index}]")
            reason = _op_reason(op, index=index)
            result = _checked_math(
                apply_fatigue_cost,
                new_state["resources"]["fatigue"],
                resource_max["fatigue"],
                _decimal(op.get("amount"), field=f"ops[{index}].amount", nonnegative=True),
                new_state["resources"]["overexertion"],
                new_state["resources"]["hp"],
                field=f"ops[{index}] Fatigue cost",
            )
            new_state["resources"]["fatigue"] = _canonical_decimal(result.fatigue, field="fatigue")
            new_state["resources"]["overexertion"] = _canonical_decimal(result.remainder, field="overexertion")
            if result.hp is not None:
                new_state["resources"]["hp"] = _canonical_decimal(result.hp, field="hp")
            outcome["resource_changes"].append({
                "resource": "fatigue",
                "cost": _canonical_decimal(result.assessed_cost, field="cost"),
                "overflow": _canonical_decimal(result.overflow, field="overflow"),
                "assessed_hp_loss": _canonical_decimal(result.assessed_hp_loss, field="assessed_hp_loss"),
                "applied_hp_loss": _canonical_decimal(result.applied_hp_loss or 0, field="applied_hp_loss"),
                "reason": reason,
            })

        elif op_name == "fatigue.recover":
            _keys(op, {"op", "amount", "reason"}, field=f"ops[{index}]")
            reason = _op_reason(op, index=index)
            result = _checked_math(
                recover_fatigue,
                new_state["resources"]["fatigue"],
                resource_max["fatigue"],
                _decimal(op.get("amount"), field=f"ops[{index}].amount", nonnegative=True),
                new_state["resources"]["overexertion"],
                field=f"ops[{index}] Fatigue recovery",
            )
            new_state["resources"]["fatigue"] = _canonical_decimal(result.fatigue, field="fatigue")
            new_state["resources"]["overexertion"] = _canonical_decimal(result.remainder, field="overexertion")
            outcome["resource_changes"].append({
                "resource": "fatigue",
                "requested_recovery": _canonical_decimal(result.requested_recovery, field="requested_recovery"),
                "remainder_recovered": _canonical_decimal(result.remainder_recovered, field="remainder_recovered"),
                "fatigue_recovered": _canonical_decimal(result.fatigue_recovered, field="fatigue_recovered"),
                "unused_recovery": _canonical_decimal(result.unused_recovery, field="unused_recovery"),
                "reason": reason,
            })

        elif op_name == "gold.store.define":
            allowed = {"op", "id", "owner", "custodian", "location", "access", "reason"}
            _keys(op, allowed, field=f"ops[{index}]")
            _op_reason(op, index=index)
            store_id = _identifier(op.get("id"), field=f"ops[{index}].id")
            if store_id == "carried" or store_id in new_state["gold"]["stored"]:
                raise InvariantError("Gold store ID already exists or is reserved")
            new_state["gold"]["stored"][store_id] = {
                "amount": 0,
                "owner": _nonempty(op.get("owner"), field=f"ops[{index}].owner"),
                "custodian": _nonempty(op.get("custodian"), field=f"ops[{index}].custodian"),
                "location": _nonempty(op.get("location"), field=f"ops[{index}].location"),
                "access": _nonempty(op.get("access"), field=f"ops[{index}].access"),
            }
            outcome["gold_changes"].append({"op": op_name, "store": store_id})

        elif op_name in {"gold.receive", "gold.spend", "gold.transfer"}:
            base_allowed = {"op", "amount", "reason"}
            if op_name == "gold.receive":
                allowed = base_allowed | {"to"}
            elif op_name == "gold.spend":
                allowed = base_allowed | {"from"}
            else:
                allowed = base_allowed | {"from", "to"}
            _keys(op, allowed, field=f"ops[{index}]")
            reason = _op_reason(op, index=index)
            amount = _integer(op.get("amount"), field=f"ops[{index}].amount", minimum=1)
            if op_name == "gold.receive":
                destination = _identifier(op.get("to"), field=f"ops[{index}].to")
                _set_gold_balance(new_state, destination, _gold_balance(new_state, destination) + amount)
                outcome["gold_changes"].append({
                    "op": "receive", "amount": amount, "to": destination, "reason": reason,
                })
            elif op_name == "gold.spend":
                source = _identifier(op.get("from"), field=f"ops[{index}].from")
                _set_gold_balance(new_state, source, _gold_balance(new_state, source) - amount)
                outcome["gold_changes"].append({
                    "op": "spend", "amount": amount, "from": source, "reason": reason,
                })
            else:
                source = _identifier(op.get("from"), field=f"ops[{index}].from")
                destination = _identifier(op.get("to"), field=f"ops[{index}].to")
                if source == destination:
                    raise InvariantError("Gold transfer source and destination must differ")
                _set_gold_balance(new_state, source, _gold_balance(new_state, source) - amount)
                _set_gold_balance(new_state, destination, _gold_balance(new_state, destination) + amount)
                outcome["gold_changes"].append({
                    "op": "transfer", "amount": amount, "from": source,
                    "to": destination, "reason": reason,
                })

        elif op_name == "exp.award":
            _keys(op, {"op", "amount", "reason", "earned_points"}, field=f"ops[{index}]")
            if new_state["subject"]["kind"] != "player":
                raise InvariantError("NPCs cannot receive EXP")
            reason = _op_reason(op, index=index)
            award = _integer(op.get("amount"), field=f"ops[{index}].amount", minimum=0)
            result = _checked_math(
                advance_exp,
                new_state["progression"]["level"],
                new_state["progression"]["exp"],
                award,
                field=f"ops[{index}] EXP award",
            )
            earned = _list(op.get("earned_points", []), field=f"ops[{index}].earned_points")
            if len(earned) != result.earned_points_granted:
                raise InvariantError("one supplied Earned Point assignment is required per Level gained")
            new_state["progression"]["level"] = result.level
            new_state["progression"]["exp"] = int(result.exp)
            new_state["progression"]["unallocated_points"] += result.player_points_granted
            for offset, raw_assignment in enumerate(earned, start=1):
                assignment = _mapping(raw_assignment, field=f"ops[{index}].earned_points")
                _keys(assignment, {"stat", "reason"}, field=f"ops[{index}].earned_points")
                stat = assignment.get("stat")
                if stat not in STAT_KEYS:
                    raise LedgerError("Earned Point stat must be STR, AGI, STA, or WIS")
                assignment_reason = _nonempty(assignment.get("reason"), field="Earned Point reason")
                deferred_stat_changes.append((stat, 1, "earned_point", assignment_reason))
                new_state["progression"]["earned_points"].append({
                    "level": result.prior_level + offset,
                    "stat": stat,
                    "reason": assignment_reason,
                    "receipt_id": receipt_id,
                })
            outcome["levels_gained"] += result.levels_gained
            outcome["player_points_granted"] += result.player_points_granted
            outcome["exp_awards"].append({"amount": award, "reason": reason})

        elif op_name == "points.allocate":
            _keys(op, {"op", "amounts", "reason"}, field=f"ops[{index}]")
            if new_state["subject"]["kind"] != "player":
                raise InvariantError("NPCs cannot allocate player points")
            reason = _op_reason(op, index=index)
            amounts = _mapping(op.get("amounts"), field=f"ops[{index}].amounts")
            if set(amounts) - set(STAT_KEYS):
                raise LedgerError("point allocations may name only STR, AGI, STA, and WIS")
            normalized_amounts = {
                stat: _integer(amount, field=f"points.{stat}", minimum=0)
                for stat, amount in amounts.items()
            }
            total = sum(normalized_amounts.values())
            if total <= 0:
                raise InvariantError("point allocation must spend at least one point")
            if total > new_state["progression"]["unallocated_points"]:
                raise InvariantError("point allocation exceeds the available bank")
            new_state["progression"]["unallocated_points"] -= total
            for stat in STAT_KEYS:
                amount = normalized_amounts.get(stat, 0)
                if amount:
                    deferred_stat_changes.append((stat, amount, "player_points", reason))

        elif op_name == "stat.change":
            _keys(op, {"op", "stat", "amount", "source", "reason"}, field=f"ops[{index}]")
            stat = op.get("stat")
            if stat not in STAT_KEYS:
                raise LedgerError("stat.change requires STR, AGI, STA, or WIS")
            amount = _integer(op.get("amount"), field=f"ops[{index}].amount")
            if amount == 0:
                raise InvariantError("stat.change amount cannot be zero")
            source = _nonempty(op.get("source"), field=f"ops[{index}].source")
            reason = _op_reason(op, index=index)
            deferred_stat_changes.append((stat, amount, source, reason))

        elif op_name.startswith(("condition.", "inventory.", "achievement.", "open_item.")):
            prefix, action = op_name.split(".", 1)
            ledger = {
                "condition": "conditions",
                "inventory": "inventory",
                "achievement": "achievements",
                "open_item": "open_items",
            }[prefix]
            if action not in {"upsert", "close"}:
                raise LedgerError("unsupported record operation")
            allowed = {"op", "id", "value"} if action == "upsert" else {"op", "id", "reason"}
            _keys(op, allowed, field=f"ops[{index}]")
            _apply_record_op(
                new_state,
                op,
                ledger=ledger,
                action=action,
                receipt_id=receipt_id,
                revision=next_revision,
                index=index,
            )
            outcome["records_changed"].append({
                "ledger": ledger, "id": op.get("id"), "action": action,
            })

        elif op_name in {"audit.add", "audit.clear"}:
            _keys(op, {"op", "flag", "reason"}, field=f"ops[{index}]")
            _op_reason(op, index=index)
            flag = _nonempty(op.get("flag"), field=f"ops[{index}].flag")
            if op_name == "audit.add":
                if flag not in new_state["audit_flags"]:
                    new_state["audit_flags"].append(flag)
            elif flag in new_state["audit_flags"]:
                new_state["audit_flags"].remove(flag)

        else:
            raise LedgerError("unknown operation")

    for stat, amount, source, reason in deferred_stat_changes:
        resulting = new_state["stats"][stat] + amount
        if resulting < 0:
            raise InvariantError(f"{stat} cannot fall below zero")
        new_state["stats"][stat] = resulting
        new_state["stat_history"].append({
            "stat": stat,
            "delta": amount,
            "source": source,
            "reason": reason,
            "receipt_id": receipt_id,
            "revision": next_revision,
        })

    new_state["time_seconds"] += proposal["elapsed_seconds"]
    if "location" in proposal:
        if proposal["location"]["value"] != state["location"] and not proposal["location"]["path"]:
            raise InvariantError("location change requires a resolved path")
        new_state["location"] = proposal["location"]["value"]
        outcome["location_path"] = proposal["location"]["path"]

    new_state["revision"] = next_revision
    new_state["last_receipt"] = receipt_id
    new_state["audit_flags"] = sorted(set(new_state["audit_flags"]))
    refresh_derived(new_state)
    new_state["state_hash"] = state_hash(new_state)
    return new_state, outcome


def _event_without_hash(event: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(event)
    value.pop("hash", None)
    return value


def enrich_event(
    state_before: dict[str, Any],
    state_after: dict[str, Any],
    proposal: dict[str, Any],
    outcome: dict[str, Any],
    *,
    private_ref: str | None = None,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "schema": EVENT_SCHEMA,
        "reducer": REDUCER_SCHEMA,
        "tool_version": TOOL_VERSION,
        "revision": state_after["revision"],
        "parent_revision": state_before["revision"],
        "receipt_id": proposal["receipt_id"],
        "request_id": proposal["request_id"],
        "request_hash": content_hash(proposal),
        "parent_hash": state_before["head_hash"],
        "state_before": state_before["state_hash"],
        "state_after": state_after["state_hash"],
        "proposal": proposal,
        "outcome": outcome,
    }
    if private_ref is not None:
        event["private_ref"] = private_ref
    event["hash"] = content_hash(_event_without_hash(event))
    return event


def _verify_event_shape(event: dict[str, Any]) -> None:
    required = {
        "schema", "reducer", "tool_version", "revision", "parent_revision", "receipt_id",
        "request_id", "request_hash", "parent_hash", "state_before", "state_after",
        "proposal", "outcome", "hash",
    }
    optional = {"private_ref"}
    if set(event) - required - optional or not required.issubset(event):
        raise CorruptionError("event fields do not match the event schema")
    if event.get("schema") != EVENT_SCHEMA:
        raise CorruptionError("event schema does not match this tool")
    if event.get("reducer") != REDUCER_SCHEMA:
        raise CorruptionError("event reducer is not supported by this tool")
    if event.get("hash") != content_hash(_event_without_hash(event)):
        raise CorruptionError("event hash mismatch")


def load_projection(root: Path) -> dict[str, Any]:
    try:
        projection = load_json_text(_state_path(root).read_text(encoding="utf-8"))
    except OSError as exc:
        raise CorruptionError("state projection is missing") from exc
    if not isinstance(projection, dict):
        raise CorruptionError("state projection is not an object")
    validate_state(projection)
    return projection


def replay(
    root: Path, *, check_projection: bool = True
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = load_manifest(root)
    state = _initial_state(manifest)
    if state["visibility"] == "private" and _inside_git_worktree(root):
        raise PrivacyError("private ledgers must remain outside every Git worktree")
    seen_receipts: set[str] = set()
    seen_requests: set[str] = set()
    events = load_events(root)
    for expected_revision, event in enumerate(events, start=1):
        _verify_event_shape(event)
        if event["revision"] != expected_revision or event["parent_revision"] != expected_revision - 1:
            raise CorruptionError("event revisions are not contiguous")
        try:
            duplicate = event["receipt_id"] in seen_receipts or event["request_id"] in seen_requests
        except TypeError as exc:
            raise CorruptionError("event IDs are malformed") from exc
        if duplicate:
            raise CorruptionError("duplicate receipt or request ID in event chain")
        seen_receipts.add(event["receipt_id"])
        seen_requests.add(event["request_id"])
        if event["parent_hash"] != state["head_hash"] or event["state_before"] != state["state_hash"]:
            raise CorruptionError("event parent does not match the preceding state")
        proposal = _validate_proposal(event["proposal"], state)
        if event["receipt_id"] != proposal["receipt_id"] or event["request_id"] != proposal["request_id"]:
            raise CorruptionError("event envelope IDs conflict with its proposal")
        if event["request_hash"] != content_hash(proposal):
            raise CorruptionError("event request hash mismatch")
        next_state, outcome = apply_proposal(state, proposal)
        if state["visibility"] == "private":
            private_ref = event.get("private_ref")
            if not isinstance(private_ref, str) or not PRIVATE_REF_PATTERN.fullmatch(private_ref):
                raise CorruptionError("private event is missing its opaque reference")
            next_state["last_private_ref"] = private_ref
            next_state["state_hash"] = state_hash(next_state)
        elif "private_ref" in event:
            raise PrivacyError("public event cannot contain a private reference")
        if canonical_json(event["outcome"]) != canonical_json(outcome):
            raise CorruptionError("event outcome does not replay exactly")
        if event["state_after"] != next_state["state_hash"]:
            raise CorruptionError("event state-after hash does not replay exactly")
        next_state["head_hash"] = event["hash"]
        validate_state(next_state)
        state = next_state
    if check_projection:
        projection = load_projection(root)
        if projection["revision"] > state["revision"]:
            raise CorruptionError("state projection is ahead of the event chain; possible tail loss")
        if (
            projection["revision"] == state["revision"]
            and canonical_json(projection) != canonical_json(state)
        ):
            raise CorruptionError("state projection conflicts with the event chain at the same revision")
    return state, events


def initialize(root: Path, raw_genesis: Any) -> dict[str, Any]:
    if root.exists() or root.is_symlink():
        raise LedgerError("ledger path already exists")
    state = normalize_genesis(raw_genesis)
    private = state["visibility"] == "private"
    if private and _inside_git_worktree(root):
        raise PrivacyError("private ledgers must live outside every Git worktree")
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "tool_version": TOOL_VERSION,
        "ledger_id": state["ledger_id"],
        "visibility": state["visibility"],
        "campaign_id": state["campaign_id"],
        "branch_id": state["branch_id"],
        "subject": copy.deepcopy(state["subject"]),
        "genesis_hash": state["state_hash"],
        "genesis_state": copy.deepcopy(state),
        "hash_note": "SHA-256 detects accidental alteration; it is not authentication or certification.",
    }
    root.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(tempfile.mkdtemp(prefix=f".{root.name}.init-", dir=root.parent))
    try:
        try:
            os.chmod(temporary_root, 0o700 if private else 0o755)
        except OSError:
            pass
        _atomic_write(_manifest_path(temporary_root), pretty_json(manifest), private=private)
        _atomic_write(_events_path(temporary_root), "", private=private)
        _atomic_write(_state_path(temporary_root), pretty_json(state), private=private)
        _fsync_directory(temporary_root)
        os.replace(temporary_root, root)
        _fsync_directory(root.parent)
    except Exception:
        if temporary_root.exists():
            shutil.rmtree(temporary_root, ignore_errors=True)
        raise
    return state


def _find_existing(events: list[dict[str, Any]], proposal: dict[str, Any]) -> dict[str, Any] | None:
    request_hash = content_hash(proposal)
    for event in events:
        if event["request_id"] == proposal["request_id"] or event["receipt_id"] == proposal["receipt_id"]:
            if (
                event["request_id"] == proposal["request_id"]
                and event["receipt_id"] == proposal["receipt_id"]
                and event["request_hash"] == request_hash
            ):
                return event
            raise InvariantError("receipt/request ID was already used for different content")
    return None


def apply_to_ledger(
    root: Path, raw_proposal: Any, *, dry_run: bool = False
) -> tuple[dict[str, Any], dict[str, Any], str]:
    if not root.is_dir():
        raise LedgerError("ledger directory does not exist")
    with ledger_lock(root):
        state, events = replay(root)
        proposal = _validate_proposal(raw_proposal, state)
        existing = _find_existing(events, proposal)
        if existing is not None:
            if not dry_run:
                try:
                    _atomic_write(
                        _state_path(root),
                        pretty_json(state),
                        private=state["visibility"] == "private",
                    )
                except Exception:
                    return state, existing, "already_applied_projection_stale"
            return state, existing, "already_applied"
        new_state, outcome = apply_proposal(state, proposal)
        private_ref = (
            f"private:{secrets.token_hex(16)}" if state["visibility"] == "private" else None
        )
        if private_ref is not None:
            new_state["last_private_ref"] = private_ref
            new_state["state_hash"] = state_hash(new_state)
        event = enrich_event(state, new_state, proposal, outcome, private_ref=private_ref)
        new_state["head_hash"] = event["hash"]
        validate_state(new_state)
        if dry_run:
            return new_state, event, "dry_run"
        private = state["visibility"] == "private"
        try:
            old_text = _events_path(root).read_text(encoding="utf-8")
        except OSError as exc:
            raise LedgerError("events.jsonl is missing") from exc
        new_text = old_text + canonical_json(event) + "\n"
        _atomic_write(_events_path(root), new_text, private=private)
        try:
            _atomic_write(_state_path(root), pretty_json(new_state), private=private)
        except Exception:
            return new_state, event, "committed_projection_stale"
        return new_state, event, "applied"


def verify_ledger(root: Path) -> tuple[dict[str, Any], int]:
    if not root.is_dir():
        raise LedgerError("ledger directory does not exist")
    with ledger_lock(root):
        state, events = replay(root)
        projection = load_projection(root)
        if canonical_json(projection) != canonical_json(state):
            raise CorruptionError("state projection is stale or conflicts with the event chain")
        return state, len(events)


def read_ledger(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not root.is_dir():
        raise LedgerError("ledger directory does not exist")
    with ledger_lock(root):
        return replay(root)


def rebuild_projection(root: Path) -> dict[str, Any]:
    if not root.is_dir():
        raise LedgerError("ledger directory does not exist")
    with ledger_lock(root):
        state, _events = replay(root, check_projection=False)
        try:
            valid_projection = load_projection(root)
        except LedgerError:
            valid_projection = None
        if valid_projection is not None:
            if valid_projection["revision"] > state["revision"]:
                raise CorruptionError(
                    "state projection is ahead of the event chain; refusing possible tail loss"
                )
            if (
                valid_projection["revision"] == state["revision"]
                and canonical_json(valid_projection) != canonical_json(state)
            ):
                raise CorruptionError(
                    "valid state projection records a divergent history at this revision"
                )
        try:
            raw_projection = load_json_text(_state_path(root).read_text(encoding="utf-8"))
        except (LedgerError, OSError, UnicodeError):
            raw_projection = None
        if valid_projection is None and (
            isinstance(raw_projection, dict)
            and isinstance(raw_projection.get("revision"), int)
            and not isinstance(raw_projection.get("revision"), bool)
            and raw_projection["revision"] > state["revision"]
        ):
            raise CorruptionError(
                "state projection is ahead of the event chain; refusing possible tail loss"
            )
        _atomic_write(
            _state_path(root), pretty_json(state), private=state["visibility"] == "private"
        )
        return state


def _active_labels(records: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for record_id, record in records.items():
        status = str(record.get("status", "active")).lower()
        if status in INACTIVE_STATUSES:
            continue
        labels.append(str(record.get("label", record.get("name", record_id))))
    return labels


def player_footer(state: dict[str, Any]) -> str:
    if state["subject"]["kind"] != "player":
        raise PrivacyError("exact NPC mechanics are not player-facing")
    stats = state["stats"]
    resources = state["resources"]
    maximum = state["derived"]["maximum"]
    progression = state["progression"]
    stores = state["gold"]["stored"]
    stored_parts = [
        f"{store['amount']} @ {store['location']} ({store['access']})"
        for store in stores.values()
        if store["amount"] or stores
    ]
    stored_detail = "; ".join(stored_parts) if stored_parts else "None"
    conditions = _active_labels(state["conditions"])
    open_items = _active_labels(state["open_items"])
    lines = [
        f"STATE r{state['revision']} · {state['branch_id']} · {state['subject']['id']}",
        f"Location: {state['location']}",
        f"Stats: STR {stats['STR']} │ AGI {stats['AGI']} │ STA {stats['STA']} │ WIS {stats['WIS']}",
        "Resources: "
        f"HP {resources['hp']}/{maximum['hp']} │ RP {resources['rp']}/{maximum['rp']} │ "
        f"Fatigue {resources['fatigue']}/{maximum['fatigue']} │ Overexertion {resources['overexertion']}/4",
        f"Level {progression['level']} │ EXP {progression['exp']}/{state['derived']['exp_threshold']} │ "
        f"Unallocated Points {progression['unallocated_points']}",
        f"Gold: {state['gold']['carried']} carried │ {state['derived']['stored_gold_total']} stored [{stored_detail}]",
        f"Conditions: {', '.join(conditions) if conditions else 'None'}",
        f"Open: {', '.join(open_items) if open_items else 'None'}",
        f"Audit: {', '.join(state['audit_flags']) if state['audit_flags'] else 'None'}",
        f"Last Applied Receipt: {state['last_receipt'] or 'None'}",
        f"Risk: {state['risk']}",
        f"Current Time: {state['derived']['time_display']}",
    ]
    return "\n".join(lines)


def safe_head(
    state: dict[str, Any],
    events_count: int | None = None,
    *,
    reveal_private: bool = False,
    include_private_ref: bool = True,
) -> dict[str, Any]:
    if state["visibility"] == "private" and not reveal_private:
        result: dict[str, Any] = {
            "visibility": "private",
            "revision": state["revision"],
            "note": "exact private values suppressed; identities suppressed",
        }
        if include_private_ref:
            result["private_ref"] = state["last_private_ref"]
        return result
    result = {
        "ledger_id": state["ledger_id"],
        "visibility": state["visibility"],
        "campaign_id": state["campaign_id"],
        "branch_id": state["branch_id"],
        "revision": state["revision"],
        "head_hash": state["head_hash"],
        "last_receipt": state["last_receipt"],
    }
    if events_count is not None:
        result["events"] = events_count
    result["subject"] = copy.deepcopy(state["subject"])
    return result


def _print_result(value: Any, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n", end="")
    else:
        print(value)
    sys.stdout.flush()


def _private_ledger_hint(root: Path) -> bool:
    try:
        value = json.loads(_manifest_path(root).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    genesis = value.get("genesis_state")
    return value.get("visibility") == "private" or (
        isinstance(genesis, dict) and genesis.get("visibility") == "private"
    )


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Finis Solutus receipt-ledger helper")
    parser.add_argument("--version", action="version", version=f"fs_ledger.py {TOOL_VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create a ledger from genesis JSON")
    init.add_argument("ledger")
    init.add_argument("genesis")
    init.add_argument("--json", action="store_true")

    apply = commands.add_parser("apply", help="preview or commit one resolved receipt")
    apply.add_argument("ledger")
    apply.add_argument("proposal")
    apply.add_argument("--dry-run", action="store_true")
    apply.add_argument("--json", action="store_true")

    status = commands.add_parser("status", help="render the current player footer")
    status.add_argument("ledger")

    head = commands.add_parser("head", help="show revision and head identity")
    head.add_argument("ledger")
    head.add_argument(
        "--reveal-private", action="store_true",
        help="print private ledger IDs and internal head hash; keep this output private",
    )
    head.add_argument("--json", action="store_true")

    verify = commands.add_parser("verify", help="replay and verify the full chain")
    verify.add_argument("ledger")
    verify.add_argument("--json", action="store_true")

    rebuild = commands.add_parser("rebuild", help="rebuild state.json from verified events")
    rebuild.add_argument("ledger")
    rebuild.add_argument("--json", action="store_true")

    show = commands.add_parser("show", help="show canonical state")
    show.add_argument("ledger")
    show.add_argument(
        "--reveal-private", action="store_true",
        help="print the complete exact private state; keep this output private",
    )
    show.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _configure_stdio()
    parser = _build_parser()
    args = parser.parse_args(argv)
    private_context = False
    committed_result = False
    try:
        root = Path(os.path.abspath(os.fspath(args.ledger)))
        private_context = _private_ledger_hint(root)
        if args.command == "init":
            raw_genesis = load_json(args.genesis)
            private_context = isinstance(raw_genesis, dict) and raw_genesis.get("visibility") == "private"
            state = initialize(root, raw_genesis)
            committed_result = True
            if state["visibility"] == "private":
                result: Any = safe_head(state)
            else:
                result = safe_head(state) if args.json else (
                    f"OK r0 ledger={state['ledger_id']} head={state['head_hash']}"
                )
            _print_result(result, as_json=args.json or state["visibility"] == "private")
        elif args.command == "apply":
            state, event, apply_status = apply_to_ledger(
                root, load_json(args.proposal), dry_run=args.dry_run
            )
            committed_result = apply_status != "dry_run"
            if state["visibility"] == "private":
                result = {
                    **safe_head(state, include_private_ref=not args.dry_run),
                    "result": apply_status,
                }
                if apply_status.endswith("projection_stale"):
                    result["recovery"] = "retry the identical request or run rebuild before continuing"
            elif args.json:
                result = {
                    "result": apply_status,
                    "head": safe_head(state),
                    "receipt": event,
                    "footer": player_footer(state),
                }
                if apply_status.endswith("projection_stale"):
                    result["recovery"] = "retry the identical request or run rebuild before continuing"
            else:
                label = {
                    "already_applied": "ALREADY_APPLIED",
                    "dry_run": "DRY_RUN",
                    "applied": "OK",
                    "committed_projection_stale": "COMMITTED_PROJECTION_STALE",
                    "already_applied_projection_stale": "ALREADY_APPLIED_PROJECTION_STALE",
                }[apply_status]
                result = f"{label} r{state['revision']} receipt={event['receipt_id']} head={state['head_hash']}"
                if apply_status.endswith("projection_stale"):
                    result += "; retry the identical request or run rebuild before continuing"
            _print_result(result, as_json=args.json or state["visibility"] == "private")
        elif args.command == "status":
            state, _events = read_ledger(root)
            _print_result(player_footer(state))
        elif args.command == "head":
            state, events = read_ledger(root)
            result = safe_head(state, len(events), reveal_private=args.reveal_private)
            compact = json.dumps(result, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            _print_result(result if args.json else compact, as_json=args.json)
        elif args.command == "verify":
            state, count = verify_ledger(root)
            result = {**safe_head(state, count), "result": "internal_chain_verified"}
            if state["visibility"] == "private":
                _print_result(result, as_json=True)
            else:
                _print_result(
                    result if args.json else (
                        f"INTERNAL_CHAIN_VERIFIED r{state['revision']} "
                        f"events={count} head={state['head_hash']}"
                    ),
                    as_json=args.json,
                )
        elif args.command == "rebuild":
            state = rebuild_projection(root)
            committed_result = True
            result = {**safe_head(state), "result": "projection_rebuilt"}
            if state["visibility"] == "private":
                _print_result(result, as_json=True)
            else:
                _print_result(
                    result if args.json else f"REBUILT r{state['revision']} head={state['head_hash']}",
                    as_json=args.json,
                )
        elif args.command == "show":
            state, _events = read_ledger(root)
            if state["visibility"] == "private" and not args.reveal_private:
                result = safe_head(state)
            else:
                result = state
            _print_result(result if args.json else pretty_json(result).rstrip(), as_json=args.json)
        else:
            parser.error("unknown command")
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except OSError:
            pass
        return 0
    except LedgerError as exc:
        detail = "private ledger operation refused" if private_context else str(exc)
        try:
            print(f"REFUSED {exc.label}: {detail}", file=sys.stderr)
        except BrokenPipeError:
            pass
        return exc.exit_code
    except OSError:
        if committed_result:
            try:
                os.write(
                    sys.stderr.fileno(),
                    b"COMPLETED OUTPUT_ERROR: state result committed; verify before continuing\n",
                )
            except (OSError, ValueError):
                pass
            return 0
        try:
            print("REFUSED IO: filesystem operation failed", file=sys.stderr)
        except BrokenPipeError:
            pass
        return LedgerError.exit_code
    except (ArithmeticError, TypeError):
        try:
            print("REFUSED SCHEMA: input failed validation", file=sys.stderr)
        except BrokenPipeError:
            pass
        return LedgerError.exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
