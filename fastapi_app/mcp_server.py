from mcp.server.fastmcp import FastMCP
import urllib.request
import urllib.parse
import json

# Initialize FastMCP Server
mcp = FastMCP("PublicWebSearch")

@mcp.tool()
def public_web_search(query: str) -> str:
    """CRITICAL: Use this tool to search the public internet or current events ONLY when the internal database does not have the answer."""
    # 1. Try DuckDuckGo Instant Answers first (no API key, good coverage)
    try:
        ddg_url = (
            "https://api.duckduckgo.com/?q="
            + urllib.parse.quote(query)
            + "&format=json&no_html=1&skip_disambig=1"
        )
        req = urllib.request.Request(ddg_url, headers={"User-Agent": "Mozilla/5.0 Enterprise-Agent/1.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())

        # Prefer the Abstract (topic summary) over RelatedTopics for direct answers
        if data.get("Abstract"):
            source = data.get("AbstractSource", "DuckDuckGo")
            return f"[{source}] {data['Abstract']}"

        # Fall back to related topic snippets if no abstract
        topics = data.get("RelatedTopics", [])
        snippets = []
        for t in topics[:3]:
            if isinstance(t, dict) and t.get("Text"):
                snippets.append(t["Text"])
        if snippets:
            return f"Web results for '{query}':\n" + "\n\n".join(snippets)

    except Exception as ddg_err:
        print(f"DuckDuckGo search failed: {ddg_err} — falling back to Wikipedia...")

    # 2. Wikipedia fallback
    try:
        wiki_url = (
            "https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch="
            + urllib.parse.quote(query)
            + "&utf8=&format=json"
        )
        req = urllib.request.Request(wiki_url, headers={"User-Agent": "Mozilla/5.0 Enterprise-Agent/1.0"})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
        results = data.get("query", {}).get("search", [])
        if results:
            snippet = (
                results[0]["snippet"]
                .replace('<span class="searchmatch">', "")
                .replace("</span>", "")
            )
            return f"[Wikipedia] {snippet}"
        return "No external web results found."
    except Exception as e:
        return f"Web search failed: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
