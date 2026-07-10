from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from leadpoet_social_brain.core.models import ViralPattern
from leadpoet_social_brain.semantic_theses import (
    SALES_THESIS_ATOM_BY_ID,
    explicitly_requested_sales_thesis_atoms,
    extract_sales_thesis_atoms,
)

if TYPE_CHECKING:
    from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


DEFAULT_ORIGINALITY_LIMIT_PER_KIND = 1_000
MAX_ORIGINALITY_LIMIT_PER_KIND = 2_000
NEAR_COPY_MIN_CONTENT_TOKENS = 8
NEAR_COPY_SHINGLE_SIZE = 3
NEAR_COPY_SHINGLE_CONTAINMENT = 0.70
NEAR_COPY_SHINGLE_JACCARD = 0.45
COPIED_PASSAGE_CONTENT_TOKENS = 8
COPIED_PASSAGE_RAW_TOKENS = 12
SEMANTIC_HISTORY_SATURATION = 2
SEMANTIC_BATCH_SATURATION = 1


EXACT_COPY_VIOLATION = "write an original post; the draft duplicates existing workspace copy"
NEAR_COPY_VIOLATION = "write an original post; the draft too closely resembles existing workspace copy"
COPIED_PASSAGE_VIOLATION = "write an original post; the draft copies a passage from existing source material"
SEMANTIC_THESIS_VIOLATION = (
    "develop a genuinely new sales thesis; the draft repeats a saturated workspace argument"
)


_URL_PATTERN = re.compile(r"(?:https?://|www\.)\S+", flags=re.I)
_HANDLE_PATTERN = re.compile(r"(?<![\w@])@[a-z0-9_]+", flags=re.I)
_WORD_PATTERN = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?", flags=re.I)
_EXACT_IGNORED_TOKENS = {"a", "an", "the"}
_CONTENT_STOPWORDS = {
    "a",
    "about",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "he",
    "her",
    "hers",
    "him",
    "his",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "me",
    "my",
    "of",
    "on",
    "or",
    "our",
    "ours",
    "she",
    "so",
    "than",
    "that",
    "the",
    "their",
    "theirs",
    "them",
    "they",
    "this",
    "those",
    "to",
    "us",
    "was",
    "we",
    "were",
    "what",
    "which",
    "who",
    "will",
    "with",
    "would",
    "you",
    "your",
    "yours",
}


_SOURCE_PRECEDENCE = {
    "post": 60,
    "queue": 50,
    "batch": 45,
    "queue_version": 40,
    "generated_draft": 30,
    "pattern": 20,
}
_MATCH_PRECEDENCE = {"exact": 0, "near_copy": 1, "copied_passage": 2}


@dataclass(frozen=True)
class OriginalityMatch:
    """A serializable match receipt that deliberately contains no source prose."""

    match_type: str
    source_kind: str
    source_id: str
    source_kinds: tuple[str, ...]
    source_ids: tuple[str, ...]
    canonical_hash: str
    candidate_content_tokens: int
    source_content_tokens: int
    shared_content_shingles: int
    shingle_containment: float
    shingle_jaccard: float
    longest_content_span: int
    longest_raw_span: int

    def to_dict(self) -> dict[str, object]:
        return {
            "match_type": self.match_type,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "source_kinds": list(self.source_kinds),
            "source_ids": list(self.source_ids),
            "canonical_hash": self.canonical_hash,
            "candidate_content_tokens": self.candidate_content_tokens,
            "source_content_tokens": self.source_content_tokens,
            "shared_content_shingles": self.shared_content_shingles,
            "shingle_containment": round(self.shingle_containment, 6),
            "shingle_jaccard": round(self.shingle_jaccard, 6),
            "longest_content_span": self.longest_content_span,
            "longest_raw_span": self.longest_raw_span,
        }


@dataclass(frozen=True)
class SemanticThesisMatch:
    """Text-free proof that a candidate repeats a saturated relation atom."""

    atom_id: str
    family_id: str
    history_lineage_count: int
    batch_lineage_count: int
    lineage_count: int
    source_kinds: tuple[str, ...]
    lineage_hashes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "atom_id": self.atom_id,
            "family_id": self.family_id,
            "history_lineage_count": self.history_lineage_count,
            "batch_lineage_count": self.batch_lineage_count,
            "lineage_count": self.lineage_count,
            "source_kinds": list(self.source_kinds),
            "lineage_hashes": list(self.lineage_hashes),
        }


@dataclass(frozen=True)
class OriginalityCheck:
    source_count: int
    raw_source_count: int
    source_kind_counts: dict[str, int]
    matches: tuple[OriginalityMatch, ...]
    semantic_source_count: int = 0
    semantic_lineage_count: int = 0
    semantic_candidate_atoms: tuple[str, ...] = ()
    semantic_exempt_atoms: tuple[str, ...] = ()
    semantic_prior_lineage_counts: dict[str, int] = field(default_factory=dict)
    semantic_matches: tuple[SemanticThesisMatch, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.matches and not self.semantic_matches

    @property
    def violations(self) -> list[str]:
        labels = {
            "exact": EXACT_COPY_VIOLATION,
            "near_copy": NEAR_COPY_VIOLATION,
            "copied_passage": COPIED_PASSAGE_VIOLATION,
        }
        values = [labels[match.match_type] for match in self.matches]
        if self.semantic_matches:
            values.append(SEMANTIC_THESIS_VIOLATION)
        return list(dict.fromkeys(values))

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "violations": self.violations,
            "diagnostics": {
                "source_count": self.source_count,
                "raw_source_count": self.raw_source_count,
                "source_kind_counts": dict(sorted(self.source_kind_counts.items())),
                "match_count": len(self.matches),
                "matches": [match.to_dict() for match in self.matches],
                "semantic_source_count": self.semantic_source_count,
                "semantic_lineage_count": self.semantic_lineage_count,
                "semantic_candidate_atoms": list(self.semantic_candidate_atoms),
                "semantic_exempt_atoms": list(self.semantic_exempt_atoms),
                "semantic_prior_lineage_counts": dict(sorted(self.semantic_prior_lineage_counts.items())),
                "semantic_match_count": len(self.semantic_matches),
                "semantic_matches": [match.to_dict() for match in self.semantic_matches],
            },
        }


@dataclass(frozen=True)
class _RawOriginalitySource:
    source_kind: str
    source_id: str
    text: str = field(repr=False)
    occurred_at: str = ""
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class _OriginalitySource:
    source_kind: str
    source_id: str
    source_kinds: tuple[str, ...]
    source_ids: tuple[str, ...]
    text: str = field(repr=False)
    canonical_text: str = field(repr=False)
    canonical_hash: str
    raw_tokens: tuple[str, ...] = field(repr=False)
    content_tokens: tuple[str, ...] = field(repr=False)
    content_shingles: frozenset[tuple[str, ...]] = field(repr=False)
    occurred_at: str = ""


@dataclass(frozen=True)
class _RawSemanticSource:
    source_kind: str
    source_id: str
    lineage_id: str
    text: str = field(repr=False)
    occurred_at: str = ""
    is_batch: bool = False


@dataclass(frozen=True)
class _SemanticSource:
    source_kind: str
    source_id: str
    lineage_id: str = field(repr=False)
    lineage_hash: str
    atom_ids: tuple[str, ...]
    occurred_at: str = ""
    is_batch: bool = False


@dataclass(frozen=True)
class OriginalityCorpus:
    """Bounded internal source text plus safe, text-free diagnostics."""

    _sources: tuple[_OriginalitySource, ...] = field(repr=False)
    raw_source_count: int
    _semantic_sources: tuple[_SemanticSource, ...] = field(default=(), repr=False)
    _semantic_exempt_atoms: tuple[str, ...] = field(default=(), repr=False)

    @property
    def source_count(self) -> int:
        return len(self._sources)

    @property
    def source_kind_counts(self) -> dict[str, int]:
        return dict(
            Counter(
                kind
                for source in self._sources
                for kind in source.source_kinds
            )
        )

    def summary(self) -> dict[str, object]:
        semantic_counts = self.semantic_atom_lineage_counts
        return {
            "source_count": self.source_count,
            "raw_source_count": self.raw_source_count,
            "source_kind_counts": dict(sorted(self.source_kind_counts.items())),
            "semantic_source_count": len(self._semantic_sources),
            "semantic_lineage_count": len({source.lineage_hash for source in self._semantic_sources}),
            "semantic_atom_lineage_counts": dict(sorted(semantic_counts.items())),
            "semantic_saturated_atoms": sorted(
                atom_id
                for atom_id in semantic_counts
                if self._semantic_atom_is_saturated(atom_id)
            ),
        }

    @property
    def semantic_atom_lineage_counts(self) -> dict[str, int]:
        return {
            atom_id: len({source.lineage_hash for source in self._semantic_sources if atom_id in source.atom_ids})
            for atom_id in sorted({atom for source in self._semantic_sources for atom in source.atom_ids})
        }

    def semantic_prompt_section(self) -> str:
        """Bounded abstract steering only; never return historical source wording."""

        counts = self.semantic_atom_lineage_counts
        if not counts:
            return "Semantic thesis memory:\n- No recognized prior sales-thesis atoms."
        lines = ["Semantic thesis memory (relation IDs and independent-lineage counts only):"]
        for atom_id, count in sorted(counts.items()):
            status = "saturated" if self._semantic_atom_is_saturated(atom_id) else "used once"
            lines.append(f"- {atom_id}: {count} ({status})")
        lines.append(
            "Do not restate a saturated relation with synonyms unless the direct user brief explicitly states that full relation."
        )
        return "\n".join(lines)

    def check(self, text: str) -> OriginalityCheck:
        candidate = _prepared_text(text)
        matches: list[OriginalityMatch] = []
        if candidate.canonical_text:
            for source in self._sources:
                match = _match_source(candidate, source)
                if match:
                    matches.append(match)
        matches.sort(
            key=lambda match: (
                _MATCH_PRECEDENCE.get(match.match_type, 99),
                match.source_kind,
                match.source_id,
            )
        )
        candidate_atoms = extract_sales_thesis_atoms(text)
        exempt_atoms = tuple(
            atom_id for atom_id in candidate_atoms if atom_id in self._semantic_exempt_atoms
        )
        prior_counts: dict[str, int] = {}
        semantic_matches: list[SemanticThesisMatch] = []
        for atom_id in candidate_atoms:
            matching_sources = [source for source in self._semantic_sources if atom_id in source.atom_ids]
            lineage_hashes = tuple(sorted({source.lineage_hash for source in matching_sources}))
            prior_counts[atom_id] = len(lineage_hashes)
            if atom_id in exempt_atoms or not self._semantic_atom_is_saturated(atom_id):
                continue
            history_lineages = {
                source.lineage_hash for source in matching_sources if not source.is_batch
            }
            batch_lineages = {
                source.lineage_hash for source in matching_sources if source.is_batch
            }
            atom = SALES_THESIS_ATOM_BY_ID[atom_id]
            semantic_matches.append(
                SemanticThesisMatch(
                    atom_id=atom_id,
                    family_id=atom.family_id,
                    history_lineage_count=len(history_lineages),
                    batch_lineage_count=len(batch_lineages),
                    lineage_count=len(lineage_hashes),
                    source_kinds=tuple(sorted({source.source_kind for source in matching_sources})),
                    lineage_hashes=lineage_hashes,
                )
            )
        return OriginalityCheck(
            source_count=self.source_count,
            raw_source_count=self.raw_source_count,
            source_kind_counts=self.source_kind_counts,
            matches=tuple(matches),
            semantic_source_count=len(self._semantic_sources),
            semantic_lineage_count=len({source.lineage_hash for source in self._semantic_sources}),
            semantic_candidate_atoms=candidate_atoms,
            semantic_exempt_atoms=exempt_atoms,
            semantic_prior_lineage_counts=prior_counts,
            semantic_matches=tuple(semantic_matches),
        )

    def _semantic_atom_is_saturated(self, atom_id: str) -> bool:
        history = {
            source.lineage_hash
            for source in self._semantic_sources
            if not source.is_batch and atom_id in source.atom_ids
        }
        batch = {
            source.lineage_hash
            for source in self._semantic_sources
            if source.is_batch and atom_id in source.atom_ids
        }
        return (
            len(history) >= SEMANTIC_HISTORY_SATURATION
            or len(batch) >= SEMANTIC_BATCH_SATURATION
        )


@dataclass(frozen=True)
class _PreparedText:
    canonical_text: str = field(repr=False)
    canonical_hash: str
    raw_tokens: tuple[str, ...] = field(repr=False)
    content_tokens: tuple[str, ...] = field(repr=False)
    content_shingles: frozenset[tuple[str, ...]] = field(repr=False)


def build_originality_corpus(
    store: SQLiteStore,
    request: object,
    *,
    patterns: Iterable[ViralPattern] = (),
    extra_texts: Iterable[str] | None = None,
    limit_per_kind: int = DEFAULT_ORIGINALITY_LIMIT_PER_KIND,
) -> OriginalityCorpus:
    """Collect the workspace material a new final post must not reproduce.

    The corpus is deliberately independent from prompt construction. Source text
    stays inside this object and is never included in ``summary`` or match receipts.
    ``revision_source_queue_id`` is an optional internal request attribute: when
    present, only that queue item, its versions, and its directly linked generated
    draft are excluded so an in-place revision does not compare against itself.
    """

    platform = str(getattr(request, "platform", "") or "").strip()
    if not platform:
        raise ValueError("Originality corpus requires a request platform.")
    limit = min(MAX_ORIGINALITY_LIMIT_PER_KIND, max(0, int(limit_per_kind)))
    if limit == 0:
        return OriginalityCorpus(_sources=(), raw_source_count=0)

    revision_queue_id = str(
        getattr(request, "revision_source_queue_id", None) or ""
    ).strip()
    raw_sources: list[_RawOriginalitySource] = []
    raw_semantic_sources: list[_RawSemanticSource] = []

    def add_source(
        source_kind: str,
        source_id: str,
        text: object,
        *,
        occurred_at: str | None = None,
        aliases: Iterable[str] = (),
        semantic_lineage: str | None = None,
        semantic_batch: bool = False,
    ) -> None:
        cleaned = str(text or "").strip()
        if not cleaned:
            return
        raw_sources.append(
            _RawOriginalitySource(
                source_kind=source_kind,
                source_id=str(source_id),
                text=cleaned,
                occurred_at=str(occurred_at or ""),
                aliases=tuple(dict.fromkeys(str(alias) for alias in aliases if str(alias))),
            )
        )
        if semantic_lineage:
            raw_semantic_sources.append(
                _RawSemanticSource(
                    source_kind=source_kind,
                    source_id=str(source_id),
                    lineage_id=semantic_lineage,
                    text=cleaned,
                    occurred_at=str(occurred_at or ""),
                    is_batch=semantic_batch,
                )
            )

    queue_items = store.list_content_queue(
        platform=platform,
        limit=limit,
        newest_first=True,
    )
    revision_item = next(
        (item for item in queue_items if item.id == revision_queue_id),
        None,
    )
    if revision_queue_id and revision_item is None:
        revision_item = store.get_content_queue_item(revision_queue_id)
        if revision_item is not None and revision_item.platform != platform:
            revision_item = None
    excluded_draft_id = revision_item.draft_id if revision_item else None

    posts = store.list_posts(platform=platform, limit=limit)
    post_notes = store.notes_by_post_id([post.id for post in posts]) if posts else {}
    for post in posts:
        note = post_notes.get(post.id)
        aliases = ["voice_example"] if note and note.imitate else []
        add_source(
            "post",
            post.id,
            post.text,
            occurred_at=post.published_at or post.created_at,
            aliases=aliases,
            semantic_lineage=(
                None
                if excluded_draft_id and post.source_draft_id == excluded_draft_id
                else f"draft:{post.source_draft_id}" if post.source_draft_id else f"post:{post.id}"
            ),
        )

    remaining_versions = limit
    for item in queue_items:
        if item.id == revision_queue_id:
            continue
        add_source(
            "queue",
            item.id,
            item.text,
            occurred_at=item.updated_at or item.created_at,
            aliases=(f"queue:{item.status}",),
            semantic_lineage=(
                (f"draft:{item.draft_id}" if item.draft_id else f"queue:{item.id}")
                if item.status in {"needs_review", "approved", "posting", "posted"}
                else None
            ),
        )
        if remaining_versions <= 0:
            continue
        versions = store.list_queued_post_versions(item.id, limit=remaining_versions)
        for version in versions:
            add_source(
                "queue_version",
                version.id,
                version.text,
                occurred_at=version.created_at,
                aliases=(f"queue_version:{version.edit_type}",),
            )
        remaining_versions -= len(versions)

    for draft in store.list_generated_drafts(platform=platform, limit=limit):
        if excluded_draft_id and draft.id == excluded_draft_id:
            continue
        add_source(
            "generated_draft",
            draft.id,
            draft.text,
            occurred_at=draft.created_at,
        )

    for pattern in list(patterns)[:limit]:
        source_text = str(pattern.source_text or "").strip() or pattern.source_summary
        add_source(
            "pattern",
            pattern.id,
            source_text,
            occurred_at=pattern.collected_at or pattern.updated_at,
        )

    batch_values = list(extra_texts or [])[-limit:]
    for index, text in enumerate(batch_values):
        add_source(
            "batch",
            f"batch:{index}",
            text,
            semantic_lineage=f"batch:{index}",
            semantic_batch=True,
        )

    sources = _dedupe_sources(raw_sources)
    semantic_sources = _prepare_semantic_sources(raw_semantic_sources)
    return OriginalityCorpus(
        _sources=tuple(sources),
        raw_source_count=len(raw_sources),
        _semantic_sources=tuple(semantic_sources),
        _semantic_exempt_atoms=_semantic_exemptions_for_request(request),
    )


def _prepare_semantic_sources(
    raw_sources: list[_RawSemanticSource],
) -> list[_SemanticSource]:
    """Collapse storage aliases into independent, current semantic lineages."""

    by_lineage: dict[str, list[_RawSemanticSource]] = {}
    for source in raw_sources:
        by_lineage.setdefault(source.lineage_id, []).append(source)

    prepared: list[_SemanticSource] = []
    for lineage_id, rows in by_lineage.items():
        primary = max(
            rows,
            key=lambda row: (
                _SOURCE_PRECEDENCE.get(row.source_kind, 0),
                row.occurred_at,
                row.source_id,
            ),
        )
        atom_ids = extract_sales_thesis_atoms(primary.text)
        if not atom_ids:
            continue
        prepared.append(
            _SemanticSource(
                source_kind=primary.source_kind,
                source_id=primary.source_id,
                lineage_id=lineage_id,
                lineage_hash=hashlib.sha256(lineage_id.encode("utf-8")).hexdigest()[:20],
                atom_ids=atom_ids,
                occurred_at=primary.occurred_at,
                is_batch=primary.is_batch,
            )
        )

    batch = sorted(
        (source for source in prepared if source.is_batch),
        key=lambda source: (source.occurred_at, source.source_id),
        reverse=True,
    )
    history = sorted(
        (source for source in prepared if not source.is_batch),
        key=lambda source: (source.occurred_at, source.source_id),
        reverse=True,
    )
    return [*history, *batch]


def _semantic_exemptions_for_request(request: object) -> tuple[str, ...]:
    """Permit only a relation explicitly supplied by the user or a style-only revision."""

    values: list[str] = []
    if bool(getattr(request, "angle_is_factual_evidence", True)):
        values.append(str(getattr(request, "angle", "") or ""))
    if bool(getattr(request, "extra_context_is_factual_evidence", True)):
        values.append(str(getattr(request, "extra_context", "") or ""))
    values.append(str(getattr(request, "call_to_action", "") or ""))
    exemptions = list(explicitly_requested_sales_thesis_atoms("\n".join(values)))

    instruction = str(getattr(request, "revision_instruction", "") or "")
    source_text = str(getattr(request, "revision_source_text", "") or "")
    if source_text and _revision_instruction_preserves_thesis(instruction):
        exemptions.extend(extract_sales_thesis_atoms(source_text))
    return tuple(dict.fromkeys(exemptions))


def _revision_instruction_preserves_thesis(instruction: str) -> bool:
    normalized = " ".join(str(instruction or "").casefold().split())
    if not normalized:
        return False
    # Preserve phrases neutralize only their own semantic-change wording.  Any
    # other request to replace, remove, invert, or redirect the argument remains
    # disqualifying even when a style word also appears in the instruction.
    semantic_scan = re.sub(
        r"\b(?:without|do not|don't|never)\s+"
        r"(?:changing?|altering?|replacing?|removing?)\s+"
        r"(?:the\s+)?(?:angle|argument|idea|point|take|thesis|topic|focus|claim|premise|conclusion)\b",
        " ",
        normalized,
    )
    if re.search(
        r"\b(?:new|different|another|change|changing|replace|replacing|shift|switch|"
        r"reframe|remove|removing|drop|delete|cut|eliminate|omit|reverse|invert)\b.{0,32}"
        r"\b(?:angle|argument|idea|point|take|thesis|topic|focus|claim|premise|conclusion|"
        r"core message)\b|"
        r"\b(?:make|rewrite|turn)\s+(?:it|this|the post)\s+about\b|"
        r"\bfocus (?:instead )?on\b|"
        r"\b(?:argue|claim|take)\s+(?:the\s+)?opposite\b|"
        r"\bopposite\s+(?:angle|argument|idea|point|take|thesis|claim|conclusion)\b",
        semantic_scan,
    ):
        return False
    return bool(
        re.search(
            r"\b(?:shorten|shorter|concise|tighten|tighter|punchier|clearer|clarify|simplify|"
            r"tone|voice|grammar|typo|line breaks?|format|cta|product mention|wording|flow|"
            r"polish|trim)\b",
            normalized,
        )
    )


def check_originality(text: str, corpus: OriginalityCorpus) -> OriginalityCheck:
    return corpus.check(text)


def originality_violations(text: str, corpus: OriginalityCorpus) -> list[str]:
    return corpus.check(text).violations


def canonical_copy_text(text: str) -> str:
    return _prepared_text(text).canonical_text


def _dedupe_sources(raw_sources: list[_RawOriginalitySource]) -> list[_OriginalitySource]:
    grouped: dict[str, list[tuple[_RawOriginalitySource, _PreparedText]]] = {}
    for raw_source in raw_sources:
        prepared = _prepared_text(raw_source.text)
        if not prepared.canonical_text:
            continue
        grouped.setdefault(prepared.canonical_text, []).append((raw_source, prepared))

    sources: list[_OriginalitySource] = []
    for canonical_text, rows in grouped.items():
        primary, prepared = max(
            rows,
            key=lambda row: (
                _SOURCE_PRECEDENCE.get(row[0].source_kind, 0),
                row[0].occurred_at,
                row[0].source_id,
            ),
        )
        source_kinds = tuple(
            sorted(
                {
                    kind
                    for raw, _value in rows
                    for kind in (raw.source_kind, *raw.aliases)
                }
            )
        )
        source_ids = tuple(sorted({raw.source_id for raw, _value in rows}))
        sources.append(
            _OriginalitySource(
                source_kind=primary.source_kind,
                source_id=primary.source_id,
                source_kinds=source_kinds,
                source_ids=source_ids,
                text=primary.text,
                canonical_text=canonical_text,
                canonical_hash=prepared.canonical_hash,
                raw_tokens=prepared.raw_tokens,
                content_tokens=prepared.content_tokens,
                content_shingles=prepared.content_shingles,
                occurred_at=primary.occurred_at,
            )
        )
    return sorted(
        sources,
        key=lambda source: (
            -_SOURCE_PRECEDENCE.get(source.source_kind, 0),
            source.occurred_at,
            source.source_id,
        ),
        reverse=False,
    )


def _prepared_text(text: str) -> _PreparedText:
    raw_tokens = tuple(_copy_tokens(text))
    exact_tokens = tuple(token for token in raw_tokens if token not in _EXACT_IGNORED_TOKENS)
    canonical_text = " ".join(exact_tokens)
    content_tokens = tuple(
        _stem_content_token(token)
        for token in raw_tokens
        if token not in _CONTENT_STOPWORDS and not token.isdigit()
    )
    content_shingles = frozenset(_shingles(content_tokens, NEAR_COPY_SHINGLE_SIZE))
    return _PreparedText(
        canonical_text=canonical_text,
        canonical_hash=hashlib.sha256(canonical_text.encode("utf-8")).hexdigest(),
        raw_tokens=raw_tokens,
        content_tokens=content_tokens,
        content_shingles=content_shingles,
    )


def _copy_tokens(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold().replace("’", "'")
    normalized = _URL_PATTERN.sub(" ", normalized)
    normalized = _HANDLE_PATTERN.sub(" ", normalized)
    return _WORD_PATTERN.findall(normalized)


def _stem_content_token(token: str) -> str:
    value = token.strip("'")
    if value.endswith("'s"):
        value = value[:-2]
    if len(value) > 5 and value.endswith("ies"):
        return f"{value[:-3]}y"
    if len(value) > 6 and value.endswith("ing"):
        value = value[:-3]
    elif len(value) > 5 and value.endswith("ed"):
        value = value[:-2]
    elif len(value) > 5 and value.endswith("es"):
        value = value[:-2]
    elif len(value) > 4 and value.endswith("s"):
        value = value[:-1]
    return value


def _shingles(tokens: tuple[str, ...], size: int) -> set[tuple[str, ...]]:
    if len(tokens) < size:
        return set()
    return {
        tuple(tokens[index:index + size])
        for index in range(len(tokens) - size + 1)
    }


def _match_source(
    candidate: _PreparedText,
    source: _OriginalitySource,
) -> OriginalityMatch | None:
    shared_shingles = candidate.content_shingles & source.content_shingles
    min_shingles = min(len(candidate.content_shingles), len(source.content_shingles))
    union_shingles = candidate.content_shingles | source.content_shingles
    containment = len(shared_shingles) / max(1, min_shingles)
    jaccard = len(shared_shingles) / max(1, len(union_shingles))
    longest_content_span = SequenceMatcher(
        None,
        candidate.content_tokens,
        source.content_tokens,
        autojunk=False,
    ).find_longest_match().size
    longest_raw_span = SequenceMatcher(
        None,
        candidate.raw_tokens,
        source.raw_tokens,
        autojunk=False,
    ).find_longest_match().size

    match_type = ""
    if candidate.canonical_text == source.canonical_text:
        match_type = "exact"
    elif (
        min(len(candidate.content_tokens), len(source.content_tokens))
        >= NEAR_COPY_MIN_CONTENT_TOKENS
        and containment >= NEAR_COPY_SHINGLE_CONTAINMENT
        and jaccard >= NEAR_COPY_SHINGLE_JACCARD
    ):
        match_type = "near_copy"
    elif (
        longest_content_span >= COPIED_PASSAGE_CONTENT_TOKENS
        or longest_raw_span >= COPIED_PASSAGE_RAW_TOKENS
    ):
        match_type = "copied_passage"
    if not match_type:
        return None

    return OriginalityMatch(
        match_type=match_type,
        source_kind=source.source_kind,
        source_id=source.source_id,
        source_kinds=source.source_kinds,
        source_ids=source.source_ids,
        canonical_hash=source.canonical_hash,
        candidate_content_tokens=len(candidate.content_tokens),
        source_content_tokens=len(source.content_tokens),
        shared_content_shingles=len(shared_shingles),
        shingle_containment=containment,
        shingle_jaccard=jaccard,
        longest_content_span=longest_content_span,
        longest_raw_span=longest_raw_span,
    )


__all__ = [
    "COPIED_PASSAGE_VIOLATION",
    "EXACT_COPY_VIOLATION",
    "NEAR_COPY_VIOLATION",
    "SEMANTIC_THESIS_VIOLATION",
    "OriginalityCheck",
    "OriginalityCorpus",
    "OriginalityMatch",
    "SemanticThesisMatch",
    "build_originality_corpus",
    "canonical_copy_text",
    "check_originality",
    "originality_violations",
]
