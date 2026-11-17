"""Keyless public search adapters; results are leads, not verified evidence."""
from html.parser import HTMLParser
import re
from urllib.parse import parse_qs, unquote, urlparse
from xml.etree import ElementTree


def bing_results(text):
    root = ElementTree.fromstring(text)
    return [{"title": item.findtext("title") or "", "url": item.findtext("link") or "",
             "snippet": item.findtext("description") or ""} for item in root.findall(".//item")]


class DuckResults(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results, self.current, self.depth = [], None, 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and "result__a" in attrs.get("class", "").split():
            url = attrs.get("href", "")
            if url.startswith("//"):
                url = "https:" + url
            redirect = parse_qs(urlparse(url).query).get("uddg")
            if redirect:
                url = redirect[0]
            self.current = {"title": "", "url": url, "snippet": ""}
        if self.current:
            self.depth += 1

    def handle_data(self, text):
        if self.current:
            self.current["title"] += text

    def handle_endtag(self, tag):
        if self.current:
            self.depth = max(0, self.depth - 1)
            if tag == "a":
                self.results.append(self.current)
                self.current, self.depth = None, 0


def duck_results(text):
    parser = DuckResults()
    parser.feed(text)
    return parser.results


def format_results(results, query, backend, limit):
    # Some public endpoints return stale/default results despite HTTP 200.
    # Reject an obviously unrelated set rather than calling that a success.
    words = set(re.findall(r"[a-z0-9][a-z0-9._-]{2,}", query.casefold())) - {
        "the", "and", "for", "with", "are", "what", "how", "all", "you", "need", "site", "https", "www", "com"}
    chinese = re.findall(r"[\u4e00-\u9fff]+", query)
    words.update(part[i:i+2] for part in chinese for i in range(len(part)-1))
    if words and not any(any(word in " ".join(str(item.get(key) or "") for key in ("title", "url", "snippet")).casefold()
                                 for word in words) for item in results):
        raise ValueError("Search returned unrelated results")
    seen, lines = set(), []
    for item in results:
        url = str(item.get("url") or "").strip()
        if urlparse(url).scheme not in {"http", "https"} or url in seen:
            continue
        seen.add(url)
        title = " ".join(str(item.get("title") or url).split())[:300]
        snippet = " ".join(str(item.get("snippet") or "").split())[:600]
        lines.append(f"- {title}\n  URL: {url}\n  {snippet}")
        if len(lines) >= limit:
            break
    if not lines:
        raise ValueError("No usable results (empty response or challenge page)")
    return f"Search backend: {backend}\nQuery: {query}\nSearch leads only; open sources to verify facts.\n" + "\n".join(lines)
