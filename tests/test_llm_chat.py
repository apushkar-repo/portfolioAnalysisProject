"""Tests for secure server-side portfolio chat."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.llm_chat import (
    GLOBAL_REQUEST_LIMIT,
    OPENAI_API_BASE,
    LLMConfig,
    LLMConfigurationError,
    LLMRequestError,
    ask_portfolio_assistant,
    build_analysis_context,
    load_llm_config,
    sanitize_assistant_output,
)
import app.llm_chat as llm_chat
from tests.fixture_market import run_fixture_analysis


FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "csv"


def _bundle():
    data = (FIXTURES / "portfolio_a.csv").read_bytes()
    return run_fixture_analysis(
        [("private-account-name.csv", data)],
        as_of=date(2026, 8, 14),
    )


def test_context_excludes_raw_files_and_source_filenames() -> None:
    context = build_analysis_context(_bundle(), "YTD")
    parsed = json.loads(context)

    assert parsed["selected_interval"] == "YTD"
    assert {holding["ticker"] for holding in parsed["holdings"]} == {
        "AAPL",
        "MSFT",
    }
    assert "private-account-name.csv" not in context
    assert "source_id" not in context


def test_configuration_uses_server_secrets_without_exposing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "LLM_API_KEY",
        "OPENAI_API_KEY",
        "LLM_MODEL",
        "LLM_BASE_URL",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    config = load_llm_config(
        {
            "llm": {
                "api_key": "server-secret",
                "model": "test-model",
                "base_url": "http://127.0.0.1:8080",
            }
        }
    )

    assert config.api_key == "server-secret"
    assert config.model == "test-model"
    assert not hasattr(config, "base_url")
    assert "server-secret" not in repr(config)


def test_missing_configuration_is_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(LLMConfigurationError, match="not configured"):
        load_llm_config({})


def test_assistant_receives_bounded_analysis_context() -> None:
    captured: dict = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(output_text="A plain-language answer.")

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.responses = FakeResponses()

    answer = ask_portfolio_assistant(
        config=LLMConfig(api_key="server-secret", model="test-model"),
        bundle=_bundle(),
        selected_interval="YTD",
        question="What drove my returns?",
        history=[{"role": "assistant", "content": "Earlier answer"}],
        client_factory=FakeClient,
    )

    assert answer == "A plain-language answer."
    assert captured["model"] == "test-model"
    assert captured["max_output_tokens"] == 700
    assert captured["store"] is False
    assert captured["client"]["base_url"] == OPENAI_API_BASE
    assert "private-account-name.csv" not in str(captured["input"])
    assert "server-secret" not in str(captured["input"])


def test_quota_failure_shows_support_message_without_technical_details() -> None:
    class QuotaError(Exception):
        status_code = 429
        code = "insufficient_quota"
        request_id = "req_abc123"

    class FakeResponses:
        def create(self, **kwargs):
            raise QuotaError("You exceeded your current quota")

    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

    with pytest.raises(LLMRequestError) as excinfo:
        ask_portfolio_assistant(
            config=LLMConfig(api_key="server-secret", model="test-model"),
            bundle=_bundle(),
            selected_interval="YTD",
            question="Why did this fail?",
            client_factory=FakeClient,
        )

    message = str(excinfo.value)
    assert "contact support" in message.lower()
    assert "429" not in message
    assert "insufficient_quota" not in message
    assert "req_abc123" not in message
    assert "server-secret" not in message


def test_unreachable_provider_shows_offline_message() -> None:
    class FakeClient:
        def __init__(self, **kwargs):
            raise ConnectionError("dns failure")

    with pytest.raises(
        LLMRequestError,
        match="Pulse is currently offline. Please try again in some time.",
    ):
        ask_portfolio_assistant(
            config=LLMConfig(api_key="server-secret"),
            bundle=_bundle(),
            selected_interval="YTD",
            question="Are you there?",
            client_factory=FakeClient,
        )


def test_question_length_is_limited() -> None:
    with pytest.raises(ValueError, match="too long"):
        ask_portfolio_assistant(
            config=LLMConfig(api_key="server-secret"),
            bundle=_bundle(),
            selected_interval="YTD",
            question="x" * 2_001,
        )


def test_assistant_output_removes_links_images_and_html() -> None:
    output = sanitize_assistant_output(
        "Read [this](https://evil.example) ![pixel](https://track.example/p.png) "
        "<script>alert(1)</script> https://example.com"
    )

    assert "https://" not in output
    assert "<script>" not in output
    assert "this" in output


def test_process_wide_rate_limit_cannot_be_bypassed_by_new_history() -> None:
    class FakeResponses:
        def create(self, **kwargs):
            return SimpleNamespace(output_text="ok")

    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

    with llm_chat._request_lock:
        llm_chat._request_times.clear()
    try:
        for index in range(GLOBAL_REQUEST_LIMIT):
            assert (
                ask_portfolio_assistant(
                    config=LLMConfig(api_key="server-secret"),
                    bundle=_bundle(),
                    selected_interval="YTD",
                    question=f"Question {index}",
                    history=[],
                    client_factory=FakeClient,
                )
                == "ok"
            )
        with pytest.raises(LLMRequestError, match="contact support"):
            ask_portfolio_assistant(
                config=LLMConfig(api_key="server-secret"),
                bundle=_bundle(),
                selected_interval="YTD",
                question="One too many",
                history=[],
                client_factory=FakeClient,
            )
    finally:
        with llm_chat._request_lock:
            llm_chat._request_times.clear()
