import re
import unicodedata
from dataclasses import dataclass

DEFAULT_BODY_SCAFFOLD_SECTIONS = (
    "Motivation",
    "Changes",
    "Validation",
    "Related Issues",
)

_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+(.*?)\s*$")
_MARKDOWN_LINK_RE = re.compile(r"\[(?P<label>[^\]]+)\]\([^)]*\)")


@dataclass(frozen=True)
class PullRequestBodyScaffold:
    body: str
    added_sections: tuple[str, ...]


def parse_required_sections(raw_values: list[str]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()

    for raw in raw_values:
        for part in raw.split(","):
            section = part.strip()
            if not section:
                continue
            normalized = normalize_section_title(section)
            if normalized in seen:
                continue
            seen.add(normalized)
            values.append(section)

    return values


def normalize_section_title(value: str) -> str:
    text = _MARKDOWN_LINK_RE.sub(lambda match: match.group("label"), value.strip())
    text = text.strip("# ")
    text = text.removesuffix(":").removesuffix("：")

    chars: list[str] = []
    for char in text.casefold():
        if char.isspace():
            continue
        category = unicodedata.category(char)
        if category.startswith(("L", "N")):
            chars.append(char)
    return "".join(chars)


def extract_markdown_section_titles(text: str) -> list[str]:
    titles: list[str] = []
    for match in _HEADING_RE.finditer(text):
        title = match.group(1).strip()
        title = re.sub(r"\s+#+\s*$", "", title).strip()
        if title:
            titles.append(title)
    return titles


def build_pull_request_body_scaffold(
    template_text: str | None,
    *,
    required_sections: list[str],
) -> PullRequestBodyScaffold:
    cleaned_template = (template_text or "").strip()
    if not cleaned_template:
        scaffold_sections = required_sections or list(DEFAULT_BODY_SCAFFOLD_SECTIONS)
        return PullRequestBodyScaffold(
            body=_render_section_scaffold(scaffold_sections),
            added_sections=tuple(scaffold_sections),
        )

    existing_titles = {normalize_section_title(title) for title in extract_markdown_section_titles(cleaned_template)}
    added_sections: list[str] = []
    blocks: list[str] = [cleaned_template]

    for section in required_sections:
        normalized = normalize_section_title(section)
        if normalized in existing_titles:
            continue
        existing_titles.add(normalized)
        added_sections.append(section)
        blocks.append(_render_one_section(section))

    return PullRequestBodyScaffold(
        body="\n\n".join(block.rstrip() for block in blocks if block.strip()).rstrip() + "\n",
        added_sections=tuple(added_sections),
    )


def _render_section_scaffold(sections: list[str]) -> str:
    blocks = [_render_one_section(section) for section in sections]
    return "\n\n".join(blocks).rstrip() + "\n"


def _render_one_section(section: str) -> str:
    return f"## {section}\n\n<!-- TODO: fill {section} -->"
