from __future__ import annotations

import html
import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any

import httpx


def _clean(value: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


async def current_search(arguments: dict[str, Any], timeout_seconds: int = 20) -> dict[str, Any]:
    query = str(arguments.get("query") or "").strip()
    mode = str(arguments.get("mode") or "current").lower()
    limit = min(max(int(arguments.get("limit") or 5), 1), 8)
    if not query:
        return {"status": "invalid_request", "message": "A search query is required.", "results": []}
    executed_at = datetime.now(UTC).isoformat()
    headers = {"User-Agent": "XV12/1.0 local assistant"}
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True, headers=headers) as client:
            if mode in {"news", "current"}:
                response = await client.get(
                    "https://www.bing.com/news/search",
                    params={"q": query, "format": "rss"},
                )
                response.raise_for_status()
                root = ET.fromstring(response.text)
                results = []
                for item in root.findall(".//item")[:limit]:
                    results.append(
                        {
                            "title": _clean(item.findtext("title")),
                            "url": (item.findtext("link") or "").strip(),
                            "snippet": _clean(item.findtext("description")),
                            "published_at": _clean(item.findtext("pubDate")),
                            "source": "Bing News RSS",
                        }
                    )
                provider = "Bing News RSS"
            else:
                response = await client.get("https://html.duckduckgo.com/html/", params={"q": query})
                response.raise_for_status()
                anchors = re.findall(
                    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                    response.text,
                    flags=re.I | re.S,
                )
                snippets = re.findall(
                    r'<(?:a|div)[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div)>',
                    response.text,
                    flags=re.I | re.S,
                )
                results = []
                for index, (url, title) in enumerate(anchors[:limit]):
                    decoded = html.unescape(url)
                    parsed = urllib.parse.urlparse(decoded)
                    if parsed.netloc.endswith("duckduckgo.com"):
                        decoded = urllib.parse.parse_qs(parsed.query).get("uddg", [decoded])[0]
                    results.append(
                        {
                            "title": _clean(title),
                            "url": decoded,
                            "snippet": _clean(snippets[index] if index < len(snippets) else ""),
                            "published_at": None,
                            "source": "DuckDuckGo HTML",
                        }
                    )
                provider = "DuckDuckGo HTML"
        for index, item in enumerate(results, start=1):
            item["reference"] = f"web:{index}"
        return {
            "status": "verified_results" if results else "no_result",
            "query": query,
            "mode": mode,
            "executed_at": executed_at,
            "provider": provider,
            "results": results,
            "evidence": {"executed": True, "result_count": len(results)},
        }
    except Exception as error:
        return {
            "status": "provider_unavailable",
            "query": query,
            "executed_at": executed_at,
            "results": [],
            "evidence": {"executed": False, "error": type(error).__name__},
        }
