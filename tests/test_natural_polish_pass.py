"""Grounding-preserving natural polish pass.

The polish pass is the final line edit that re-voices an already-approved post
for natural rhythm. It must only ever swap a clean post for an equally clean,
better-voiced post: any regression, invented number, provider error, or failed
exact-final audit has to leave the approved copy untouched.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from leadpoet_social_brain.core.models import DraftRequest
from leadpoet_social_brain.generation import (
    SocialBrainGenerator,
    polish_introduces_unsupported_specifics,
)
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore

from tests.test_social_brain import (
    distinct_bad_writer_options,
    evaluation_json,
    exact_final_audit_json_for_prompt,
    polished_post_from_prompt,
)


SELECTED = "A timing signal is only useful when it changes which account a rep opens first."
POLISHED = "A timing signal only matters when it changes which account a rep opens first."


def natural_request() -> DraftRequest:
    return DraftRequest(
        platform="x",
        topic="sales operations",
        post_type="founder_insight",
        angle="Explain why timing signals should change account priority.",
        prompt_profile="natural",
    )


class PolishProvider:
    """Writer + evaluator agree on SELECTED; the polish reply is configurable."""

    def __init__(self, polish_reply: str | None = None, *, raise_on_polish: bool = False):
        self.prompts: list[str] = []
        self.polish_reply = polish_reply
        self.raise_on_polish = raise_on_polish

    def generate(self, prompt: str, *, variants: int) -> list[str]:
        self.prompts.append(prompt)
        if "Audit this exact final X post" in prompt:
            return [exact_final_audit_json_for_prompt(prompt)]
        if "final line edit" in prompt:
            if self.raise_on_polish:
                raise RuntimeError("polish model unavailable")
            if self.polish_reply is None:
                return [polished_post_from_prompt(prompt)]
            return [self.polish_reply]
        if "Write 3 X draft options" in prompt:
            return [SELECTED, *distinct_bad_writer_options(SELECTED, 3)[1:]][:variants]
        return [evaluation_json(SELECTED)]


def generate_once(store: SQLiteStore, provider: object):
    return SocialBrainGenerator(store, provider=provider).generate(natural_request(), save=False)[0]


def polish_receipt(draft) -> dict:
    return draft.context_snapshot["evaluation"]["natural_polish"]


class NaturalPolishPassTests(unittest.TestCase):
    def test_polish_applies_better_voiced_text_and_records_receipt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(Path(tmpdir) / "brain.sqlite3")
            provider = PolishProvider(polish_reply=POLISHED)

            draft = generate_once(store, provider)

            self.assertEqual(draft.text, POLISHED)
            receipt = polish_receipt(draft)
            self.assertTrue(receipt["applied"])
            self.assertEqual(receipt["status"], "applied")
            self.assertTrue(any("final line edit" in prompt for prompt in provider.prompts))
            store.close()

    def test_polish_keeps_approved_copy_when_the_edit_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(Path(tmpdir) / "brain.sqlite3")
            provider = PolishProvider()  # echoes the post back unchanged

            draft = generate_once(store, provider)

            self.assertEqual(draft.text, SELECTED)
            receipt = polish_receipt(draft)
            self.assertFalse(receipt["applied"])
            self.assertEqual(receipt["status"], "unchanged")
            store.close()

    def test_polish_is_discarded_when_it_reintroduces_banned_voice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(Path(tmpdir) / "brain.sqlite3")
            provider = PolishProvider(
                polish_reply="Here's the thing: the future of timing signals is unlocked at scale."
            )

            draft = generate_once(store, provider)

            self.assertEqual(draft.text, SELECTED)
            receipt = polish_receipt(draft)
            self.assertFalse(receipt["applied"])
            self.assertEqual(receipt["status"], "rejected")
            self.assertTrue(receipt["rejected_violations"])
            store.close()

    def test_polish_is_discarded_when_it_invents_a_metric(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(Path(tmpdir) / "brain.sqlite3")
            provider = PolishProvider(
                polish_reply=(
                    "A timing signal only matters when it changes which account a rep opens first. "
                    "It lifts reply rates 37 percent."
                )
            )

            draft = generate_once(store, provider)

            self.assertEqual(draft.text, SELECTED)
            self.assertFalse(polish_receipt(draft)["applied"])
            store.close()

    def test_polish_provider_failure_never_breaks_generation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(Path(tmpdir) / "brain.sqlite3")
            provider = PolishProvider(raise_on_polish=True)

            draft = generate_once(store, provider)

            self.assertEqual(draft.text, SELECTED)
            receipt = polish_receipt(draft)
            self.assertFalse(receipt["applied"])
            self.assertEqual(receipt["status"], "skipped_provider_error")
            store.close()

    def test_polish_reverts_when_the_exact_final_audit_rejects_the_edit(self):
        """A polish edit can never turn a passing generation into a failure."""

        class AuditRejectsPolishProvider(PolishProvider):
            def generate(self, prompt: str, *, variants: int) -> list[str]:
                if "Audit this exact final X post" in prompt and POLISHED in prompt:
                    self.prompts.append(prompt)
                    # Untrustworthy audit for the polished copy only.
                    return [json.dumps({"passed": False})]
                return super().generate(prompt, variants=variants)

        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(Path(tmpdir) / "brain.sqlite3")
            provider = AuditRejectsPolishProvider(polish_reply=POLISHED)

            draft = generate_once(store, provider)

            self.assertEqual(draft.text, SELECTED)
            receipt = polish_receipt(draft)
            self.assertFalse(receipt["applied"])
            self.assertEqual(receipt["status"], "reverted_failed_exact_final_audit")
            self.assertTrue(draft.context_snapshot["evaluation"]["exact_final_audit"]["passed"])
            store.close()

    def test_polish_model_is_recorded_in_the_draft_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SQLiteStore(Path(tmpdir) / "brain.sqlite3")
            store.save_app_setting("model.polish", "openrouter/polisher")
            store.save_app_setting("model.default", "openrouter/default")

            draft = generate_once(store, PolishProvider())

            self.assertEqual(draft.context_snapshot["models_used"]["polish"], "openrouter/polisher")
            store.close()


class PolishNumberGuardTests(unittest.TestCase):
    def test_new_number_is_rejected(self):
        self.assertTrue(
            polish_introduces_unsupported_specifics("Timing beats volume.", "Timing beats volume by 30 percent.")
        )

    def test_preserved_numbers_are_allowed(self):
        self.assertFalse(
            polish_introduces_unsupported_specifics(
                "3 accounts moved last week.",
                "Last week 3 accounts moved.",
            )
        )

    def test_dropping_a_number_is_allowed(self):
        self.assertFalse(
            polish_introduces_unsupported_specifics("3 accounts moved.", "Accounts moved.")
        )


if __name__ == "__main__":
    unittest.main()
