from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import localcontext
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import fs_ledger  # noqa: E402


def public_genesis() -> dict[str, object]:
    return {
        "schema": fs_ledger.GENESIS_SCHEMA,
        "ledger_id": "test-ledger",
        "visibility": "public",
        "campaign_id": "campaign-A",
        "branch_id": "branch:A",
        "subject": {"kind": "player", "id": "PC1", "name": "Test Player"},
        "kernel": {"version": "v0.16 Draft"},
        "mechanics_profile": "fs-v0.16-default",
        "time_seconds": 32400,
        "location": "Workshop",
        "risk": "Normal",
        "stats": {"STR": 15, "AGI": 15, "STA": 15, "WIS": 15},
        "resources": {"hp": "135", "rp": "150", "fatigue": "0", "overexertion": "0"},
        "progression": {"level": 1, "exp": 95, "unallocated_points": 0, "earned_points": []},
        "gold": {
            "carried": 20,
            "stored": {
                "vault": {
                    "amount": 30,
                    "owner": "PC1",
                    "custodian": "Vault keeper",
                    "location": "Town vault",
                    "access": "Claim ticket",
                }
            },
        },
        "conditions": {},
        "inventory": {},
        "achievements": {},
        "open_items": {},
        "audit_flags": [],
        "metadata": {"fixture": True},
    }


def private_genesis() -> dict[str, object]:
    value = public_genesis()
    value.update({
        "ledger_id": "npc-ledger",
        "visibility": "private",
        "subject": {"kind": "npc", "id": "NPC1", "name": "Hidden NPC"},
    })
    value.pop("progression")
    value["metadata"] = {"note": "private"}
    return value


def proposal_for(
    state: dict[str, object], *, receipt: str = "R000001", request: str = "request-1"
) -> dict[str, object]:
    return {
        "schema": fs_ledger.PROPOSAL_SCHEMA,
        "receipt_id": receipt,
        "request_id": request,
        "kind": "resolved_block",
        "campaign_id": state["campaign_id"],
        "branch_id": state["branch_id"],
        "subject_id": state["subject"]["id"],  # type: ignore[index]
        "expected_revision": state["revision"],
        "expected_head": state["head_hash"],
        "summary": "Resolved fixture block",
        "elapsed_seconds": 1800,
        "location": {"value": "Market", "path": "Walked from the workshop"},
        "ops": [
            {"op": "rp.change", "amount": "-10", "reason": "Tool use"},
            {"op": "fatigue.exert", "amount": "4", "reason": "Work"},
            {"op": "gold.spend", "amount": 5, "from": "carried", "reason": "Supplies"},
            {
                "op": "exp.award",
                "amount": 10,
                "reason": "First full repair",
                "earned_points": [{"stat": "AGI", "reason": "Precise handling"}],
            },
            {"op": "points.allocate", "amounts": {"STA": 1}, "reason": "Player allocation"},
            {
                "op": "achievement.upsert",
                "id": "repair",
                "value": {
                    "label": "Repair", "result": "verified", "acceptance": "pending",
                    "account": "open",
                },
            },
            {"op": "open_item.upsert", "id": "payment", "value": {"label": "Payment", "status": "open"}},
        ],
        "refs": {"scene_id": "S1"},
    }


class LedgerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "ledger"
        self.state = fs_ledger.initialize(self.root, public_genesis())

    def digest(self) -> str:
        digest = hashlib.sha256()
        digest.update((self.root / "events.jsonl").read_bytes())
        digest.update((self.root / "state.json").read_bytes())
        return digest.hexdigest()


class GenesisAndProjectionTests(LedgerFixture):
    def test_genesis_derives_exact_maxima_and_footer(self) -> None:
        state, count = fs_ledger.verify_ledger(self.root)
        self.assertEqual(count, 0)
        self.assertEqual(state["derived"]["maximum"], {"hp": "135", "rp": "150", "fatigue": "105"})
        self.assertIn("STATE r0 · branch:A · PC1", fs_ledger.player_footer(state))
        self.assertTrue(fs_ledger.player_footer(state).endswith("Current Time: Day 1 · 9:00 AM"))

    def test_golden_example_genesis_hash_matches_proposal(self) -> None:
        genesis = fs_ledger.load_json(REPO_ROOT / "scripts/examples/public_genesis.json")
        proposal = fs_ledger.load_json(REPO_ROOT / "scripts/examples/resolved_block.json")
        state = fs_ledger.normalize_genesis(genesis)
        self.assertEqual(state["head_hash"], proposal["expected_head"])
        self.assertEqual(state["head_hash"], "sha256:84418c04e1661489652412a528b5a38566aed9c9a2471680c3425c8985d3eff4")

    def test_stale_projection_is_detected_and_rebuildable(self) -> None:
        projection = json.loads((self.root / "state.json").read_text())
        projection["location"] = "Wrong Place"
        (self.root / "state.json").write_text(json.dumps(projection))
        with self.assertRaises(fs_ledger.CorruptionError):
            fs_ledger.verify_ledger(self.root)
        self.assertEqual(fs_ledger.rebuild_projection(self.root)["location"], "Workshop")

    def test_genesis_head_anchor_cannot_be_forged_independently(self) -> None:
        path = self.root / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["genesis_state"]["head_hash"] = "sha256:" + "0" * 64
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(fs_ledger.CorruptionError, "head anchor"):
            fs_ledger.replay(self.root)

    def test_valid_same_revision_divergent_projection_is_refused(self) -> None:
        projection = json.loads((self.root / "state.json").read_text())
        projection["location"] = "Divergent"
        projection["state_hash"] = fs_ledger.state_hash(projection)
        (self.root / "state.json").write_text(fs_ledger.pretty_json(projection))
        with self.assertRaisesRegex(fs_ledger.CorruptionError, "divergent history"):
            fs_ledger.rebuild_projection(self.root)


class ApplyAndReplayTests(LedgerFixture):
    def test_atomic_composite_block_and_exact_replay(self) -> None:
        new_state, event, status = fs_ledger.apply_to_ledger(self.root, proposal_for(self.state))
        self.assertEqual(status, "applied")
        self.assertEqual(new_state["resources"], {"hp": "135", "rp": "140", "fatigue": "4", "overexertion": "0"})
        self.assertEqual(new_state["progression"]["level"], 2)
        self.assertEqual(new_state["stats"], {"STR": 15, "AGI": 16, "STA": 16, "WIS": 15})
        replayed, events = fs_ledger.replay(self.root)
        self.assertEqual(replayed, new_state)
        self.assertEqual(events[0]["hash"], event["hash"])

    def test_identical_retry_is_noop_and_collision_refuses(self) -> None:
        proposal = proposal_for(self.state)
        first, _event, _ = fs_ledger.apply_to_ledger(self.root, proposal)
        before = self.digest()
        retried, _event, status = fs_ledger.apply_to_ledger(self.root, proposal)
        self.assertEqual((retried, status), (first, "already_applied"))
        self.assertEqual(before, self.digest())
        conflict = copy.deepcopy(proposal)
        conflict["summary"] = "different"
        with self.assertRaises(fs_ledger.InvariantError):
            fs_ledger.apply_to_ledger(self.root, conflict)

    def test_identical_retry_repairs_stale_projection(self) -> None:
        proposal = proposal_for(self.state)
        committed, _event, _ = fs_ledger.apply_to_ledger(self.root, proposal)
        (self.root / "state.json").write_text(fs_ledger.pretty_json(self.state))
        healed, _event, status = fs_ledger.apply_to_ledger(self.root, proposal)
        self.assertEqual((healed, status), (committed, "already_applied"))
        fs_ledger.verify_ledger(self.root)

    def test_projection_failure_reports_committed_then_retry_heals(self) -> None:
        proposal = proposal_for(self.state)
        real_write = fs_ledger._atomic_write

        def fail_projection(path: Path, text: str, *, private: bool = False) -> None:
            if path.name == "state.json":
                raise OSError("synthetic")
            real_write(path, text, private=private)

        with mock.patch.object(fs_ledger, "_atomic_write", side_effect=fail_projection):
            committed, _event, status = fs_ledger.apply_to_ledger(self.root, proposal)
        self.assertEqual(status, "committed_projection_stale")
        self.assertEqual(fs_ledger.replay(self.root)[0], committed)
        self.assertEqual(fs_ledger.apply_to_ledger(self.root, proposal)[2], "already_applied")

    def test_failed_projection_repair_still_reports_applied(self) -> None:
        proposal = proposal_for(self.state)
        committed, _event, _ = fs_ledger.apply_to_ledger(self.root, proposal)
        with mock.patch.object(fs_ledger, "_atomic_write", side_effect=OSError("synthetic")):
            replayed, _event, status = fs_ledger.apply_to_ledger(self.root, proposal)
        self.assertEqual(status, "already_applied_projection_stale")
        self.assertEqual(replayed, committed)

    def test_event_tail_loss_is_not_silently_replaced(self) -> None:
        fs_ledger.apply_to_ledger(self.root, proposal_for(self.state))
        (self.root / "events.jsonl").write_text("")
        with self.assertRaisesRegex(fs_ledger.CorruptionError, "tail loss"):
            fs_ledger.replay(self.root)
        with self.assertRaisesRegex(fs_ledger.CorruptionError, "tail loss"):
            fs_ledger.rebuild_projection(self.root)

    def test_missing_terminal_newline_is_refused_before_append(self) -> None:
        fs_ledger.apply_to_ledger(self.root, proposal_for(self.state))
        path = self.root / "events.jsonl"
        path.write_text(path.read_text().rstrip("\n"))
        with self.assertRaisesRegex(fs_ledger.CorruptionError, "terminal newline"):
            fs_ledger.replay(self.root)

    def test_unicode_line_separators_replay_exactly(self) -> None:
        for marker in ("\u0085", "\u2028", "\u2029"):
            with self.subTest(marker=ord(marker)):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary) / "ledger"
                    state = fs_ledger.initialize(root, public_genesis())
                    proposal = proposal_for(state)
                    proposal["summary"] = f"before{marker}after"
                    applied, _event, _ = fs_ledger.apply_to_ledger(root, proposal)
                    self.assertEqual(fs_ledger.replay(root)[0], applied)

    def test_reversed_point_mapping_order_replays_deterministically(self) -> None:
        genesis = public_genesis()
        genesis["progression"]["unallocated_points"] = 2  # type: ignore[index]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "ledger"
            state = fs_ledger.initialize(root, genesis)
            proposal = proposal_for(state)
            proposal.update({"kind": "allocation", "elapsed_seconds": 0})
            proposal.pop("location")
            proposal["ops"] = [{"op": "points.allocate", "amounts": {"STA": 1, "AGI": 1}, "reason": "order"}]
            applied, _event, _ = fs_ledger.apply_to_ledger(root, proposal)
            self.assertEqual([x["stat"] for x in applied["stat_history"]], ["AGI", "STA"])
            self.assertEqual(fs_ledger.replay(root)[0], applied)

    def test_direct_resource_delta_is_exact_beyond_ambient_precision(self) -> None:
        genesis = public_genesis()
        genesis["resources"]["hp"] = "10000000000000000000000000000"  # type: ignore[index]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "ledger"
            state = fs_ledger.initialize(root, genesis)
            proposal = proposal_for(state)
            proposal.update({"kind": "correction", "elapsed_seconds": 0})
            proposal.pop("location")
            proposal["ops"] = [{"op": "hp.change", "amount": "1", "reason": "exact"}]
            with localcontext() as context:
                context.prec = 5
                applied, _event, _ = fs_ledger.apply_to_ledger(root, proposal)
            self.assertEqual(applied["resources"]["hp"], "10000000000000000000000000001")
            self.assertEqual(fs_ledger.replay(root)[0], applied)

    def test_stale_head_and_overdraw_mutate_nothing(self) -> None:
        stale = proposal_for(self.state)
        stale["expected_head"] = "sha256:" + "0" * 64
        before = self.digest()
        with self.assertRaises(fs_ledger.StaleHeadError):
            fs_ledger.apply_to_ledger(self.root, stale)
        overdraw = proposal_for(self.state, receipt="R2", request="Q2")
        overdraw["ops"] = [{"op": "gold.spend", "amount": 21, "from": "carried", "reason": "no"}]
        with self.assertRaises(fs_ledger.InvariantError):
            fs_ledger.apply_to_ledger(self.root, overdraw)
        self.assertEqual(before, self.digest())

    def test_dry_run_changes_no_files(self) -> None:
        before = self.digest()
        state, _event, status = fs_ledger.apply_to_ledger(self.root, proposal_for(self.state), dry_run=True)
        self.assertEqual((state["revision"], status), (1, "dry_run"))
        self.assertEqual(before, self.digest())

    def test_event_tampering_is_detected_at_first_edge(self) -> None:
        fs_ledger.apply_to_ledger(self.root, proposal_for(self.state))
        path = self.root / "events.jsonl"
        event = json.loads(path.read_text())
        event["proposal"]["summary"] = "tampered"
        path.write_text(fs_ledger.canonical_json(event) + "\n")
        with self.assertRaisesRegex(fs_ledger.CorruptionError, "event hash"):
            fs_ledger.replay(self.root)

    def test_event_envelope_ids_must_match_proposal(self) -> None:
        fs_ledger.apply_to_ledger(self.root, proposal_for(self.state))
        path = self.root / "events.jsonl"
        event = json.loads(path.read_text())
        event["receipt_id"] = "different"
        event["hash"] = fs_ledger.content_hash(fs_ledger._event_without_hash(event))
        path.write_text(fs_ledger.canonical_json(event) + "\n")
        with self.assertRaisesRegex(fs_ledger.CorruptionError, "envelope"):
            fs_ledger.replay(self.root)


class MechanicsAndOpenStateTests(LedgerFixture):
    def test_overexertion_formula_and_remainder_persist(self) -> None:
        proposal = proposal_for(self.state)
        proposal.update({"kind": "correction", "elapsed_seconds": 0})
        proposal.pop("location")
        proposal["ops"] = [{"op": "fatigue.exert", "amount": "110", "reason": "stress"}]
        state, _event, _ = fs_ledger.apply_to_ledger(self.root, proposal)
        self.assertEqual(state["resources"], {"hp": "134", "rp": "150", "fatigue": "105", "overexertion": "1"})

    def test_fatigue_recovery_consumes_remainder_first(self) -> None:
        proposal = proposal_for(self.state)
        proposal.update({"kind": "correction", "elapsed_seconds": 0})
        proposal.pop("location")
        proposal["ops"] = [
            {"op": "fatigue.exert", "amount": "107", "reason": "stress"},
            {"op": "fatigue.recover", "amount": "3", "reason": "rest"},
        ]
        state, event, _ = fs_ledger.apply_to_ledger(self.root, proposal)
        self.assertEqual(state["resources"]["overexertion"], "0")
        self.assertEqual(state["resources"]["fatigue"], "104")
        self.assertEqual(event["outcome"]["resource_changes"][1]["remainder_recovered"], "2")

    def test_multiple_exp_awards_accumulate_outcome_and_levels(self) -> None:
        proposal = proposal_for(self.state)
        proposal["ops"] = [
            {"op": "exp.award", "amount": 10, "reason": "one", "earned_points": [{"stat": "AGI", "reason": "one"}]},
            {"op": "exp.award", "amount": 115, "reason": "two", "earned_points": [{"stat": "STA", "reason": "two"}]},
        ]
        state, event, _ = fs_ledger.apply_to_ledger(self.root, proposal)
        self.assertEqual(event["outcome"]["levels_gained"], 2)
        self.assertEqual(len(event["outcome"]["exp_awards"]), 2)
        self.assertEqual(state["progression"]["level"], 3)

    def test_open_item_and_achievement_close_independently(self) -> None:
        state, _event, _ = fs_ledger.apply_to_ledger(self.root, proposal_for(self.state))
        proposal = proposal_for(state, receipt="R2", request="Q2")
        proposal["ops"] = [{"op": "open_item.close", "id": "payment", "reason": "paid"}]
        state, _event, _ = fs_ledger.apply_to_ledger(self.root, proposal)
        self.assertEqual(state["open_items"]["payment"]["status"], "closed")
        self.assertNotEqual(state["achievements"]["repair"].get("status"), "closed")

    def test_closing_inactive_record_cannot_replace_provenance(self) -> None:
        state, _event, _ = fs_ledger.apply_to_ledger(self.root, proposal_for(self.state))
        close = proposal_for(state, receipt="R2", request="Q2")
        close["ops"] = [{"op": "open_item.close", "id": "payment", "reason": "paid"}]
        state, _event, _ = fs_ledger.apply_to_ledger(self.root, close)
        again = proposal_for(state, receipt="R3", request="Q3")
        again["ops"] = [{"op": "open_item.close", "id": "payment", "reason": "replace"}]
        with self.assertRaises(fs_ledger.InvariantError):
            fs_ledger.apply_to_ledger(self.root, again)
        self.assertEqual(fs_ledger.replay(self.root)[0]["open_items"]["payment"]["closure_receipt"], "R2")

    def test_upsert_cannot_fake_closure_or_provenance(self) -> None:
        proposal = proposal_for(self.state)
        proposal["ops"] = [{
            "op": "open_item.upsert", "id": "x",
            "value": {"label": "X", "closure_receipt": "fake"},
        }]
        with self.assertRaises(fs_ledger.InvariantError):
            fs_ledger.apply_to_ledger(self.root, proposal)

    def test_gold_transfer_preserves_total_and_custody(self) -> None:
        proposal = proposal_for(self.state)
        proposal["ops"] = [{"op": "gold.transfer", "amount": 10, "from": "carried", "to": "vault", "reason": "deposit"}]
        state, _event, _ = fs_ledger.apply_to_ledger(self.root, proposal)
        self.assertEqual((state["gold"]["carried"], state["gold"]["stored"]["vault"]["amount"]), (10, 40))


class PrivateBoundaryTests(unittest.TestCase):
    def test_private_npc_has_no_progression_and_output_redacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "private-ledger"
            state = fs_ledger.initialize(root, private_genesis())
            self.assertNotIn("progression", state)
            head = fs_ledger.safe_head(state)
            self.assertNotIn("ledger_id", head)
            self.assertRegex(head["private_ref"], fs_ledger.PRIVATE_REF_PATTERN)

    def test_private_updates_use_opaque_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "private-ledger"
            state = fs_ledger.initialize(root, private_genesis())
            proposal = proposal_for(state)
            proposal.update({"kind": "npc_update", "elapsed_seconds": 0})
            proposal.pop("location")
            proposal["ops"] = [{"op": "stat.change", "stat": "STR", "amount": 5, "source": "training", "reason": "six months"}]
            updated, event, _ = fs_ledger.apply_to_ledger(root, proposal)
            self.assertNotIn("progression", updated)
            self.assertRegex(event["private_ref"], fs_ledger.PRIVATE_REF_PATTERN)

    def test_npc_progression_is_rejected_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "private-ledger"
            state = fs_ledger.initialize(root, private_genesis())
            proposal = proposal_for(state)
            proposal.update({"kind": "npc_update", "elapsed_seconds": 0})
            proposal.pop("location")
            proposal["ops"] = [{"op": "inventory.upsert", "id": "x", "value": {"exp": 3}}]
            with self.assertRaises(fs_ledger.InvariantError):
                fs_ledger.apply_to_ledger(root, proposal)

    def test_private_ledger_refuses_git_worktree_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / ".git").mkdir()
            with self.assertRaises(fs_ledger.PrivacyError):
                fs_ledger.initialize(base / "private", private_genesis())

    def test_private_ledger_refused_if_git_boundary_appears_later(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "private"
            fs_ledger.initialize(root, private_genesis())
            (base / ".git").mkdir()
            with self.assertRaises(fs_ledger.PrivacyError):
                fs_ledger.replay(root)

    @unittest.skipIf(os.name == "nt", "symlink creation differs on Windows")
    def test_cli_keeps_lexical_symlink_for_private_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            target = base / "external"
            fs_ledger.initialize(target, private_genesis())
            public = base / "public"
            (public / ".git").mkdir(parents=True)
            link = public / "linked-private"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(fs_ledger.PrivacyError):
                fs_ledger.read_ledger(link)


class StrictInputAndDurabilityTests(LedgerFixture):
    def test_duplicate_keys_floats_and_nonfinite_rejected(self) -> None:
        for text in ('{"a":1,"a":2}', '{"a":1.5}', '{"a":NaN}'):
            with self.subTest(text=text), self.assertRaises(fs_ledger.LedgerError):
                fs_ledger.load_json_text(text)

    def test_public_extensions_reject_private_mechanics_and_credentials(self) -> None:
        genesis = public_genesis()
        genesis["metadata"] = {"note": "api_key=secretvalue123456"}
        with self.assertRaises(fs_ledger.PrivacyError):
            fs_ledger.normalize_genesis(genesis)
        proposal = proposal_for(self.state)
        proposal["refs"] = {"evidence": ["ok"], "npc_stats": "bad"}
        with self.assertRaises(fs_ledger.LedgerError):
            fs_ledger.apply_to_ledger(self.root, proposal)

    def test_bad_decimal_is_clean_cli_refusal_without_mutation(self) -> None:
        proposal = proposal_for(self.state)
        proposal["ops"] = [{"op": "rp.change", "amount": "not-a-number", "reason": "bad"}]
        path = Path(self.temporary.name) / "bad.json"
        path.write_text(json.dumps(proposal))
        before = self.digest()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = fs_ledger.main(["apply", str(self.root), str(path)])
        self.assertEqual(code, 2)
        self.assertIn("REFUSED", err.getvalue())
        self.assertEqual(before, self.digest())

    def test_failed_initialization_leaves_no_partial_target(self) -> None:
        target = Path(self.temporary.name) / "new-ledger"
        real_write = fs_ledger._atomic_write
        calls = 0

        def fail_second(path: Path, text: str, *, private: bool = False) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic")
            real_write(path, text, private=private)

        with mock.patch.object(fs_ledger, "_atomic_write", side_effect=fail_second):
            with self.assertRaises(OSError):
                fs_ledger.initialize(target, public_genesis())
        self.assertFalse(target.exists())

    def test_atomic_write_works_without_fchmod(self) -> None:
        target = Path(self.temporary.name) / "atomic.txt"
        with mock.patch.object(fs_ledger.os, "fchmod", None, create=True):
            fs_ledger._atomic_write(target, "value\n")
        self.assertEqual(target.read_text(), "value\n")

    def test_verify_obeys_writer_lock(self) -> None:
        with fs_ledger.ledger_lock(self.root):
            with self.assertRaises(fs_ledger.LockError):
                fs_ledger.verify_ledger(self.root)

    def test_directory_close_failure_is_best_effort(self) -> None:
        real_close = os.close
        called = False

        def fail_once(fd: int) -> None:
            nonlocal called
            if not called:
                called = True
                real_close(fd)
                raise OSError("synthetic close")
            real_close(fd)

        with mock.patch.object(fs_ledger.os, "close", side_effect=fail_once):
            fs_ledger._fsync_directory(self.root)
        self.assertTrue(called)

    @unittest.skipIf(os.name == "nt", "test wrapper targets POSIX flock path")
    def test_lock_handle_close_failure_is_best_effort(self) -> None:
        real_handle = (self.root / "LOCK").open("a+b")
        wrapped = mock.Mock(wraps=real_handle)
        wrapped.close.side_effect = OSError("synthetic close")
        try:
            with mock.patch.object(Path, "open", return_value=wrapped):
                with fs_ledger.ledger_lock(self.root):
                    pass
        finally:
            real_handle.close()

    def test_broken_pipe_after_success_returns_zero(self) -> None:
        with mock.patch.object(fs_ledger, "_print_result", side_effect=BrokenPipeError):
            self.assertEqual(fs_ledger.main(["head", str(self.root)]), 0)

    def test_output_error_after_apply_reports_committed_result(self) -> None:
        proposal_path = Path(self.temporary.name) / "commit-output-error.json"
        proposal_path.write_text(json.dumps(proposal_for(self.state)), encoding="utf-8")
        with (
            mock.patch.object(fs_ledger, "_print_result", side_effect=OSError("synthetic")),
            mock.patch.object(fs_ledger.os, "write") as write,
        ):
            self.assertEqual(
                fs_ledger.main(["apply", str(self.root), str(proposal_path)]),
                0,
            )
        state, events = fs_ledger.read_ledger(self.root)
        self.assertEqual(state["revision"], 1)
        self.assertEqual(len(events), 1)
        self.assertIn(b"COMPLETED OUTPUT_ERROR", write.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
