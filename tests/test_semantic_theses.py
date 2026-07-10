from __future__ import annotations

import json
import unittest

from leadpoet_social_brain.semantic_theses import (
    MAX_THESIS_RELATION_TOKENS,
    SALES_THESIS_ATOMS,
    SALES_THESIS_ATOM_BY_ID,
    brief_explicitly_requests_sales_thesis,
    explicitly_requested_sales_thesis_atoms,
    extract_sales_thesis_atoms,
    extract_sales_thesis_families,
)


LEAD_QUALITY = "lead-quality-outperforms-outreach-volume"
BUYER_SIGNAL = "buyer-signal-precedes-outreach"
BULK_DELIVERABILITY = "bulk-outreach-harms-deliverability"
AI_SPAM = "sales-ai-amplifies-template-spam"
TOKEN_CONTEXT = "token-personalization-differs-from-contextual-personalization"
FALSE_CONTEXT = "false-context-harms-message-credibility"
READINESS = "buyer-readiness-outperforms-firmographic-fit"
SHARED_LISTS = "shared-static-lists-cause-buyer-saturation"
STALE_DATA = "stale-contact-data-wastes-seller-time"
HUMAN_CLOSE = "human-sellers-retain-complex-closing-role"
MODEL_COMPRESSION = "foundation-models-threaten-enrichment-layer"
SMALL_LIST = "small-quality-list-enables-angle-learning"


class SemanticSalesThesisTest(unittest.TestCase):
    def test_registry_is_closed_unique_and_text_free(self):
        expected = (
            LEAD_QUALITY,
            BUYER_SIGNAL,
            BULK_DELIVERABILITY,
            AI_SPAM,
            TOKEN_CONTEXT,
            FALSE_CONTEXT,
            READINESS,
            SHARED_LISTS,
            STALE_DATA,
            HUMAN_CLOSE,
            MODEL_COMPRESSION,
            SMALL_LIST,
        )

        self.assertEqual(tuple(atom.atom_id for atom in SALES_THESIS_ATOMS), expected)
        self.assertEqual(set(SALES_THESIS_ATOM_BY_ID), set(expected))
        self.assertEqual(len({atom.family_id for atom in SALES_THESIS_ATOMS}), len(expected))
        self.assertTrue(
            all(
                set(atom.to_dict())
                == {"atom_id", "family_id", "subject_id", "relation_id", "object_id"}
                for atom in SALES_THESIS_ATOMS
            )
        )

    def test_extracts_each_atom_from_direct_assertions(self):
        examples = {
            LEAD_QUALITY: "Lead quality matters more than outreach volume.",
            BUYER_SIGNAL: "A buyer signal must come before outbound.",
            BULK_DELIVERABILITY: "Mass outreach damages sender reputation.",
            AI_SPAM: "Sales AI multiplies generic outreach.",
            TOKEN_CONTEXT: (
                "Mail-merge personalization is not the same as contextual personalization."
            ),
            FALSE_CONTEXT: "Invented account context erodes message credibility.",
            READINESS: "Buyer readiness beats firmographic fit.",
            SHARED_LISTS: "Shared static prospect lists drive buyer saturation.",
            STALE_DATA: "Stale contact data wastes seller hours.",
            HUMAN_CLOSE: "Human AEs still matter in complex enterprise deals.",
            MODEL_COMPRESSION: "Foundation models commoditize thin enrichment layers.",
            SMALL_LIST: "A small, curated account list accelerates angle learning.",
        }

        for atom_id, text in examples.items():
            with self.subTest(atom_id=atom_id):
                self.assertEqual(extract_sales_thesis_atoms(text), (atom_id,))

    def test_extracts_directionally_equivalent_paraphrases(self):
        examples = {
            LEAD_QUALITY: "More outbound cannot compensate for lead quality.",
            BUYER_SIGNAL: "Outreach should wait for a buying signal.",
            BULK_DELIVERABILITY: "Deliverability suffers under spray-and-pray outbound.",
            AI_SPAM: "Template spam gets worse with AI SDRs.",
            TOKEN_CONTEXT: (
                "Context-based personalization goes beyond first-name personalization."
            ),
            FALSE_CONTEXT: "Prospect trust collapses when buyer context is fabricated.",
            READINESS: "Firmographics matter less than readiness to buy.",
            SHARED_LISTS: (
                "Audience fatigue grows when everyone works from the same list."
            ),
            STALE_DATA: "Rep hours are lost to outdated contact records.",
            HUMAN_CLOSE: "Complex enterprise deals still require human sellers.",
            MODEL_COMPRESSION: "Enrichment providers face pressure from frontier models.",
            SMALL_LIST: (
                "Message learning accelerates with a tight list of well-qualified accounts."
            ),
        }

        for atom_id, text in examples.items():
            with self.subTest(atom_id=atom_id):
                self.assertEqual(extract_sales_thesis_atoms(text), (atom_id,))

    def test_rejects_inverses_negations_and_opposite_polarity(self):
        examples = (
            "Outreach volume matters more than lead quality.",
            "Buyer signals arrive after outreach.",
            "Bulk outreach does not hurt deliverability.",
            "Sales AI reduces template spam.",
            "Token personalization is the same as contextual personalization.",
            "False account context does not damage message credibility.",
            "Firmographic fit matters more than buyer readiness.",
            "Shared static lists prevent buyer saturation.",
            "Fresh contact data saves seller time.",
            "Human sellers are unnecessary in complex deals.",
            "Foundation models strengthen enrichment layers.",
            "A small quality lead list blocks angle learning.",
            "Sales AI scales without generic outreach.",
            "Token personalization does not differ from contextual personalization.",
            "Complex deals no longer require human sellers.",
        )

        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(extract_sales_thesis_atoms(text), ())

    def test_rejects_keyword_cooccurrence_without_the_full_relation(self):
        examples = (
            "The dashboard tracks lead quality beside outreach volume.",
            "We reviewed buyer signals and outreach in the same meeting.",
            "Deliverability and mass outreach are separate agenda items.",
            "The report has chapters on Sales AI and generic outreach.",
            "Compare token personalization with contextual personalization.",
            "The form stores invented account context and a credibility score.",
            "Buyer readiness and firmographic fit are two available fields.",
            "Shared static lists and buyer saturation appear in the glossary.",
            "Stale contact data is listed beside seller hours.",
            "Human sellers discussed complex deals yesterday.",
            "Foundation models integrate with enrichment providers.",
            "A small curated lead list includes an angle-learning tag.",
        )

        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(extract_sales_thesis_atoms(text), ())

    def test_rejects_questions_hypotheticals_and_denied_claims(self):
        examples = (
            "Does lead quality matter more than outreach volume?",
            "If bulk outreach damaged sender reputation, we would stop it.",
            "Imagine Sales AI multiplied generic outreach.",
            "It is wrong to claim buyer readiness beats firmographic fit.",
            "We reject the idea that foundation models threaten enrichment layers.",
            "Do not say that stale contact data wastes seller hours.",
            "Do not believe that buyer readiness beats firmographic fit.",
            "There is no evidence that buyer readiness beats firmographic fit.",
            "The statement that buyer readiness beats firmographic fit is false.",
            "A vendor claims buyer readiness beats firmographic fit, but I disagree.",
        )

        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(extract_sales_thesis_atoms(text), ())

    def test_relation_must_be_local_to_at_most_two_adjacent_clauses(self):
        adjacent = "A buyer signal must come before; outbound starts."
        nonadjacent = (
            "Buyer signals matter; territory design also matters; "
            "outbound should follow the plan."
        )
        filler = " ".join(f"detail{index}" for index in range(MAX_THESIS_RELATION_TOKENS + 1))
        too_distant = f"Buyer readiness matters more than {filler} firmographic fit."

        self.assertEqual(extract_sales_thesis_atoms(adjacent), (BUYER_SIGNAL,))
        self.assertEqual(extract_sales_thesis_atoms(nonadjacent), ())
        self.assertEqual(extract_sales_thesis_atoms(too_distant), ())

    def test_preference_constructions_are_directional(self):
        self.assertEqual(
            extract_sales_thesis_atoms("Choose lead quality over outreach volume."),
            (LEAD_QUALITY,),
        )
        self.assertEqual(
            extract_sales_thesis_atoms("Lead quality, not outreach volume."),
            (LEAD_QUALITY,),
        )
        self.assertEqual(
            extract_sales_thesis_atoms("Not firmographic fit, but buyer readiness."),
            (READINESS,),
        )
        self.assertEqual(
            extract_sales_thesis_atoms(
                "Lead quality not only affects outreach volume; it changes forecasting."
            ),
            (),
        )

    def test_returns_families_once_and_in_registry_order(self):
        text = (
            "Lead quality matters more than outreach volume. "
            "A small curated account list accelerates angle learning. "
            "Better lead quality still matters more than email volume."
        )

        self.assertEqual(
            extract_sales_thesis_atoms(text),
            (LEAD_QUALITY, SMALL_LIST),
        )
        self.assertEqual(
            extract_sales_thesis_families(text),
            ("selection-over-scale", "targeting-learning-loop"),
        )

    def test_brief_exemption_requires_an_affirmative_full_relation(self):
        positive = "Write about why buyer signals must come before outreach."
        mention_only = "Write about buyer signals and outbound workflows."
        prohibited = "Claim to avoid: buyer signals must come before outreach."
        rebuttal = "Rebut the idea that buyer signals must come before outreach."
        question = "Explore this question: must buyer signals come before outreach?"

        self.assertEqual(explicitly_requested_sales_thesis_atoms(positive), (BUYER_SIGNAL,))
        self.assertEqual(explicitly_requested_sales_thesis_atoms(mention_only), ())
        self.assertEqual(explicitly_requested_sales_thesis_atoms(prohibited), ())
        self.assertEqual(explicitly_requested_sales_thesis_atoms(rebuttal), ())
        self.assertEqual(explicitly_requested_sales_thesis_atoms(question), ())
        self.assertTrue(brief_explicitly_requests_sales_thesis(positive, BUYER_SIGNAL))
        self.assertFalse(brief_explicitly_requests_sales_thesis(mention_only, BUYER_SIGNAL))
        self.assertFalse(brief_explicitly_requests_sales_thesis(positive, "unknown-atom"))

    def test_public_outputs_do_not_include_candidate_prose(self):
        prose = "Buyer readiness beats firmographic fit."
        atom_ids = extract_sales_thesis_atoms(prose)
        families = extract_sales_thesis_families(prose)
        receipt = SALES_THESIS_ATOM_BY_ID[atom_ids[0]].to_dict()
        serialized = json.dumps(
            {"atom_ids": atom_ids, "families": families, "receipt": receipt},
            sort_keys=True,
        )

        self.assertNotIn(prose.lower(), serialized.lower())
        self.assertNotIn("beats firmographic", serialized.lower())
        self.assertEqual(receipt["relation_id"], "outperforms")


if __name__ == "__main__":
    unittest.main()
