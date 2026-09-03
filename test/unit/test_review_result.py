"""Contract tests for the versioned structured-review payload (#51).

The validator is the trusted wrapper's gate: a payload that does not pass here
never becomes a GitHub review, so every rejection the story names is asserted
here, and every rejection names the offending field path.
"""
import copy
import json
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import ralph_review_result  # noqa: E402

FIXTURES = os.path.join(REPO_ROOT, "test", "fixtures", "reviews")
CONTRACT_DOC = os.path.join(REPO_ROOT, "docs", "review-contract.md")


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


def reviewed_diff():
    with open(os.path.join(FIXTURES, "head.diff")) as fh:
        return fh.read()


def validate(payload, diff=None):
    changed = ralph_review_result.changed_lines(diff) if diff is not None else None
    return ralph_review_result.validate_review(payload, changed=changed)


def field_paths(result):
    return [error.split(":", 1)[0] for error in result.errors]


class LateBlockersAreBoundedByCategory(unittest.TestCase):
    """Round two adjudicates; it does not go looking for fresh objections.

    A round that could raise any blocker it liked would never converge: the
    Implementation Agent would answer a different review every time, and the
    round limit would run out on goalposts rather than on disagreement.
    """

    def prior(self):
        return fixture("valid-inline.json")["findings"]  # F-1, F-2

    def later(self, ident="F-9", category="defect", blocking=True):
        result = dict(fixture("cross-cutting.json"), round=2)
        finding = dict(result["findings"][0], id=ident, category=category,
                       blocking=blocking)
        result["findings"] = [finding]
        if not blocking:
            result["verdict"] = "comment"
        return ralph_review_result.validate_review(
            result, prior_findings=self.prior())

    def test_a_regression_the_fixes_introduced_may_still_block_late(self):
        self.assertTrue(self.later(category="defect").ok)

    def test_a_missed_safety_defect_may_still_block_late(self):
        self.assertTrue(self.later(category="safety_regression").ok)

    def test_a_new_late_blocker_of_any_other_kind_is_refused(self):
        result = self.later(category="missing_tests")

        self.assertFalse(result.ok)
        self.assertIn("findings/0/category", " ".join(result.errors))
        self.assertIn("F-9", " ".join(result.errors))

    def test_a_finding_carried_over_from_the_earlier_round_is_not_late(self):
        self.assertTrue(self.later(ident="F-1", category="missing_tests").ok)

    def test_a_late_non_blocking_remark_is_not_restricted(self):
        self.assertTrue(
            self.later(category="style_preference", blocking=False).ok)

    def test_round_one_may_raise_any_blocker_the_policy_allows(self):
        result = ralph_review_result.validate_review(
            fixture("valid-inline.json"), prior_findings=[])

        self.assertTrue(result.ok, result.errors)


class ChangedLines(unittest.TestCase):
    """A finding may cite any line GitHub would accept an inline thread on."""

    def test_maps_the_new_side_of_every_hunk(self):
        changed = ralph_review_result.changed_lines(reviewed_diff())
        self.assertEqual(sorted(changed), ["lib/ralph_review_result.py"])
        self.assertEqual(changed["lib/ralph_review_result.py"], set(range(8, 19)))


class ValidPayloads(unittest.TestCase):
    def test_inline_findings_validate_against_the_reviewed_diff(self):
        result = validate(fixture("valid-inline.json"), reviewed_diff())
        self.assertTrue(result.ok, result.errors)
        self.assertIn("request_changes", result.summary())
        self.assertIn("1 blocking", result.summary())

    def test_a_cross_cutting_finding_needs_no_source_location(self):
        result = validate(fixture("cross-cutting.json"), reviewed_diff())
        self.assertTrue(result.ok, result.errors)

    def test_non_blocking_suggestions_may_accompany_an_approval(self):
        result = validate(fixture("non-blocking.json"), reviewed_diff())
        self.assertTrue(result.ok, result.errors)

    def test_a_payload_validates_without_a_diff_to_range_check_against(self):
        result = validate(fixture("valid-inline.json"))
        self.assertTrue(result.ok, result.errors)


class RequiredResultFields(unittest.TestCase):
    def test_verdict_head_model_round_and_summary_are_required(self):
        for field in ("verdict", "head", "model", "round", "summary"):
            payload = fixture("valid-inline.json")
            del payload[field]
            result = validate(payload)
            self.assertFalse(result.ok, field)
            self.assertIn(field, " ".join(result.errors), field)

    def test_the_contract_version_is_required_and_exact(self):
        payload = fixture("valid-inline.json")
        payload["contract"] = "ralph-review/v99"
        self.assertFalse(validate(payload).ok)
        del payload["contract"]
        self.assertFalse(validate(payload).ok)

    def test_the_head_must_be_an_exact_commit(self):
        for head in ("HEAD", "9f1c2d3", ""):
            payload = fixture("valid-inline.json")
            payload["head"] = head
            result = validate(payload)
            self.assertFalse(result.ok, head)
            self.assertIn("head", field_paths(result))

    def test_the_round_must_be_a_positive_integer(self):
        for round_no in (0, -1, "one", 1.5):
            payload = fixture("valid-inline.json")
            payload["round"] = round_no
            self.assertFalse(validate(payload).ok, round_no)

    def test_unknown_top_level_keys_are_rejected(self):
        payload = fixture("valid-inline.json")
        payload["merge"] = True
        result = validate(payload)
        self.assertFalse(result.ok)
        self.assertIn("merge", " ".join(result.errors))


class FindingIdentifiers(unittest.TestCase):
    def test_every_finding_carries_an_identifier(self):
        payload = fixture("valid-inline.json")
        del payload["findings"][0]["id"]
        result = validate(payload)
        self.assertFalse(result.ok)
        self.assertIn("findings/0", field_paths(result)[0])

    def test_identifiers_are_unique_within_a_result(self):
        payload = fixture("valid-inline.json")
        payload["findings"][1]["id"] = payload["findings"][0]["id"]
        result = validate(payload)
        self.assertFalse(result.ok)
        self.assertIn("findings/1/id", field_paths(result))
        self.assertIn("duplicate", " ".join(result.errors))


class BlockingPolicy(unittest.TestCase):
    """Blocking is declared, never inferred, and only for the narrow reasons."""

    def test_the_blocking_classification_is_required(self):
        payload = fixture("valid-inline.json")
        del payload["findings"][0]["blocking"]
        result = validate(payload)
        self.assertFalse(result.ok)
        self.assertIn("blocking", " ".join(result.errors))

    def test_a_blocking_finding_must_name_a_blocking_category(self):
        payload = fixture("valid-inline.json")
        payload["findings"][0]["category"] = "style_preference"
        result = validate(payload)
        self.assertFalse(result.ok)
        self.assertIn("findings/0/category", field_paths(result))

    def test_a_preference_may_not_be_declared_blocking_by_category(self):
        payload = fixture("non-blocking.json")
        payload["findings"][0]["category"] = "defect"
        result = validate(payload)
        self.assertFalse(result.ok)
        self.assertIn("findings/0/category", field_paths(result))

    def test_the_narrow_blocking_reasons_are_exactly_the_documented_ones(self):
        self.assertEqual(
            set(ralph_review_result.BLOCKING_CATEGORIES),
            {"acceptance_criteria", "defect", "safety_regression",
             "explicit_rule", "missing_tests", "scope_creep"})
        self.assertEqual(
            set(ralph_review_result.NON_BLOCKING_CATEGORIES),
            {"style_preference", "speculative_improvement", "preexisting_issue"})

    def test_every_finding_states_its_claim_evidence_requirement_and_check(self):
        for field in ("claim", "evidence", "requirement", "verification"):
            payload = fixture("valid-inline.json")
            del payload["findings"][0][field]
            result = validate(payload)
            self.assertFalse(result.ok, field)
            self.assertIn(field, " ".join(result.errors), field)

    def test_the_verdict_agrees_with_the_blocking_findings(self):
        approving = fixture("non-blocking.json")
        approving["findings"][0].update(blocking=True, category="defect")
        result = validate(approving)
        self.assertFalse(result.ok)
        self.assertIn("verdict", field_paths(result))

        demanding = fixture("valid-inline.json")
        demanding["findings"][0].update(blocking=False, category="style_preference")
        result = validate(demanding)
        self.assertFalse(result.ok)
        self.assertIn("verdict", field_paths(result))


class SourceLocations(unittest.TestCase):
    def test_malformed_locations_are_each_named(self):
        result = validate(fixture("malformed-location.json"), reviewed_diff())
        self.assertFalse(result.ok)
        paths = field_paths(result)
        self.assertIn("findings/0/location/line", paths)
        self.assertIn("findings/1/location/path", paths)
        self.assertIn("findings/2/location/end_line", paths)

    def test_a_path_the_diff_never_touched_is_out_of_range(self):
        payload = fixture("valid-inline.json")
        payload["findings"][0]["location"]["path"] = "lib/ralph_select.py"
        result = validate(payload, reviewed_diff())
        self.assertFalse(result.ok)
        self.assertIn("findings/0/location/path", field_paths(result))

    def test_a_range_ending_outside_the_diff_is_rejected(self):
        payload = fixture("valid-inline.json")
        payload["findings"][0]["location"]["end_line"] = 40
        result = validate(payload, reviewed_diff())
        self.assertFalse(result.ok)
        self.assertIn("findings/0/location/end_line", field_paths(result))


class PayloadSize(unittest.TestCase):
    def test_an_oversized_payload_is_rejected(self):
        result = validate(fixture("oversized.json"))
        self.assertFalse(result.ok)
        self.assertIn("%d" % ralph_review_result.MAX_PAYLOAD_BYTES,
                      " ".join(result.errors))

    def test_the_raw_text_is_measured_when_it_is_available(self):
        payload = fixture("valid-inline.json")
        padded = json.dumps(payload) + " " * ralph_review_result.MAX_PAYLOAD_BYTES
        result = ralph_review_result.validate_review(payload, raw=padded)
        self.assertFalse(result.ok)

    def test_the_finding_cap_matches_the_shipped_schema(self):
        with open(os.path.join(REPO_ROOT, "schema", "review.schema.json")) as fh:
            schema = json.load(fh)
        self.assertEqual(schema["properties"]["findings"]["maxItems"],
                         ralph_review_result.MAX_FINDINGS)

    def test_an_unreasonable_number_of_findings_is_rejected(self):
        payload = fixture("valid-inline.json")
        template = payload["findings"][1]
        payload["findings"] = [dict(copy.deepcopy(template), id="F-%d" % n)
                               for n in range(ralph_review_result.MAX_FINDINGS + 1)]
        payload["verdict"] = "comment"
        result = validate(payload)
        self.assertFalse(result.ok)
        self.assertIn("findings", " ".join(result.errors))


class Cli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [os.path.join(REPO_ROOT, "bin", "ralph"), "--validate-review"] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_a_valid_payload_exits_zero_and_reports_the_result(self):
        proc = self.run_cli(os.path.join(FIXTURES, "valid-inline.json"),
                            os.path.join(FIXTURES, "head.diff"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("request_changes", proc.stdout)
        self.assertIn("ralph-review/v1", proc.stdout)

    def test_rejection_names_the_offending_fields_on_stderr(self):
        proc = self.run_cli(os.path.join(FIXTURES, "malformed-location.json"),
                            os.path.join(FIXTURES, "head.diff"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("findings/0/location/line", proc.stderr)
        self.assertIn("findings/1/location/path", proc.stderr)

    def test_an_oversized_payload_is_refused_by_the_cli(self):
        proc = self.run_cli(os.path.join(FIXTURES, "oversized.json"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("bytes", proc.stderr)

    def test_a_payload_may_be_validated_from_stdin(self):
        with open(os.path.join(FIXTURES, "cross-cutting.json")) as fh:
            payload = fh.read()
        proc = subprocess.run(
            [os.path.join(REPO_ROOT, "bin", "ralph"), "--validate-review", "-"],
            input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_missing_arguments_are_a_usage_error(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 2)


class ContractIsDocumented(unittest.TestCase):
    """AC: the contract is versioned and documented, blocking policy included."""

    def setUp(self):
        with open(CONTRACT_DOC) as fh:
            self.text = fh.read()

    def test_names_its_version(self):
        self.assertIn(ralph_review_result.CONTRACT_VERSION, self.text)

    def test_documents_every_result_and_finding_field(self):
        for field in ("verdict", "head", "model", "round", "summary", "findings",
                      "id", "blocking", "category", "claim", "evidence",
                      "requirement", "verification", "location"):
            self.assertIn(field, self.text, field)

    def test_documents_the_blocking_policy_on_both_sides(self):
        for category in (ralph_review_result.BLOCKING_CATEGORIES
                         + ralph_review_result.NON_BLOCKING_CATEGORIES):
            self.assertIn(category, self.text, category)

    def test_documents_the_size_limit(self):
        self.assertIn("%d" % ralph_review_result.MAX_PAYLOAD_BYTES, self.text)

    def test_documents_what_a_later_round_may_and_may_not_raise(self):
        low = " ".join(self.text.lower().split())
        self.assertIn("adjudicat", low)
        self.assertIn("withdraw", low)
        for category in ralph_review_result.LATE_BLOCKING_CATEGORIES:
            self.assertIn(category, low)

    def test_documents_the_dispute_disposition_and_its_evidence(self):
        low = " ".join(self.text.lower().split())
        self.assertIn("ralph-response/v1", low)
        for disposition in ("accepted", "disputed", "unresolved"):
            self.assertIn(disposition, low)
        self.assertIn("a dispute changes no code", low)


if __name__ == "__main__":
    unittest.main()
