from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any

from leadpoet_social_brain.core.models import LeadMagnet, LeadMagnetSource, LeadMagnetVersion, utc_now
from leadpoet_social_brain.lead_magnet_scout import LEAD_MAGNET_FORMATS, normalize_format, parse_json_object
from leadpoet_social_brain.providers import DraftProvider, generate_with_provider, resolve_model_routing
from leadpoet_social_brain.storage.sqlite_store import SQLiteStore


MISSING_LEAD_MAGNET_PROVIDER = "Lead-magnet generation requires a configured LLM provider."
SUPPORTED_DEPTHS = {"quick", "standard", "deep"}
SUPPORTED_PRODUCT_INTEGRATIONS = {"light", "embedded", "strong"}
LEAD_MAGNET_STATUSES = {"needs_review", "approved", "published", "failed", "archived"}


@dataclass(frozen=True)
class LeadMagnetGenerationResult:
    item: LeadMagnet
    version: LeadMagnetVersion
    warnings: list[str]


def generate_lead_magnet(
    store: SQLiteStore,
    provider: DraftProvider | None,
    *,
    audience: str,
    sales_outcome: str,
    format: str = "prompt_library",
    depth: str = "standard",
    product_integration: str = "embedded",
    title: str | None = None,
    cta: str | None = None,
    instruction: str | None = None,
    source_ids: list[str] | None = None,
    research_run_id: str | None = None,
    evidence_summary: str | None = None,
) -> LeadMagnetGenerationResult:
    if provider is None:
        raise ValueError(MISSING_LEAD_MAGNET_PROVIDER)
    audience = clean_required(audience, "audience")
    sales_outcome = clean_required(sales_outcome, "sales_outcome")
    format = normalize_format(format)
    depth = normalize_choice(depth, SUPPORTED_DEPTHS, "standard")
    product_integration = normalize_choice(product_integration, SUPPORTED_PRODUCT_INTEGRATIONS, "embedded")
    sources = select_sources(store, source_ids)
    product_context = leadpoet_product_context(store)
    recent = store.list_lead_magnets(limit=8)
    brief = {
        "audience": audience,
        "sales_outcome": sales_outcome,
        "format": format,
        "depth": depth,
        "product_integration": product_integration,
        "title": clean_optional(title),
        "cta": clean_optional(cta),
        "instruction": clean_optional(instruction),
        "source_ids": [source.id for source in sources],
    }
    prompt = build_lead_magnet_prompt(
        brief,
        sources=sources,
        product_context=product_context,
        recent=recent,
    )
    model = resolve_model_routing(store, provider).default
    content, contract_warnings = generate_valid_content(provider, prompt, format=format, model=model)
    content = ensure_leadpoet_workflow(content, product_context=product_context)
    markdown = render_lead_magnet_markdown(content)
    warnings = list(contract_warnings)
    metric_sources = [source for source in sources if source.evidence_strength == "metric_backed"]
    if not metric_sources:
        warnings.append(
            "This draft is based on qualitative lead-magnet patterns; no selected source has observed engagement metrics."
        )
    if not product_context:
        warnings.append(
            "No saved Leadpoet product facts were available. The draft includes a clearly marked integration placeholder for review."
        )
    item = LeadMagnet(
        title=clean_text(content.get("title")) or clean_text(title) or "Untitled lead magnet",
        promise=clean_text(content.get("promise")) or sales_outcome,
        audience=audience,
        sales_outcome=sales_outcome,
        format=format,
        depth=depth,
        product_integration=product_integration,
        cta=clean_optional(content.get("cta") or cta),
        status="needs_review",
        source_ids=[source.id for source in sources],
        research_run_id=research_run_id,
        evidence_summary=clean_optional(evidence_summary) or evidence_summary_for_sources(sources),
        content=content,
        markdown=markdown,
        generation_brief=brief,
    )
    item = store.save_lead_magnet(item)
    version = store.save_lead_magnet_version(
        LeadMagnetVersion(
            lead_magnet_id=item.id,
            content=content,
            markdown=markdown,
            edit_type="generated",
            instruction=clean_optional(instruction),
            source_ids=[source.id for source in sources],
        )
    )
    return LeadMagnetGenerationResult(item=item, version=version, warnings=unique_strings(warnings))


def revise_lead_magnet(
    store: SQLiteStore,
    provider: DraftProvider | None,
    *,
    lead_magnet_id: str,
    instruction: str,
) -> LeadMagnetGenerationResult:
    if provider is None:
        raise ValueError(MISSING_LEAD_MAGNET_PROVIDER)
    item = store.get_lead_magnet(lead_magnet_id)
    if not item:
        raise ValueError("Lead magnet not found.")
    instruction = clean_required(instruction, "instruction")
    if item.status == "published":
        raise ValueError("Published lead magnets cannot be revised in place. Archive or duplicate it first.")
    sources = [source for source_id in item.source_ids if (source := store.get_lead_magnet_source(source_id))]
    prompt = f"""Revise this lead magnet without changing unsupported facts or source receipts.

Instruction:
{instruction}

Current structured content:
{json.dumps(item.content, indent=2, sort_keys=True)}

Evidence boundary:
{source_evidence_json(sources)}

Saved Leadpoet product facts:
{leadpoet_product_context(store) or 'No product facts saved. Keep the Leadpoet section explicitly marked for product review.'}

Return the complete revised compact JSON document using the same schema. Do not return commentary."""
    content, warnings = generate_valid_content(
        provider,
        prompt,
        format=item.format,
        model=resolve_model_routing(store, provider).default,
    )
    content = ensure_leadpoet_workflow(content, product_context=leadpoet_product_context(store))
    markdown = render_lead_magnet_markdown(content)
    updated = store.save_lead_magnet(
        replace(
            item,
            title=clean_text(content.get("title")) or item.title,
            promise=clean_text(content.get("promise")) or item.promise,
            cta=clean_optional(content.get("cta")) or item.cta,
            status="needs_review",
            content=content,
            markdown=markdown,
            notion_page_id=None,
            notion_url=None,
            published_at=None,
            failure_reason=None,
            updated_at=utc_now(),
        )
    )
    version = store.save_lead_magnet_version(
        LeadMagnetVersion(
            lead_magnet_id=item.id,
            content=content,
            markdown=markdown,
            edit_type="ai_revision",
            instruction=instruction,
            source_ids=item.source_ids,
        )
    )
    return LeadMagnetGenerationResult(item=updated, version=version, warnings=warnings)


def approve_lead_magnet(store: SQLiteStore, lead_magnet_id: str) -> LeadMagnet:
    item = store.get_lead_magnet(lead_magnet_id)
    if not item:
        raise ValueError("Lead magnet not found.")
    if item.status not in {"needs_review", "failed", "approved"}:
        raise ValueError(f"Lead magnet cannot be approved from status {item.status!r}.")
    return store.save_lead_magnet(replace(item, status="approved", failure_reason=None, updated_at=utc_now()))


def archive_lead_magnet(store: SQLiteStore, lead_magnet_id: str) -> LeadMagnet:
    item = store.get_lead_magnet(lead_magnet_id)
    if not item:
        raise ValueError("Lead magnet not found.")
    return store.save_lead_magnet(replace(item, status="archived", updated_at=utc_now()))


def generate_valid_content(
    provider: DraftProvider,
    prompt: str,
    *,
    format: str,
    model: str | None,
) -> tuple[dict[str, Any], list[str]]:
    raw_values = generate_with_provider(provider, prompt, variants=1, model=model)
    raw = raw_values[0] if raw_values else ""
    content = parse_json_object(raw)
    violations = lead_magnet_contract_violations(content, format=format)
    retried = False
    if violations:
        retried = True
        repair_prompt = f"""Repair the following lead-magnet response so it satisfies the contract.

Violations:
{json.dumps(violations)}

Original response:
{raw}

Return compact JSON only. Required keys: title, promise, audience, expected_outcome, overview, sections, implementation_checklist, leadpoet_workflow, cta.
sections must be an array of useful section objects with heading plus body, items, prompts, steps, or questions."""
        repaired = generate_with_provider(provider, repair_prompt, variants=1, model=model)
        raw = repaired[0] if repaired else ""
        content = parse_json_object(raw)
        violations = lead_magnet_contract_violations(content, format=format)
    if violations or not isinstance(content, dict):
        raise ValueError("Provider returned an invalid lead-magnet document: " + "; ".join(violations or ["not a JSON object"]))
    normalized = normalize_content(content)
    return normalized, (["Provider response required one schema-repair pass."] if retried else [])


def build_lead_magnet_prompt(
    brief: dict[str, Any],
    *,
    sources: list[LeadMagnetSource],
    product_context: str,
    recent: list[LeadMagnet],
) -> str:
    format_contract = {
        "prompt_library": (
            "Build a prompt repository. Include prompt entries with name, purpose, required_inputs, prompt, usage_steps, and expected_output."
        ),
        "playbook": "Build a step-by-step playbook. Include stages with objective, steps, examples, and exit criteria.",
        "template_pack": "Build a checklist and template pack. Include copyable templates, usage notes, and a completion checklist.",
        "scorecard": "Build a diagnostic scorecard. Include questions, scoring rules, score bands, and recommended actions.",
    }[str(brief["format"])]
    recent_summary = [
        {"title": item.title, "format": item.format, "promise": item.promise, "sales_outcome": item.sales_outcome}
        for item in recent
    ]
    return f"""Create an original, practical sales lead magnet for Leadpoet.

Brief:
{json.dumps(brief, indent=2, sort_keys=True)}

Format contract:
{format_contract}

Research receipts from the last 30 days:
{source_evidence_json(sources)}

Evidence rules:
- Synthesize mechanics across sources. Never copy source wording, proprietary prompts, or templates.
- Only observed metrics may support a performance claim. A qualitative source is inspiration, not proof that it performed well.
- Do not invent customer stories, conversion claims, product features, or benchmarks.
- Source notes must distinguish observations from inferences and refer to source IDs.

Saved Leadpoet product facts and positioning (closed factual set for the product step):
{product_context or 'No product facts are saved. Add a short Leadpoet integration placeholder clearly labeled for product review; make no capability claim.'}

Product integration mode: {brief['product_integration']}.
The resource must incorporate Leadpoet as one operationally useful step, not as repeated promotional copy.

Recent lead magnets to avoid repeating:
{json.dumps(recent_summary, indent=2)}

Return compact JSON only with:
- title, promise, audience, expected_outcome, overview, format
- sections: useful objects with heading and one or more of body, items, prompts, steps, templates, questions
- implementation_checklist: array of concrete actions
- leadpoet_workflow: object with heading, context_basis, steps
- source_notes: array of objects with source_id, observation, adaptation
- cta

Make the result complete enough to paste into Notion. Do not include markdown fences or commentary."""


def lead_magnet_contract_violations(content: dict[str, object] | None, *, format: str) -> list[str]:
    if not isinstance(content, dict):
        return ["response is not a JSON object"]
    violations: list[str] = []
    for key in ("title", "promise", "audience", "expected_outcome", "overview"):
        if not clean_text(content.get(key)):
            violations.append(f"{key} is required")
    sections = content.get("sections")
    if not isinstance(sections, list) or not sections:
        violations.append("sections must be a non-empty array")
    elif any(not isinstance(section, dict) or not clean_text(section.get("heading")) for section in sections):
        violations.append("each section must be an object with a heading")
    checklist = content.get("implementation_checklist")
    if not isinstance(checklist, list) or not any(clean_text(item) for item in checklist):
        violations.append("implementation_checklist must contain actions")
    workflow = content.get("leadpoet_workflow")
    if not isinstance(workflow, dict) or not isinstance(workflow.get("steps"), list) or not workflow.get("steps"):
        violations.append("leadpoet_workflow must contain steps")
    if format == "prompt_library" and isinstance(sections, list):
        has_prompts = any(isinstance(section, dict) and isinstance(section.get("prompts"), list) and section["prompts"] for section in sections)
        if not has_prompts:
            violations.append("prompt_library requires a section with prompts")
    if format == "scorecard" and isinstance(sections, list):
        has_questions = any(isinstance(section, dict) and isinstance(section.get("questions"), list) and section["questions"] for section in sections)
        if not has_questions:
            violations.append("scorecard requires a section with questions")
    return violations


def normalize_content(content: dict[str, object]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(content, default=str))
    normalized["title"] = clean_text(normalized.get("title"))
    normalized["promise"] = clean_text(normalized.get("promise"))
    normalized["audience"] = clean_text(normalized.get("audience"))
    normalized["expected_outcome"] = clean_text(normalized.get("expected_outcome"))
    normalized["overview"] = clean_text(normalized.get("overview"))
    normalized["cta"] = clean_text(normalized.get("cta"))
    return normalized


def ensure_leadpoet_workflow(content: dict[str, Any], *, product_context: str) -> dict[str, Any]:
    result = json.loads(json.dumps(content))
    workflow = result.get("leadpoet_workflow")
    if not isinstance(workflow, dict):
        workflow = {}
    steps = workflow.get("steps")
    if not isinstance(steps, list):
        steps = []
    if not any("leadpoet" in clean_text(step).lower() for step in steps):
        if product_context:
            steps.append("Use Leadpoet at this handoff according to the saved product workflow and verify the selected account context before continuing.")
        else:
            steps.append("Product review required: add the exact Leadpoet handoff after approved product facts are saved.")
    workflow["heading"] = clean_text(workflow.get("heading")) or "Where Leadpoet fits"
    workflow["context_basis"] = clean_text(workflow.get("context_basis")) or (
        "Saved Leadpoet product context" if product_context else "Product facts missing; review required"
    )
    workflow["steps"] = steps
    result["leadpoet_workflow"] = workflow
    return result


def render_lead_magnet_markdown(content: dict[str, Any]) -> str:
    lines = [f"# {clean_text(content.get('title'))}", "", clean_text(content.get("promise")), ""]
    if clean_text(content.get("audience")):
        lines.extend([f"**For:** {clean_text(content.get('audience'))}", ""])
    if clean_text(content.get("expected_outcome")):
        lines.extend([f"**Outcome:** {clean_text(content.get('expected_outcome'))}", ""])
    if clean_text(content.get("overview")):
        lines.extend(["## How to use this", "", clean_text(content.get("overview")), ""])
    for section in content.get("sections", []):
        if not isinstance(section, dict):
            continue
        lines.extend([f"## {clean_text(section.get('heading'))}", ""])
        if clean_text(section.get("body")):
            lines.extend([clean_text(section.get("body")), ""])
        append_markdown_collection(lines, section.get("items"), ordered=False)
        append_markdown_collection(lines, section.get("steps"), ordered=True)
        append_structured_entries(lines, section.get("prompts"), label="Prompt")
        append_structured_entries(lines, section.get("templates"), label="Template")
        append_structured_entries(lines, section.get("questions"), label="Question")
    workflow = content.get("leadpoet_workflow")
    if isinstance(workflow, dict):
        lines.extend([f"## {clean_text(workflow.get('heading')) or 'Where Leadpoet fits'}", ""])
        if clean_text(workflow.get("context_basis")):
            lines.extend([f"_Basis: {clean_text(workflow.get('context_basis'))}_", ""])
        append_markdown_collection(lines, workflow.get("steps"), ordered=True)
    checklist = content.get("implementation_checklist")
    if isinstance(checklist, list) and checklist:
        lines.extend(["## Implementation checklist", ""])
        for item in checklist:
            lines.append(f"- [ ] {clean_text(item)}")
        lines.append("")
    source_notes = content.get("source_notes")
    if isinstance(source_notes, list) and source_notes:
        lines.extend(["## Research notes", ""])
        for note in source_notes:
            if isinstance(note, dict):
                source_id = clean_text(note.get("source_id")) or "source"
                observation = clean_text(note.get("observation"))
                adaptation = clean_text(note.get("adaptation"))
                lines.append(f"- **{source_id}:** {observation}" + (f" Adaptation: {adaptation}" if adaptation else ""))
        lines.append("")
    if clean_text(content.get("cta")):
        lines.extend(["## Next step", "", clean_text(content.get("cta")), ""])
    return "\n".join(lines).strip() + "\n"


def append_markdown_collection(lines: list[str], value: object, *, ordered: bool) -> None:
    if not isinstance(value, list):
        return
    for index, item in enumerate(value, start=1):
        prefix = f"{index}." if ordered else "-"
        if isinstance(item, dict):
            heading = clean_text(item.get("title") or item.get("name") or item.get("heading"))
            body = clean_text(item.get("body") or item.get("description") or item.get("text") or item.get("step"))
            rendered = f"**{heading}:** {body}" if heading else body
        else:
            rendered = clean_text(item)
        if rendered:
            lines.append(f"{prefix} {rendered}")
    if value:
        lines.append("")


def append_structured_entries(lines: list[str], value: object, *, label: str) -> None:
    if not isinstance(value, list):
        return
    for index, entry in enumerate(value, start=1):
        if not isinstance(entry, dict):
            text = clean_text(entry)
            if text:
                lines.extend([f"### {label} {index}", "", text, ""])
            continue
        name = clean_text(entry.get("name") or entry.get("title")) or f"{label} {index}"
        lines.extend([f"### {name}", ""])
        for key, raw in entry.items():
            if key in {"name", "title"} or raw in (None, "", []):
                continue
            heading = key.replace("_", " ").title()
            if key in {"prompt", "template"}:
                lines.extend([f"**{heading}**", "", "```text", clean_text(raw), "```", ""])
            elif isinstance(raw, list):
                lines.append(f"**{heading}**")
                for item in raw:
                    lines.append(f"- {clean_text(item)}")
                lines.append("")
            else:
                lines.extend([f"**{heading}:** {clean_text(raw)}", ""])


def select_sources(store: SQLiteStore, source_ids: list[str] | None, *, limit: int = 8) -> list[LeadMagnetSource]:
    if source_ids is not None:
        selected = [source for source_id in source_ids if (source := store.get_lead_magnet_source(source_id))]
    else:
        selected = store.list_lead_magnet_sources(status="active", limit=limit)
    return sorted(selected, key=lambda item: (item.performance_score, item.published_at or ""), reverse=True)[:limit]


def leadpoet_product_context(store: SQLiteStore) -> str:
    sections = [
        section
        for section in store.list_context_sections(active_only=True)
        if section.kind in {"product", "positioning", "proof", "guardrail"} and clean_text(section.content)
    ]
    return "\n\n".join(f"## {section.name}\n{section.content.strip()}" for section in sections)


def source_evidence_json(sources: list[LeadMagnetSource]) -> str:
    rows = [
        {
            "source_id": source.id,
            "title": source.title,
            "url": source.source_url,
            "published_at": source.published_at,
            "format": source.format,
            "promise": source.promise,
            "audience": source.audience,
            "delivery_mechanism": source.delivery_mechanism,
            "why_it_worked": source.why_it_worked,
            "evidence_strength": source.evidence_strength,
            "performance_score": source.performance_score,
            "observed_metrics": {
                "likes": source.likes,
                "replies": source.replies,
                "reposts": source.reposts,
                "bookmarks": source.bookmarks,
                "impressions": source.impressions,
            },
        }
        for source in sources
    ]
    return json.dumps(rows, indent=2, sort_keys=True)


def evidence_summary_for_sources(sources: list[LeadMagnetSource]) -> str:
    if not sources:
        return "No research sources selected."
    metric_backed = sum(1 for source in sources if source.evidence_strength == "metric_backed")
    formats = ", ".join(sorted({source.format for source in sources}))
    return f"{len(sources)} recent source(s), {metric_backed} metric-backed. Observed formats: {formats}."


def normalize_choice(value: object, choices: set[str], default: str) -> str:
    cleaned = clean_text(value).lower().replace("-", "_").replace(" ", "_")
    return cleaned if cleaned in choices else default


def clean_required(value: object, name: str) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        raise ValueError(f"{name} is required.")
    return cleaned


def clean_text(value: object) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return " ".join(str(value or "").split())


def clean_optional(value: object) -> str | None:
    cleaned = clean_text(value)
    return cleaned or None


def unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    return [value for value in values if value and not (value in seen or seen.add(value))]
