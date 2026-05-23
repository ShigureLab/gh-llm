from gh_llm.pr_body import (
    build_pull_request_body_scaffold,
    extract_markdown_section_titles,
    normalize_section_title,
    parse_required_sections,
)


def test_parse_required_sections_deduplicates_comma_separated_values() -> None:
    assert parse_required_sections(["Motivation, Validation", "validation", "Related Issues"]) == [
        "Motivation",
        "Validation",
        "Related Issues",
    ]


def test_extract_markdown_section_titles_ignores_closing_hashes() -> None:
    body = "## Motivation ##\n\nText\n\n### Validation\n"
    assert extract_markdown_section_titles(body) == ["Motivation", "Validation"]


def test_normalize_section_title_is_case_and_punctuation_insensitive() -> None:
    assert normalize_section_title("关联 Issue：") == normalize_section_title("关联 issue")


def test_build_pull_request_body_scaffold_does_not_duplicate_existing_sections() -> None:
    scaffold = build_pull_request_body_scaffold(
        "## 关联 Issue\n\nAlready present\n",
        required_sections=["关联 issue", "验证结果"],
    )

    assert scaffold.added_sections == ("验证结果",)
    assert scaffold.body.count("## 关联 Issue") == 1
    assert "## 验证结果" in scaffold.body
