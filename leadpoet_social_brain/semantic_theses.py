from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Iterator, Mapping, Sequence


MAX_THESIS_RELATION_TOKENS = 64


@dataclass(frozen=True)
class SalesThesisAtom:
    """A stable, text-free identity for a recurring sales thesis.

    The identifiers are suitable for persistence, counts, and diagnostic receipts.
    Detection evidence is intentionally not retained on the public value.
    """

    atom_id: str
    family_id: str
    subject_id: str
    relation_id: str
    object_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "atom_id": self.atom_id,
            "family_id": self.family_id,
            "subject_id": self.subject_id,
            "relation_id": self.relation_id,
            "object_id": self.object_id,
        }


@dataclass(frozen=True)
class _ThesisRule:
    atom: SalesThesisAtom
    subject_patterns: tuple[str, ...]
    object_patterns: tuple[str, ...]
    forward_cues: tuple[str, ...]
    reverse_cues: tuple[str, ...] = ()
    preference: bool = False


def _atom(
    atom_id: str,
    family_id: str,
    subject_id: str,
    relation_id: str,
    object_id: str,
) -> SalesThesisAtom:
    return SalesThesisAtom(
        atom_id=atom_id,
        family_id=family_id,
        subject_id=subject_id,
        relation_id=relation_id,
        object_id=object_id,
    )


_RULES: tuple[_ThesisRule, ...] = (
    _ThesisRule(
        atom=_atom(
            "lead-quality-outperforms-outreach-volume",
            "selection-over-scale",
            "lead-quality",
            "outperforms",
            "outreach-volume",
        ),
        subject_patterns=(
            r"\blead quality\b",
            r"\b(?:account|prospect|target) quality\b",
            r"\bquality (?:of )?(?:the )?(?:leads?|accounts?|prospects?|targets?)\b",
            r"\b(?:better|well)[ -]qualified (?:leads?|accounts?|prospects?)\b",
            r"\b(?:the )?right (?:leads?|accounts?|buyers?|prospects?)\b",
            r"\btargeting quality\b",
        ),
        object_patterns=(
            r"\boutreach volume\b",
            r"\b(?:email|message|send) volume\b",
            r"\bvolume of (?:outreach|emails?|messages?|sends?)\b",
            r"\b(?:more|mass|high[ -]volume) (?:outreach|outbound|emails?|messages?|sends?)\b",
            r"\bnumber of (?:emails?|messages?|sends?)\b",
        ),
        forward_cues=(
            r"\bbeats?\b",
            r"\boutperforms?\b",
            r"\bmatters? more than\b",
            r"\bis (?:far )?(?:more )?(?:important|valuable|useful|effective) than\b",
            r"\btakes? priority over\b",
            r"\bwins? over\b",
            r"\bshould come before\b",
        ),
        reverse_cues=(
            r"\bmatters? less than\b",
            r"\bis less (?:important|valuable|useful|effective) than\b",
            r"\b(?:cannot|can't) (?:make up|compensate) for\b",
            r"\bdoes(?:n't| not) (?:make up|compensate) for\b",
        ),
        preference=True,
    ),
    _ThesisRule(
        atom=_atom(
            "buyer-signal-precedes-outreach",
            "signal-led-outbound",
            "buyer-signal",
            "prerequisite-for",
            "outreach",
        ),
        subject_patterns=(
            r"\bbuyer signals?\b",
            r"\bbuying signals?\b",
            r"\b(?:buyer|buying|account|intent) intent signals?\b",
            r"\baccount signals?\b",
            r"\bevidence of (?:buyer |buying )?intent\b",
            r"\bwhy[ -]now (?:signal|evidence)\b",
        ),
        object_patterns=(
            r"\boutreach\b",
            r"\boutbound\b",
            r"\boutbound (?:messages?|emails?|sequences?|motion)\b",
            r"\b(?:sales |cold )?(?:messages?|emails?|sequences?|sends?)\b",
            r"\bcontacting (?:a |the )?(?:buyer|account|prospect)\b",
        ),
        forward_cues=(
            r"\b(?:must|should|needs? to|has to) (?:come|happen|exist) before\b",
            r"\b(?:comes?|happens?|exists?) before\b",
            r"\bshould precede\b",
            r"\bis (?:a )?(?:prerequisite|required|necessary) for\b",
            r"\bis required before\b",
            r"\bmust be present before\b",
        ),
        reverse_cues=(
            r"\b(?:must|should|can only) (?:follow|wait for|start after|happen after)\b",
            r"\b(?:follows?|waits? for|starts? after|happens? after)\b",
            r"\b(?:requires?|depends? on|is conditional on)\b",
            r"\bshould not (?:start|happen|go out) without\b",
            r"\bmust not (?:start|happen|go out) without\b",
        ),
    ),
    _ThesisRule(
        atom=_atom(
            "bulk-outreach-harms-deliverability",
            "volume-externalities",
            "bulk-outreach",
            "harms",
            "deliverability",
        ),
        subject_patterns=(
            r"\b(?:bulk|mass|indiscriminate|high[ -]volume|volume[ -]first) (?:outreach|outbound|emailing|messaging|emails?|messages?|sends?)\b",
            r"\bspray[ -]and[ -]pray(?: outreach| outbound| email)?\b",
        ),
        object_patterns=(
            r"\bdeliverability\b",
            r"\b(?:sender|domain|email) reputation\b",
            r"\binbox placement\b",
            r"\bspam placement\b",
        ),
        forward_cues=(
            r"\b(?:harms?|hurts?|damages?|erodes?|undermines?|weakens?|degrades?|wrecks?)\b",
            r"\bis bad for\b",
            r"\bcomes at the expense of\b",
            r"\bcauses? (?:damage|problems?|issues?) (?:for|to)\b",
            r"\bleads? to (?:lower|worse)\b",
        ),
        reverse_cues=(
            r"\b(?:suffers?|drops?|falls?|erodes?|degrades?) (?:from|under|with|when)\b",
            r"\bis (?:harmed|hurt|damaged|eroded|undermined|weakened|degraded) by\b",
        ),
    ),
    _ThesisRule(
        atom=_atom(
            "sales-ai-amplifies-template-spam",
            "automation-externalities",
            "sales-ai",
            "amplifies",
            "template-spam",
        ),
        subject_patterns=(
            r"\bsales ai\b",
            r"\bai (?:sdrs?|sales agents?|sales reps?)\b",
            r"\bai[ -]powered (?:outreach|outbound|sales automation)\b",
            r"\b(?:outbound|sales) automation\b",
            r"\bautomated outbound\b",
        ),
        object_patterns=(
            r"\btemplate spam\b",
            r"\btemplated (?:spam|outreach|emails?|messages?)\b",
            r"\bgeneric (?:spam|outreach|emails?|messages?)\b",
            r"\bcopy[ -]paste (?:outreach|emails?|messages?)\b",
            r"\bmessage sameness\b",
        ),
        forward_cues=(
            r"\b(?:amplifies?|multiplies?|scales?|accelerates?|increases?|industrializes?|mass[ -]produces?)\b",
        ),
        reverse_cues=(
            r"\b(?:is|gets?) (?:amplified|multiplied|scaled|accelerated|worse|more common) (?:by|with|when)\b",
            r"\b(?:grows?|spreads?|worsens?) (?:with|when|because of)\b",
        ),
    ),
    _ThesisRule(
        atom=_atom(
            "token-personalization-differs-from-contextual-personalization",
            "personalization-depth",
            "token-personalization",
            "not-equivalent-to",
            "contextual-personalization",
        ),
        subject_patterns=(
            r"\btoken personalization\b",
            r"\b(?:mail[ -]merge|merge[ -]field|first[ -]name) personalization\b",
            r"\bvariable substitution\b",
            r"\b(?:name|company) tokens?\b",
            r"\binserting (?:a )?(?:first )?name\b",
        ),
        object_patterns=(
            r"\bcontextual personalization\b",
            r"\bcontext[ -]based personalization\b",
            r"\b(?:account|buyer) context\b",
            r"\breasoned personalization\b",
        ),
        forward_cues=(
            r"\bis not (?:the same as|equal to|equivalent to)\b",
            r"\bisn't (?:the same as|equal to|equivalent to)\b",
            r"\bdiffers? from\b",
            r"\bfalls? short of\b",
            r"\b(?:cannot|can't) replace\b",
            r"\bis no substitute for\b",
        ),
        reverse_cues=(
            r"\bgoes? beyond\b",
            r"\brequires? more than\b",
            r"\b(?:cannot|can't) be reduced to\b",
            r"\bis not (?:the same as|equal to|equivalent to)\b",
            r"\bisn't (?:the same as|equal to|equivalent to)\b",
            r"\bdiffers? from\b",
        ),
    ),
    _ThesisRule(
        atom=_atom(
            "false-context-harms-message-credibility",
            "context-integrity",
            "false-account-context",
            "harms",
            "message-credibility",
        ),
        subject_patterns=(
            r"\b(?:false|wrong|inaccurate|fabricated|invented|hallucinated|mistaken|made[ -]up) (?:account|buyer|company) context\b",
            r"\b(?:false|wrong|inaccurate|fabricated|invented|hallucinated|made[ -]up) context\b",
            r"\b(?:account|buyer|company) context (?:is|was) (?:false|wrong|inaccurate|fabricated|invented|hallucinated|mistaken|made[ -]up)\b",
        ),
        object_patterns=(
            r"\b(?:message|outreach|seller) credibility\b",
            r"\b(?:buyer|prospect) trust\b",
            r"\bmessage believability\b",
        ),
        forward_cues=(
            r"\b(?:harms?|hurts?|damages?|erodes?|undermines?|weakens?|destroys?|kills?)\b",
            r"\bcauses? (?:damage|problems?|issues?) (?:for|to)\b",
        ),
        reverse_cues=(
            r"\b(?:suffers?|drops?|falls?|erodes?|collapses?) (?:from|under|with|when|because of)\b",
            r"\bis (?:harmed|hurt|damaged|eroded|undermined|weakened|destroyed) by\b",
        ),
    ),
    _ThesisRule(
        atom=_atom(
            "buyer-readiness-outperforms-firmographic-fit",
            "buyer-prioritization",
            "buyer-readiness",
            "outperforms",
            "firmographic-fit",
        ),
        subject_patterns=(
            r"\bbuyer readiness\b",
            r"\bbuying readiness\b",
            r"\bpurchase readiness\b",
            r"\breadiness to (?:buy|engage|talk)\b",
            r"\bin[ -]market timing\b",
        ),
        object_patterns=(
            r"\bfirmographic fit\b",
            r"\bicp fit\b",
            r"\bcompany fit\b",
            r"\bstatic fit\b",
            r"\bfirmographics\b",
        ),
        forward_cues=(
            r"\bbeats?\b",
            r"\boutperforms?\b",
            r"\bmatters? more than\b",
            r"\bis (?:far )?(?:more )?(?:important|valuable|useful|predictive) than\b",
            r"\btakes? priority over\b",
            r"\bshould come before\b",
        ),
        reverse_cues=(
            r"\bmatters? less than\b",
            r"\bis less (?:important|valuable|useful|predictive) than\b",
            r"\b(?:cannot|can't) (?:make up|compensate) for\b",
        ),
        preference=True,
    ),
    _ThesisRule(
        atom=_atom(
            "shared-static-lists-cause-buyer-saturation",
            "data-commoditization",
            "shared-static-lists",
            "causes",
            "buyer-saturation",
        ),
        subject_patterns=(
            r"\b(?:shared|static|commodity|widely shared) (?:contact|lead|prospect|account|target|seller) lists?\b",
            r"\b(?:the )?same (?:contact|lead|prospect|account|target) lists?\b",
            r"\b(?:vendor|brokered) (?:contact|lead|prospect|account) lists?\b",
            r"\beveryone (?:uses?|works? from) (?:the )?same list\b",
        ),
        object_patterns=(
            r"\b(?:buyer|prospect|contact|audience) saturation\b",
            r"\bover[ -]contacted (?:buyers?|prospects?|accounts?)\b",
            r"\b(?:buyer|prospect|audience) fatigue\b",
            r"\b(?:same|shared) (?:buyers?|prospects?|inboxes?) (?:being )?flooded\b",
        ),
        forward_cues=(
            r"\b(?:causes?|creates?|drives?|produces?|increases?|worsens?)\b",
            r"\bleads? to\b",
        ),
        reverse_cues=(
            r"\b(?:comes?|results?|grows?|rises?|happens?) (?:from|because of|when|as)\b",
            r"\bis (?:caused|created|driven|increased|worsened) by\b",
        ),
    ),
    _ThesisRule(
        atom=_atom(
            "stale-contact-data-wastes-seller-time",
            "data-freshness",
            "stale-contact-data",
            "wastes",
            "seller-time",
        ),
        subject_patterns=(
            r"\b(?:stale|outdated|old|expired|decayed|obsolete) (?:contact|account|crm|lead|prospect) (?:data|records?|details?|information)\b",
            r"\b(?:stale|outdated|expired|obsolete) (?:contacts?|emails?|phone numbers?)\b",
        ),
        object_patterns=(
            r"\b(?:seller|sales rep|rep|sdr|ae) (?:time|hours?)\b",
            r"\bselling time\b",
            r"\b(?:hours?|time) (?:for|from) (?:sellers?|sales reps?|reps?|sdrs?|aes?)\b",
        ),
        forward_cues=(
            r"\b(?:wastes?|burns?|eats? up|steals?|drains?|costs?)\b",
        ),
        reverse_cues=(
            r"\b(?:is|gets?) wasted (?:on|by|because of)\b",
            r"\b(?:is|are|gets?|get) lost (?:to|on|because of)\b",
            r"\bdisappears? (?:into|because of)\b",
        ),
    ),
    _ThesisRule(
        atom=_atom(
            "human-sellers-retain-complex-closing-role",
            "human-ai-boundary",
            "human-seller",
            "retains-role-in",
            "complex-deal-closing",
        ),
        subject_patterns=(
            r"\bhuman (?:sellers?|sales reps?|reps?|aes?)\b",
            r"\b(?:human|people) sellers?\b",
            r"\bhuman aes?\b",
            r"\bsalespeople\b",
        ),
        object_patterns=(
            r"\b(?:complex|enterprise|nuanced|multi[ -]stakeholder) deals?\b",
            r"\bcomplex deal closing\b",
            r"\benterprise (?:deal )?closing\b",
            r"\b(?:complex|enterprise) negotiations?\b",
            r"\bmulti[ -]stakeholder (?:close|closing|sale)\b",
        ),
        forward_cues=(
            r"\b(?:still )?(?:matters?|belongs?|is (?:needed|essential|required)) (?:in|for|when closing)\b",
            r"\bremains? (?:needed|essential|required|responsible) (?:in|for)\b",
            r"\bretains? (?:a |the )?role (?:in|for)\b",
            r"\b(?:should|must) (?:own|handle|lead|close)\b",
            r"\b(?:cannot|can't) be replaced (?:in|for|when closing)\b",
        ),
        reverse_cues=(
            r"\b(?:still )?(?:needs?|requires?|depends? on)\b",
            r"\b(?:cannot|can't) (?:close|happen|work) without\b",
            r"\bshould (?:stay|remain) with\b",
            r"\bremains? (?:a )?human (?:job|responsibility) for\b",
        ),
    ),
    _ThesisRule(
        atom=_atom(
            "foundation-models-threaten-enrichment-layer",
            "model-stack-compression",
            "foundation-model",
            "threatens",
            "enrichment-layer",
        ),
        subject_patterns=(
            r"\bfoundation models?\b",
            r"\bbase models?\b",
            r"\bfrontier models?\b",
            r"\bgeneral[ -]purpose (?:ai )?models?\b",
            r"\bllms?\b",
        ),
        object_patterns=(
            r"\benrichment (?:layers?|providers?|tools?|vendors?|apis?|services?)\b",
            r"\bdata enrichment\b",
            r"\bpoint[ -]solution enrichment\b",
            r"\bthin enrichment wrappers?\b",
        ),
        forward_cues=(
            r"\b(?:threatens?|compresses?|commoditi[sz]es?|replaces?|subsumes?|erodes?|displaces?)\b",
            r"\bmakes? (?:the |an )?(?:existing |current )?.{0,24}\bobsolete\b",
        ),
        reverse_cues=(
            r"\b(?:is|are|gets?) (?:threatened|compressed|commoditi[sz]ed|replaced|subsumed|displaced) by\b",
            r"\bfaces? (?:pressure|competition|displacement) from\b",
        ),
    ),
    _ThesisRule(
        atom=_atom(
            "small-quality-list-enables-angle-learning",
            "targeting-learning-loop",
            "small-quality-list",
            "enables",
            "angle-learning",
        ),
        subject_patterns=(
            r"\b(?:small|short|tight|focused|curated|high[ -]quality) (?:account|lead|prospect|target) lists?\b",
            r"\bsmall,? (?:curated|high[ -]quality|well[ -]qualified) (?:account|lead|prospect|target) lists?\b",
            r"\b(?:small|short|tight|focused|curated|high[ -]quality) lists? of (?:well[ -]selected|well[ -]qualified|qualified|right) (?:accounts?|leads?|prospects?|targets?)\b",
            r"\b(?:a )?handful of (?:well[ -]selected|qualified|right) (?:accounts?|leads?|prospects?)\b",
            r"\bfewer,? better (?:accounts?|leads?|prospects?|targets?)\b",
        ),
        object_patterns=(
            r"\b(?:angle|message|messaging|positioning) learning\b",
            r"\blearn(?:ing)? (?:which|what) (?:angle|message|positioning) works?\b",
            r"\b(?:faster|better) (?:angle|message|positioning) feedback\b",
            r"\btest(?:ing)? (?:outreach )?(?:angles?|messages?|positioning)\b",
            r"\biterat(?:e|es|ing|ion) (?:on )?(?:angles?|messages?|positioning)\b",
            r"\b(?:angle|message|positioning) learning loop\b",
        ),
        forward_cues=(
            r"\b(?:enables?|helps?|accelerates?|improves?|sharpens?|creates?|supports?|lets?)\b",
        ),
        reverse_cues=(
            r"\b(?:improves?|accelerates?|comes?|works?) (?:faster|better)? ?(?:with|from|on)\b",
            r"\bis enabled by\b",
        ),
    ),
)


SALES_THESIS_ATOMS: tuple[SalesThesisAtom, ...] = tuple(rule.atom for rule in _RULES)
SALES_THESIS_ATOM_BY_ID: Mapping[str, SalesThesisAtom] = {
    atom.atom_id: atom for atom in SALES_THESIS_ATOMS
}


_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n+")
_CLAUSE_BOUNDARY = re.compile(
    r"\s*(?:;|\u2014|\u2013)\s*|,\s+(?=(?:but|yet|while|whereas|instead)\b)",
    flags=re.I,
)
_WORD_PATTERN = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?", flags=re.I)
_CUE_PREFIX_NEGATION = re.compile(
    r"(?:\bnot\b|\bnever\b|\bno longer\b|\bdoesn't\b|\bdoes not\b|"
    r"\bdidn't\b|\bdid not\b|\bcan't\b|\bcannot\b|\bwon't\b|\bwouldn't\b|"
    r"\brather than\b)\s+(?:\w+\s+){0,3}$",
    flags=re.I,
)
_RELATION_GAP_BLOCKER = re.compile(
    r"\b(?:not|never|neither|nor|without|prevent|prevents|prevented|reduce|reduces|reduced)\b",
    flags=re.I,
)
_NONASSERTIVE_PATTERNS = (
    re.compile(r"\?\s*$"),
    re.compile(r"^\s*(?:if|whether|suppose|supposing|imagine|imagining)\b", flags=re.I),
    re.compile(
        r"\b(?:avoid|do not|don't|never|must not|should not)\s+"
        r"(?:say|claim|argue|write|post|repeat|mention|assert|imply|use)\b",
        flags=re.I,
    ),
    re.compile(r"\b(?:claims?|ideas?|theses|arguments?) to avoid\b", flags=re.I),
    re.compile(r"\b(?:forbidden|excluded) (?:claim|idea|thesis|argument|topic)s?\b", flags=re.I),
    re.compile(
        r"\b(?:wrong|false|misleading|untrue|a myth) (?:to say|to claim|that)\b",
        flags=re.I,
    ),
    re.compile(
        r"\b(?:rebut|refute|reject|challenge|disprove|argue against)\b",
        flags=re.I,
    ),
    re.compile(
        r"\b(?:do not|don't|never|must not|should not)\s+"
        r"(?:believe|accept|assume|conclude|infer|treat)\b",
        flags=re.I,
    ),
    re.compile(
        r"\b(?:there is|there's|we have|i have)\s+no\s+"
        r"(?:credible\s+|good\s+|reliable\s+)?evidence\s+(?:that|to\s+(?:show|support|suggest))\b",
        flags=re.I,
    ),
    re.compile(
        r"\b(?:statement|claim|idea|thesis|argument)\b[^.!?\n]{0,180}"
        r"\b(?:is|was)\s+(?:false|wrong|misleading|untrue|a myth)\b",
        flags=re.I,
    ),
)
_SENTENCE_SCOPE_NONASSERTIVE_PATTERNS = (
    # A contrast boundary would otherwise expose the attributed first clause as
    # its own affirmative candidate ("A vendor claims X, but I disagree").
    re.compile(
        r"\b(?:claims?|claimed|argues?|argued|asserts?|asserted|says?|said)\b"
        r"[^.!?\n]{0,180},?\s+(?:but|yet)\s+"
        r"(?:i|we)\s+(?:disagree|reject|doubt|do not agree|don't agree)\b",
        flags=re.I,
    ),
)
_PREFERENCE_PREFIX = re.compile(r"\b(?:prioriti[sz]e|favor|choose|pick|opt for)\s*$", flags=re.I)
_PREFERENCE_CONNECTOR = re.compile(
    r"^\s*(?:over|instead of|rather than)\s*$",
    flags=re.I,
)
_PREFERENCE_FORWARD_CONTRAST = re.compile(r"^\s*,?\s*not\s*$", flags=re.I)
_PREFERENCE_REVERSE_CONTRAST = re.compile(r"\bnot\s*$", flags=re.I)
_PREFERENCE_BUT = re.compile(r"^\s*(?:,\s*)?but\b", flags=re.I)


def extract_sales_thesis_atoms(text: str) -> tuple[str, ...]:
    """Return asserted closed-ontology thesis atom IDs in registry order.

    A match needs both concepts and a directionally compatible relation cue within
    one clause or two adjacent clauses. Questions, hypotheticals, prohibitions, and
    negated relations are excluded. The return value never contains source prose.
    """

    normalized = _normalized_text(text)
    if not normalized:
        return ()
    windows = tuple(_candidate_windows(normalized))
    return tuple(
        rule.atom.atom_id
        for rule in _RULES
        if any(_rule_matches_window(rule, window) for window in windows)
    )


def extract_sales_thesis_families(text: str) -> tuple[str, ...]:
    """Return stable family IDs for asserted sales-thesis atoms."""

    atom_ids = set(extract_sales_thesis_atoms(text))
    return tuple(
        atom.family_id
        for atom in SALES_THESIS_ATOMS
        if atom.atom_id in atom_ids
    )


def explicitly_requested_sales_thesis_atoms(brief: str) -> tuple[str, ...]:
    """Return only full, affirmative thesis relations expressed by a brief.

    Keyword-only topic mentions do not qualify. Because the core extractor rejects
    questions, exclusions, and rebuttal instructions, those cannot create a
    saturation exemption either.
    """

    return extract_sales_thesis_atoms(brief)


def brief_explicitly_requests_sales_thesis(brief: str, atom_id: str) -> bool:
    """Test a brief against one known atom without exposing matched source text."""

    if atom_id not in SALES_THESIS_ATOM_BY_ID:
        return False
    return atom_id in explicitly_requested_sales_thesis_atoms(brief)


def _normalized_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = value.replace("\u2018", "'").replace("\u2019", "'")
    return re.sub(r"[ \t\f\v]+", " ", value).strip().lower()


def _candidate_windows(text: str) -> Iterator[str]:
    seen: set[str] = set()
    for sentence in _SENTENCE_BOUNDARY.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        if any(pattern.search(sentence) for pattern in _SENTENCE_SCOPE_NONASSERTIVE_PATTERNS):
            continue
        clauses = [value.strip() for value in _CLAUSE_BOUNDARY.split(sentence) if value.strip()]
        for index, clause in enumerate(clauses):
            for window in (clause, " ".join(clauses[index : index + 2])):
                cleaned = window.strip()
                if cleaned and cleaned not in seen:
                    seen.add(cleaned)
                    yield cleaned


def _rule_matches_window(rule: _ThesisRule, window: str) -> bool:
    if _is_nonassertive(window):
        return False
    subject_spans = tuple(_pattern_spans(window, rule.subject_patterns))
    object_spans = tuple(_pattern_spans(window, rule.object_patterns))
    if not subject_spans or not object_spans:
        return False
    if _ordered_relation_matches(
        window,
        subject_spans,
        object_spans,
        rule.forward_cues,
    ):
        return True
    if _ordered_relation_matches(
        window,
        object_spans,
        subject_spans,
        rule.reverse_cues,
    ):
        return True
    return rule.preference and _preference_relation_matches(
        window,
        subject_spans,
        object_spans,
    )


def _ordered_relation_matches(
    text: str,
    first_spans: Sequence[tuple[int, int]],
    second_spans: Sequence[tuple[int, int]],
    cue_patterns: Iterable[str],
) -> bool:
    cue_spans = tuple(_pattern_spans(text, cue_patterns))
    if not cue_spans:
        return False
    for first_start, first_end in first_spans:
        for cue_start, cue_end in cue_spans:
            if cue_start < first_end:
                continue
            for second_start, second_end in second_spans:
                if second_start < cue_end:
                    continue
                if not _span_within_token_limit(text[first_start:second_end]):
                    continue
                prefix = text[first_end:cue_start]
                cue_prefix = text[max(first_end, cue_start - 48):cue_start]
                gap = text[cue_end:second_start]
                if _CUE_PREFIX_NEGATION.search(cue_prefix):
                    continue
                if _RELATION_GAP_BLOCKER.search(gap):
                    continue
                if _denial_between_concept_and_cue(prefix):
                    continue
                return True
    return False


def _preference_relation_matches(
    text: str,
    subject_spans: Sequence[tuple[int, int]],
    object_spans: Sequence[tuple[int, int]],
) -> bool:
    for subject_start, subject_end in subject_spans:
        for object_start, object_end in object_spans:
            if subject_end <= object_start:
                relation_span = text[subject_start:object_end]
                between = text[subject_end:object_start]
                before_subject = text[max(0, subject_start - 48):subject_start]
                if (
                    _span_within_token_limit(relation_span)
                    and (
                        (
                            _PREFERENCE_PREFIX.search(before_subject)
                            and _PREFERENCE_CONNECTOR.match(between)
                        )
                        or _PREFERENCE_FORWARD_CONTRAST.match(between)
                    )
                ):
                    return True
            if object_end <= subject_start:
                relation_span = text[object_start:subject_end]
                between = text[object_end:subject_start]
                if (
                    _span_within_token_limit(relation_span)
                    and _PREFERENCE_REVERSE_CONTRAST.search(text[max(0, object_start - 12):object_start])
                    and _PREFERENCE_BUT.match(between)
                ):
                    return True
    return False


def _pattern_spans(text: str, patterns: Iterable[str]) -> Iterator[tuple[int, int]]:
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.I):
            yield match.span()


def _span_within_token_limit(text: str) -> bool:
    return len(_WORD_PATTERN.findall(text)) <= MAX_THESIS_RELATION_TOKENS


def _is_nonassertive(text: str) -> bool:
    return any(pattern.search(text) for pattern in _NONASSERTIVE_PATTERNS)


def _denial_between_concept_and_cue(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:doesn't|does not|didn't|did not|cannot|can't|never|no longer|without)\b",
            text,
            flags=re.I,
        )
    )


__all__ = [
    "MAX_THESIS_RELATION_TOKENS",
    "SALES_THESIS_ATOMS",
    "SALES_THESIS_ATOM_BY_ID",
    "SalesThesisAtom",
    "brief_explicitly_requests_sales_thesis",
    "explicitly_requested_sales_thesis_atoms",
    "extract_sales_thesis_atoms",
    "extract_sales_thesis_families",
]
