from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = PROJECT_ROOT / "skills"


@dataclass(frozen=True)
class SkillAsset:
    path: str
    content: str
    sha256: str


POST_TYPE_SKILL_FILES = {
    "founder_insight": "post_types/founder_insight.md",
    "product_update": "post_types/product_update.md",
    "bittensor": "post_types/bittensor_update.md",
    "bittensor_update": "post_types/bittensor_update.md",
    "quote_tweet": "post_types/quote_tweet.md",
}


def load_skill(path: str | Path) -> SkillAsset:
    relative_path = Path(path)
    full_path = SKILLS_ROOT / relative_path
    content = full_path.read_text(encoding="utf-8").strip()
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return SkillAsset(
        path=str(Path("skills") / relative_path),
        content=content,
        sha256=digest,
    )


def generation_skills(*, platform: str, post_type: str) -> list[SkillAsset]:
    paths = ["global_voice.md", f"platforms/{platform}.md"]
    post_type_path = POST_TYPE_SKILL_FILES.get(post_type)
    if post_type_path:
        paths.append(post_type_path)
    paths.append("review_filter.md")

    skills: list[SkillAsset] = []
    for path in paths:
        full_path = SKILLS_ROOT / path
        if full_path.exists():
            skills.append(load_skill(path))
    return skills

