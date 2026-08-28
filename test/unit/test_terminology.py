"""Docs/terminology guard: CONTEXT.md is the single glossary, and no shipped
text may contradict it (#43, ADR-0001 amendment).

Two standing rules are enforced here:

  * CONTEXT.md's `## Language` section defines every term the tooling and the
    prompts speak, including the target-repository model and the review
    vocabulary this Feature introduces.
  * HIL is the only accepted spelling; a shipped document may name HITL only to
    forbid it.

Scanned surfaces are the *shipped* ones. `ralph/` is the snarktank-style build
harness used to construct ai-utils (README "Memory & learnings"), not shipped
tooling, and `test/` fixtures deliberately carry invalid text.
"""
import glob
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONTEXT = os.path.join(REPO_ROOT, "CONTEXT.md")
ADR_0001 = os.path.join(REPO_ROOT, "docs", "adr",
                        "0001-ai-utils-as-config-driven-tooling-submodule.md")


def _read(path):
    with open(path) as fh:
        return fh.read()


def _rel(path):
    return os.path.relpath(path, REPO_ROOT)


def _shipped_docs():
    """Every shipped markdown surface: the root docs, the ADRs, the iteration
    prompts, and the skills."""
    paths = [os.path.join(REPO_ROOT, name)
             for name in ("README.md", "CONTEXT.md", "AGENTS.md")]
    for sub in ("docs", "prompts", "skills"):
        paths += glob.glob(os.path.join(REPO_ROOT, sub, "**", "*.md"),
                           recursive=True)
    return sorted(p for p in paths if os.path.isfile(p))


def _shipped_tooling():
    """Shipped executable/tooling text (comments and docstrings included)."""
    paths = [os.path.join(REPO_ROOT, "bin", "ralph"),
             os.path.join(REPO_ROOT, "bin", "ralph.sh")]
    paths += glob.glob(os.path.join(REPO_ROOT, "lib", "*.py"))
    paths += glob.glob(os.path.join(REPO_ROOT, "schema", "*.json"))
    return sorted(p for p in paths if os.path.isfile(p))


def _paragraphs(text):
    """Blank-line separated chunks, so a claim that wraps across source lines is
    still judged as one statement."""
    return [chunk for chunk in re.split(r"\n\s*\n", text) if chunk.strip()]


def _sentences(paragraph):
    return re.split(r"(?<=[.!?])\s+", " ".join(paragraph.split()))


def glossary():
    """CONTEXT.md's `## Language` entries as {term: definition}."""
    text = _read(CONTEXT)
    section = text.split("## Language", 1)[1]
    entries, term = {}, None
    for line in section.splitlines():
        head = re.match(r"^\*\*([^*]+)\*\*", line)
        if head:
            term = head.group(1).strip()
            entries[term] = ""
        elif term:
            entries[term] += line + "\n"
    return entries


class ContextGlossaryDefinesTheVocabulary(unittest.TestCase):
    """Every term the Feature speaks is defined once, in CONTEXT.md."""

    def setUp(self):
        self.terms = glossary()

    def test_defines_target_repository(self):
        self.assertIn("Target Repository", self.terms,
                      "CONTEXT.md must define Target Repository")

    def test_target_repository_covers_the_self_hosting_case(self):
        body = self.terms.get("Target Repository", "").lower()
        self.assertIn("ai-utils", body,
                      "Target Repository must say when ai-utils is the target")
        self.assertTrue("checkout root" in body or "checkout-root" in body,
                        "Target Repository must name the checkout-root case")

    def test_defines_the_review_vocabulary(self):
        for term in ("Model Profile", "Implementation Agent", "Review Agent",
                     "In Review", "Finding", "Negotiation Round",
                     "Human Arbitration", "Control Plane", "Token Ledger"):
            self.assertIn(term, self.terms,
                          "CONTEXT.md must define %s" % term)

    def test_keeps_the_standing_terms(self):
        for term in ("Superproject", "Ralph Loop", "Story", "PRD", "Feature",
                     "AFK Story", "HIL Story"):
            self.assertIn(term, self.terms,
                          "CONTEXT.md must keep defining %s" % term)

    def test_superproject_stays_the_submodule_mount_case(self):
        body = self.terms.get("Superproject", "").lower()
        self.assertIn("submodule", body,
                      "Superproject must remain the submodule-mount case")


class Adr0001CarriesTheTargetRepositoryAmendment(unittest.TestCase):
    """ADR-0001's superproject-only invariant is replaced by the
    target-repository model, as a recorded amendment."""

    def setUp(self):
        self.text = _read(ADR_0001)
        self.low = self.text.lower()

    def test_has_an_amendment_section(self):
        self.assertIn("amendment", self.low,
                      "ADR-0001 must record the change as an amendment")

    def test_states_the_target_repository_model(self):
        self.assertIn("target repository", self.low,
                      "the amendment must name the target repository model")

    def test_submodule_mount_never_mutates_ai_utils(self):
        amendment = self.low.split("amendment", 1)[1]
        self.assertRegex(
            amendment, r"submodule[\s\S]{0,200}?never[^.]{0,60}?ai-utils",
            "the amendment must state a submodule mount never mutates ai-utils")

    def test_checkout_root_ai_utils_may_target_itself(self):
        amendment = self.low.split("amendment", 1)[1]
        self.assertTrue("checkout root" in amendment or "checkout-root" in amendment,
                        "the amendment must name the checkout-root case")
        self.assertRegex(
            amendment, r"target[\s\S]{0,80}?itself|itself[\s\S]{0,80}?target",
            "the amendment must say a checkout-root ai-utils may target itself")


class NoShippedTextForbidsTargetingAiUtils(unittest.TestCase):
    """AC: no shipped documentation or tooling text still claims Ralph can never
    target ai-utils. A claim is only acceptable when it is scoped to the
    submodule-mount case in the same sentence."""

    CLAIM = re.compile(r"never[^.]{0,60}?ai-utils", re.IGNORECASE)
    SUPERPROJECT_ONLY = re.compile(r"only ever (?:modifies|targets|runs against)"
                                   r"[^.]{0,40}?superproject", re.IGNORECASE)
    # A sentence is acceptable when it scopes itself to the submodule mount, or
    # when it states the target-repository model rather than the old invariant.
    SCOPED = re.compile(r"submodule|mounted|\bmount\b|target repositor",
                        re.IGNORECASE)

    def flagged(self, sentence):
        if self.SCOPED.search(sentence):
            return False
        return bool(self.CLAIM.search(sentence)
                    or self.SUPERPROJECT_ONLY.search(sentence))

    def offenders(self, paths):
        return ["%s: %s" % (_rel(path), sentence)
                for path in paths
                for paragraph in _paragraphs(_read(path))
                for sentence in _sentences(paragraph)
                if self.flagged(sentence)]

    def test_the_guard_would_catch_the_old_invariant(self):
        # The rule is only worth having if the superseded claim is rejected.
        self.assertTrue(self.flagged(
            "Ralph only ever modifies the superproject, never `ai-utils`."))
        self.assertFalse(self.flagged(
            "Ralph never mutates a mounted `ai-utils` checkout."))
        self.assertFalse(self.flagged(
            "Ralph only ever modifies its target repository."))

    def test_no_documentation_claims_ai_utils_is_off_limits(self):
        self.assertEqual(self.offenders(_shipped_docs()), [])

    def test_no_tooling_text_claims_ai_utils_is_off_limits(self):
        self.assertEqual(self.offenders(_shipped_tooling()), [])


class HilIsTheOnlyAcceptedSpelling(unittest.TestCase):
    """Standing rule (CONTEXT.md `_Avoid_: HITL`): shipped docs may name HITL
    only to forbid it."""

    # `\W` would not match the `_` in CONTEXT.md's `_Avoid_: HITL`, so the
    # separator class is spelled out as "not alphanumeric".
    FORBIDDING = re.compile(r"(?:not|never|avoid|instead of)[^A-Za-z0-9]{0,4}"
                            r"[*_`]{0,2}HITL", re.IGNORECASE)

    def test_shipped_docs_use_hil(self):
        offenders = []
        for path in _shipped_docs():
            for paragraph in _paragraphs(_read(path)):
                if "HITL" in paragraph and not self.FORBIDDING.search(paragraph):
                    offenders.append("%s: %s" % (_rel(path),
                                                 paragraph.splitlines()[0]))
        self.assertEqual(offenders, [],
                         "shipped docs must use HIL, not HITL")

    def test_the_guard_would_catch_a_bare_hitl(self):
        # The rule is only worth having if an unqualified mention is rejected.
        self.assertIsNone(self.FORBIDDING.search("A HITL story needs a human."))
        self.assertIsNotNone(self.FORBIDDING.search("it is HIL, never *HITL*"))
        self.assertIsNotNone(self.FORBIDDING.search('_Avoid_: HITL (use "HIL")'))


if __name__ == "__main__":
    unittest.main()
