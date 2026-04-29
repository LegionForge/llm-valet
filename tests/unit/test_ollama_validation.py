"""Tests for Ollama model name validation — pure logic, no HTTP required."""

import re

# Mirror the regex from providers/ollama.py so validation tests stay fast and
# isolated.  If the regex changes there, it must change here too.
_MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9:._-]+$")


def _valid(name: str) -> bool:
    return bool(_MODEL_NAME_RE.match(name))


# ── Valid names ────────────────────────────────────────────────────────────────


class TestValidModelNames:
    def test_simple_name(self) -> None:
        assert _valid("llama3")

    def test_name_with_tag(self) -> None:
        assert _valid("llama3.2:3b")

    def test_name_with_latest_tag(self) -> None:
        assert _valid("mistral:latest")

    def test_name_with_dashes(self) -> None:
        assert _valid("llama3.2-vision:11b")

    def test_name_with_underscores_and_dots(self) -> None:
        assert _valid("qwen_2.5:72b-instruct")

    def test_name_with_multiple_colons(self) -> None:
        # Ollama uses name:tag; multiple colons are technically invalid per Ollama
        # but the regex allows them — the API will reject them, not us.
        assert _valid("model:tag:extra")

    def test_uppercase(self) -> None:
        assert _valid("Llama3")

    def test_digits_only(self) -> None:
        assert _valid("123")


# ── Invalid names ──────────────────────────────────────────────────────────────


class TestInvalidModelNames:
    def test_empty_string(self) -> None:
        assert not _valid("")

    def test_space_in_name(self) -> None:
        assert not _valid("llama 3")

    def test_slash_path_traversal(self) -> None:
        assert not _valid("../../etc/passwd")

    def test_semicolon_injection(self) -> None:
        assert not _valid("llama3; rm -rf /")

    def test_pipe_injection(self) -> None:
        assert not _valid("llama3 | cat /etc/shadow")

    def test_newline_injection(self) -> None:
        assert not _valid("llama3\nINJECTED")

    def test_null_byte(self) -> None:
        assert not _valid("llama3\x00")

    def test_backtick_injection(self) -> None:
        assert not _valid("llama3`id`")

    def test_dollar_expansion(self) -> None:
        assert not _valid("llama3$HOME")

    def test_angle_brackets(self) -> None:
        assert not _valid("<script>")

    def test_parentheses(self) -> None:
        assert not _valid("model()")
