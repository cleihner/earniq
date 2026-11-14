"""Talk to an OpenAI-compatible chat completions endpoint.

Works with anything that speaks `/v1/chat/completions`: Ollama, LM Studio,
llama.cpp server, mlx_lm.server, vLLM, or a hosted API. The project was built
against a local 30B mixture-of-experts model (Qwen3-30B-A3B, 4-bit); smaller
models work, but the guardrails in `judge.py` matter more the smaller they get.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from . import config


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    key = config.llm_api_key()
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def chat(messages: list[dict], max_tokens: int, temperature: float = 0.1) -> tuple[str, str, str]:
    """(answer text, model id the server reports, finish_reason).

    The model id comes from the response, not from configuration: which model
    is actually loaded is up to the server and can change.

    finish_reason "length" means the model stopped mid-sentence. The JSON is
    then incomplete, and incomplete JSON once became a finding with score 0
    here, which looked like the verdict "not relevant" but was no verdict at
    all. Callers must check it.
    """
    body = {"messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "stream": False}
    if config.LLM_MODEL:
        body["model"] = config.LLM_MODEL
    req = urllib.request.Request(f"{config.LLM_BASE_URL}/chat/completions",
                                 json.dumps(body).encode("utf-8"), _headers())
    with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT) as f:
        d = json.loads(f.read())
    choice = (d.get("choices") or [{}])[0]
    text = choice.get("message", {}).get("content", "") or ""
    # Reasoning models put their thinking in <think> blocks; the answer follows.
    text = re.sub(r"(?is)<think>.*?</think>", "", text).strip()
    return text, str(d.get("model") or ""), str(choice.get("finish_reason") or "")


def json_from(answer: str) -> dict:
    """The first complete JSON object in the answer.

    Models like to put "Here is the result:" in front or append an
    explanation. Bracket matching instead of json.loads on the whole string.
    """
    t = re.sub(r"^```(?:json)?|```$", "", (answer or "").strip(), flags=re.MULTILINE)
    start = t.find("{")
    if start < 0:
        return {}
    depth, in_str, escaped = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except ValueError:
                    return {}
    return {}


def model_label(reported: str) -> str:
    """Some servers answer with a placeholder such as "default_model".

    Then the endpoint is the more honest label than a name that says nothing.
    """
    if reported and reported != "default_model":
        return reported
    return config.LLM_MODEL or f"local ({config.LLM_BASE_URL.split('//')[-1]})"


def ping() -> dict:
    """Health of the model endpoint.

    Distinguishes three cases because they have different causes: unreachable
    (machine off, network, proxy), key rejected (auth), and fine. `/models`
    lists what the server offers, not necessarily what is loaded; which model
    answered is recorded on every single finding.
    """
    try:
        req = urllib.request.Request(f"{config.LLM_BASE_URL}/models", headers=_headers())
        with urllib.request.urlopen(req, timeout=6) as f:
            f.read(1)
        return {"ok": True, "reason": "", "url": config.LLM_BASE_URL, "model": config.LLM_MODEL}
    except urllib.error.HTTPError as e:
        reason = "API key missing or rejected" if e.code in (401, 403) else f"HTTP {e.code}"
        return {"ok": False, "reason": reason, "url": config.LLM_BASE_URL, "model": config.LLM_MODEL}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": type(e).__name__, "url": config.LLM_BASE_URL, "model": config.LLM_MODEL}
