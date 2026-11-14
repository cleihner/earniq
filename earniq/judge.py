"""Judge a page text against the criteria of a monitor.

The model returns **JSON**: score, summary, reasoning, a verbatim quote and the
fields defined on the monitor. The quote is what keeps the result checkable:
a number alone could be made up, a quote can be held against the stored text.

Three guardrails sit in code, not in the prompt, because a local model failed
at each of them repeatedly, even after the prompt was sharpened twice:

* **Evidence check**: the quote must appear in the page text, otherwise it is
  flagged as not found.
* **Keyword match** (`match`): list lookups are not a language task. The model
  kept insisting that a company was on a keyword list it was not on.
* **Score cap** (`cap`): comparing magnitudes is not a language task either.
  The model rated a USD 196 million contract as "above the USD 1 billion
  threshold", then, asked for the number itself, converted units (196 -> 196200)
  or invented a volume where the text named none.

Rule of thumb: anything that can be decided from a finite list or computed
belongs in code.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error

from . import config, llm


def _system() -> str:
    return f"""You check the text of a web page against given criteria.

Answer only with a JSON object, no preamble, no code block:

{{"score": 0-100,
 "summary": "two to three sentences in {config.SUMMARY_LANGUAGE} on what the page says",
 "reasoning": "one to two sentences on why this score",
 "quote": "verbatim passage from the text that carries the score, max 300 characters",
 "fields": {{}}}}

Rules:
- score is how well the criteria are met: 0 = not at all, 100 = fully.
  Be strict. An average hit is around 40-60.
- quote must appear verbatim in the text. If you find no such passage, return ""
  and keep the score low.
- fields contains exactly the keys requested below, no others.
  If a value is not in the text, use null. Never guess.
- Do not invent anything that is not in the text."""


def _field_prompt(fields: list) -> str:
    """Only the fields the model should actually fill in.

    Fields with "match" are computed afterwards from a keyword list and are
    therefore not mentioned at all; otherwise the model guesses a value it
    cannot know.
    """
    fields = [f for f in fields if not f.get("match")]
    if not fields:
        return "fields: empty object {}"
    lines = []
    for f in fields:
        name = str(f.get("name", "")).strip()
        if not name:
            continue
        desc = str(f.get("description", "")).strip()
        lines.append(f'  "{name}": {desc or "value from the text"}')
    return "fields must have exactly these keys:\n{\n" + ",\n".join(lines) + "\n}"


def evaluate(monitor: dict, source: dict, text: str, diff: str = "", max_chars: int = 12000) -> dict:
    """One verdict. Never raises: an error is part of the result."""
    t0 = time.time()
    try:
        fields = json.loads(monitor.get("fields") or "[]")
    except ValueError:
        fields = []

    shortened = (text or "")[:max_chars]
    cut = len(text or "") > max_chars
    parts = [
        f"CRITERIA:\n{monitor.get('criteria') or '(none given, judge general relevance)'}",
        _field_prompt(fields),
        f"SOURCE: {source.get('title') or ''} | {source.get('url', '')[:200]}",
    ]
    if diff:
        # On a changed page the change is the actual news.
        parts.append(f"CHANGED SINCE THE LAST FETCH:\n{diff[:3000]}")
    parts.append(f"PAGE TEXT{' (shortened)' if cut else ''}:\n{shortened}")

    msgs = [{"role": "system", "content": _system()},
            {"role": "user", "content": "\n\n".join(parts)}]

    answer, model, error, reached, finish = "", "", "", True, ""
    try:
        answer, model, finish = llm.chat(msgs, max_tokens=1000)
        model = llm.model_label(model)
        if finish == "length":
            error = "answer cut off"
    except urllib.error.HTTPError as e:
        reached = False
        error = "API key rejected" if e.code in (401, 403) else f"model answered HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 - machine off, network down, timeout
        reached = False
        error = f"model unreachable ({type(e).__name__})"

    d = llm.json_from(answer)
    try:
        score = int(round(float(d.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    score = max(0, min(100, score))

    quote = str(d.get("quote") or "").strip()[:300]
    # Cross-check: is the quote really in the text? Flag instead of silently
    # accepting: invented evidence is worse than none.
    if quote and not quote_found(quote, shortened):
        quote = f"[not found verbatim in text] {quote}"

    raw_fields = d.get("fields")
    if not isinstance(raw_fields, dict):
        raw_fields = {}
    allowed = [str(f.get("name", "")).strip() for f in fields
               if str(f.get("name", "")).strip() and not f.get("match")]
    values = {k: raw_fields.get(k) for k in allowed} if allowed else {}
    apply_matches(fields, values, shortened)
    score, capped = apply_caps(fields, values, score, shortened)

    # A verdict is usable only if the model answered AND the answer parsed as
    # JSON. Otherwise nothing is stored; see monitor.py.
    return {
        "reached": reached,
        "usable": bool(reached and d and finish != "length"),
        "score": score,
        "summary": str(d.get("summary") or "").strip()[:1500],
        "reasoning": ((f"[{capped}] " if capped else "") + str(d.get("reasoning") or "").strip())[:800],
        "quote": quote,
        "fields": json.dumps(values, ensure_ascii=False),
        "model": model,
        "duration_ms": int((time.time() - t0) * 1000),
        "error": error or ("" if d else "model answer was not JSON"),
    }


def apply_matches(fields: list, values: dict, text: str) -> None:
    """Fields that follow from a keyword list instead of from the model.

    In the configuration:
        {"name": "is_watchlist", "match": ["apple", "asml"], "against": "company"}

    `against` names the field whose value is searched; without it the whole
    page text is searched. The result is true/false, and "<name>_match" holds
    the keyword that matched.
    """
    for f in fields:
        keywords = f.get("match")
        name = str(f.get("name", "")).strip()
        if not keywords or not name:
            continue
        against = str(f.get("against") or "").strip()
        haystack = (str(values.get(against) or "") if against else text).lower()
        hit = next((k for k in keywords if str(k).lower() in haystack), "")
        values[name] = bool(hit)
        if hit:
            values[f"{name}_match"] = hit


def parse_number(value):
    """196, 196.2, "196,2", "up to 196 million" -> 196.2; otherwise None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    t = str(value).replace(" ", "").replace("\xa0", "").replace(" ", "")
    m = re.search(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?", t)
    if not m:
        return None
    z = m.group(0)
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", z):      # 1.200 / 1.200,5 (German)
        z = z.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", z):    # 1,200 / 1,200.5 (English)
        z = z.replace(",", "")
    else:
        z = z.replace(",", ".")
    return float(z)


def number_in_text(number: float, text: str) -> bool:
    """Does the number appear like this in the text? 196 / 196,2 / 196.2, or as bn/billion from 1000.

    A number without evidence counts as "no number".
    """
    t = (text or "").replace(" ", " ").replace("\xa0", " ")

    def forms(z):
        g = int(z)
        f = [f"{z:g}", f"{z:g}".replace(".", ",")]
        if z == g and g >= 1000:
            f += [f"{g:,}", f"{g:,}".replace(",", "."), f"{g:,}".replace(",", " ")]
        return f

    def appears(form, after=""):
        return re.search(r"(?<![\d.,])" + re.escape(form) + r"(?![\d]|[.,]\d)" + after, t, re.I) is not None

    if any(appears(f) for f in forms(number)):
        return True
    if number >= 1000:
        bn = number / 1000
        return any(appears(f, r"\s*(?:mrd|milliard|billion|bn|b\b)") for f in forms(bn))
    return False


def apply_caps(fields: list, values: dict, score: int, text: str = "") -> tuple[int, str]:
    """Score caps from numeric fields. The model supplies the number, code compares.

    In the configuration:
        {"name": "volume_musd", "description": "...",
         "cap": {"below": 1000, "score": 30, "if_missing": 40,
                 "when": {"topic": ["order"]}}}

    below/score:  if the value is below `below`, at most `score`.
    if_missing:   if there is no number, at most this score (omit = no cap).
    when:         the cap only applies if these fields have one of the values.
    """
    notes = []
    for f in fields:
        cap = f.get("cap")
        name = str(f.get("name", "")).strip()
        if not isinstance(cap, dict) or not name:
            continue
        condition = cap.get("when") or {}
        if any(str(values.get(k) or "").strip().lower() not in [str(x).lower() for x in v]
               for k, v in condition.items()):
            continue
        number = parse_number(values.get(name))
        if number is not None and (number <= 0 or (text and not number_in_text(number, text))):
            values[name] = None              # 0 means "not stated" for the model; no evidence = invented
            notes.append(f"{name} {number:g} not found in text" if number > 0 else "")
            number = None
        limit, why = None, ""
        if number is None:
            if cap.get("if_missing") is not None:
                limit, why = int(cap["if_missing"]), f"{name} missing"
        elif cap.get("below") is not None and number < float(cap["below"]):
            limit, why = int(cap.get("score", 30)), f"{name} {number:g} below {float(cap['below']):g}"
        if limit is not None and score > limit:
            notes.append(f"score {score} capped at {limit} ({why})")
            score = limit
    return score, "; ".join(n for n in notes if n)


def _normal(s: str) -> str:
    """For the evidence check: lower case, no punctuation, single spaces.

    Needed because page text comes from JSON or HTML and carries quotes, line
    breaks and footnote marks that the model smooths out when quoting. A
    strict comparison flagged almost every correct quote as invented.
    """
    s = re.sub(r"\\[nrt]", " ", (s or "").lower())
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def quote_found(quote: str, text: str) -> bool:
    """Counts as found if the beginning of the quote is in the text.

    Whole sentences rarely match character by character: models shorten the
    end, add ellipses, drop a footnote. The first words are enough to prove
    the passage exists.
    """
    q, t = _normal(quote), _normal(text)
    if not q or not t:
        return False
    if q in t:
        return True
    probe = " ".join(q.split()[:8])
    return len(probe) >= 20 and probe in t
