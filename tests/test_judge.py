"""The guardrails that sit in code instead of the prompt."""
import json

from earniq import judge, llm


def test_json_from_tolerates_preamble_and_fences():
    assert llm.json_from('Here you go:\n```json\n{"score": 70, "quote": "a {b}"}\n```') == {"score": 70, "quote": "a {b}"}
    assert llm.json_from("no json at all") == {}
    assert llm.json_from('{"score": 70, "summary": "cut off') == {}


def test_parse_number_handles_both_conventions():
    assert judge.parse_number("1.200,5") == 1200.5
    assert judge.parse_number("1,200.5") == 1200.5
    assert judge.parse_number("up to 196 million") == 196
    assert judge.parse_number("196,2") == 196.2
    assert judge.parse_number(None) is None
    assert judge.parse_number(True) is None


def test_number_in_text_needs_evidence():
    text = "The Navy awarded a contract worth up to USD 196 million. Backlog rose to 2.4 billion."
    assert judge.number_in_text(196, text)
    assert not judge.number_in_text(196200, text)      # unit converted by the model
    assert judge.number_in_text(2400, text)            # written as billion
    assert not judge.number_in_text(40000, text)       # invented


def test_cap_below_threshold():
    fields = [{"name": "volume_musd", "cap": {"below": 1000, "score": 30, "if_missing": 40}}]
    values = {"volume_musd": 196}
    score, note = judge.apply_caps(fields, values, 85, "a contract worth USD 196 million")
    assert score == 30 and "capped at 30" in note


def test_cap_drops_unsupported_number():
    fields = [{"name": "volume_musd", "cap": {"below": 1000, "score": 30, "if_missing": 40}}]
    values = {"volume_musd": 40000}
    score, note = judge.apply_caps(fields, values, 75, "a launch contract, no amount disclosed")
    assert values["volume_musd"] is None
    assert score == 40 and "not found in text" in note


def test_cap_respects_condition():
    fields = [{"name": "volume_musd", "cap": {"below": 1000, "score": 30, "when": {"topic": ["order"]}}}]
    values = {"volume_musd": 196, "topic": "product"}
    score, note = judge.apply_caps(fields, values, 85, "USD 196 million")
    assert score == 85 and note == ""


def test_keyword_match_is_code_not_model():
    fields = [{"name": "on_watchlist", "match": ["apple", "asml"], "against": "company"}]
    values = {"company": "Example Discount Stores"}
    judge.apply_matches(fields, values, "")
    assert values["on_watchlist"] is False
    values = {"company": "ASML Holding"}
    judge.apply_matches(fields, values, "")
    assert values["on_watchlist"] is True and values["on_watchlist_match"] == "asml"


def test_quote_found_is_lenient_on_punctuation_but_not_on_content():
    text = 'Revenue was "USD 94.9 billion",\nup 6 percent year over year.'
    assert judge.quote_found("Revenue was USD 94.9 billion, up 6 percent year over year", text)
    assert judge.quote_found("Revenue was USD 94.9 billion, up 6 percent year over year...", text)
    assert not judge.quote_found("Revenue was USD 120 billion, a record", text)


def test_evaluate_marks_invented_quote_and_applies_cap(monkeypatch):
    answer = json.dumps({"score": 85, "summary": "Order.", "reasoning": "Large order.",
                         "quote": "a record order of USD 5 billion", "fields": {"volume_musd": 196, "topic": "order"}})
    monkeypatch.setattr(llm, "chat", lambda *a, **k: (answer, "test-model", "stop"))
    monitor = {"criteria": "orders", "fields": json.dumps([
        {"name": "topic", "description": "order or other"},
        {"name": "volume_musd", "description": "amount", "cap": {"below": 1000, "score": 30}}])}
    v = judge.evaluate(monitor, {"url": "https://example.com"}, "A contract worth USD 196 million was signed.")
    assert v["usable"] and v["score"] == 30
    assert v["quote"].startswith("[not found verbatim in text]")
    assert v["reasoning"].startswith("[score 85 capped at 30")


def test_evaluate_truncated_answer_is_not_usable(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: ('{"score": 0, "summary": "half', "m", "length"))
    v = judge.evaluate({"criteria": "", "fields": "[]"}, {"url": "u"}, "text")
    assert not v["usable"]
