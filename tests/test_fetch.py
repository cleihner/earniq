from earniq import fetch


def test_text_prefers_main_and_drops_scripts():
    html = ("<html><head><title>IR</title><script>var x=1</script></head><body><nav>Menu</nav>"
            "<main>" + "<p>Q3 results: revenue up 12 percent.</p>" * 20 + "</main></body></html>")
    t = fetch.text_from_html(html)
    assert "revenue up 12 percent" in t and "var x" not in t and "Menu" not in t


def test_block_detection():
    assert fetch.blocked({"http_code": 402, "text": ""}).startswith("blocked (HTTP 402")
    assert fetch.blocked({"http_code": 200, "text": "Just a moment... checking your browser"}).startswith("blocked")
    long_article = "access denied " + "real content " * 400
    assert fetch.blocked({"http_code": 200, "text": long_article}) == ""


def test_diff_lists_added_and_removed_lines():
    d = fetch.diff("Q2 results\nOld item", "Q2 results\nQ3 results published")
    assert "+ Q3 results published" in d and "- Old item" in d


def test_sec_gets_declared_user_agent(monkeypatch):
    from earniq import config
    monkeypatch.setattr(config, "sec_user_agent", lambda: "Jane Doe jane@example.com")
    assert fetch.headers_for("https://www.sec.gov/cgi-bin/browse-edgar")["User-Agent"] == "Jane Doe jane@example.com"
    assert fetch.headers_for("https://example.com")["User-Agent"] == fetch.UA
