#!/usr/bin/env python3
"""
Confluence Page Reader
=======================
Reads Confluence Cloud page content and exports it as storage format (XHTML)
or converts it to markdown.

Supports reading by:
  - Page ID (numeric)
  - Page URL (extracts page ID automatically)
  - Title + Space key

Usage:
  # Read by URL
  python3 confluence_read.py --url "https://yourorg.atlassian.net/wiki/spaces/TT/pages/12345/Page+Title"

  # Read by page ID
  python3 confluence_read.py --page-id 12345

  # Read by title + space
  python3 confluence_read.py --title "Page Title" --space TT

  # Save to file
  python3 confluence_read.py --url "..." --output page_content.html

  # Output as markdown (best-effort conversion)
  python3 confluence_read.py --url "..." --format markdown

  # Output metadata only (title, version, labels, etc.)
  python3 confluence_read.py --url "..." --metadata-only
"""

import argparse
import json
import os
import re
import sys

try:
    import requests
    from requests.auth import HTTPBasicAuth
except ImportError:
    print("Error: 'requests' package required. Install with: pip3 install requests")
    sys.exit(1)


# ─── .env File Loading ───────────────────────────────────────────────────────

def load_dotenv(env_path=None):
    """Load variables from a .env file into os.environ (without overriding existing vars)."""
    if env_path is None:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

    if not os.path.isfile(env_path):
        return

    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = value


# ─── Confluence Reader Client ─────────────────────────────────────────────────

class ConfluenceReader:
    def __init__(self, base_url, email, api_token):
        self.base_url = base_url.rstrip("/")
        self.auth = HTTPBasicAuth(email, api_token)
        self.headers = {"Accept": "application/json", "Content-Type": "application/json"}

    def _get_v1(self, path, params=None):
        """GET against /wiki/rest/api/..."""
        url = f"{self.base_url}/rest/api/{path}"
        resp = requests.get(url, auth=self.auth, headers=self.headers, params=params)
        resp.raise_for_status()
        return resp.json()

    def _get_v2(self, path, params=None):
        """GET against /wiki/api/v2/..."""
        url = f"{self.base_url}/api/v2/{path}"
        resp = requests.get(url, auth=self.auth, headers=self.headers, params=params)
        resp.raise_for_status()
        return resp.json()

    def get_page_by_id(self, page_id, expand="body.storage,version,metadata.labels"):
        """Fetch a page by its numeric ID.

        Returns dict with: id, title, version, body (storage format), labels.
        """
        data = self._get_v1(f"content/{page_id}", params={"expand": expand})
        return self._parse_page_response(data)

    def get_page_by_title(self, space_key, title, expand="body.storage,version,metadata.labels"):
        """Find a page by title in a space.

        Returns dict with: id, title, version, body (storage format), labels.
        Returns None if not found.
        """
        data = self._get_v1("content", params={
            "spaceKey": space_key,
            "title": title,
            "expand": expand,
        })
        results = data.get("results", [])
        if not results:
            return None
        return self._parse_page_response(results[0])

    def _parse_page_response(self, data):
        """Extract useful fields from a Confluence API page response."""
        body_storage = ""
        if "body" in data and "storage" in data["body"]:
            body_storage = data["body"]["storage"].get("value", "")

        labels = []
        if "metadata" in data and "labels" in data["metadata"]:
            labels = [
                lbl.get("name", "") for lbl in data["metadata"]["labels"].get("results", [])
            ]

        version_info = data.get("version", {})

        return {
            "id": str(data.get("id", "")),
            "title": data.get("title", ""),
            "version": version_info.get("number", 0),
            "version_message": version_info.get("message", ""),
            "version_by": version_info.get("by", {}).get("displayName", ""),
            "version_when": version_info.get("when", ""),
            "space_key": data.get("space", {}).get("key", ""),
            "body_storage": body_storage,
            "labels": labels,
            "url": data.get("_links", {}).get("webui", ""),
        }


# ─── URL Parsing ──────────────────────────────────────────────────────────────

def extract_page_id_from_url(url):
    """Extract page ID from a Confluence URL.

    Supports formats:
      https://org.atlassian.net/wiki/spaces/KEY/pages/12345/Page+Title
      https://org.atlassian.net/wiki/spaces/KEY/pages/12345
    """
    match = re.search(r'/pages/(\d+)', url)
    if match:
        return match.group(1)
    return None


def extract_base_url_from_url(url):
    """Extract base URL from a Confluence page URL.

    e.g. https://greatergoods.atlassian.net/wiki/spaces/TT/pages/12345
         -> https://greatergoods.atlassian.net/wiki
    """
    match = re.match(r'(https?://[^/]+/wiki)', url)
    if match:
        return match.group(1)
    return None


# ─── Storage-to-Markdown Conversion ──────────────────────────────────────────

def storage_to_markdown(html):
    """Best-effort conversion from Confluence storage format (XHTML) to markdown.

    This handles common elements but is not a complete HTML-to-markdown converter.
    """
    if not html:
        return ""

    text = html

    # Structured macros (code blocks)
    text = re.sub(
        r'<ac:structured-macro[^>]*ac:name="code"[^>]*>.*?'
        r'<ac:parameter ac:name="language">([^<]*)</ac:parameter>.*?'
        r'<ac:plain-text-body><!\[CDATA\[(.*?)\]\]></ac:plain-text-body>.*?'
        r'</ac:structured-macro>',
        lambda m: f'\n```{m.group(1)}\n{m.group(2)}\n```\n',
        text, flags=re.DOTALL
    )
    # Code blocks without language
    text = re.sub(
        r'<ac:structured-macro[^>]*ac:name="code"[^>]*>.*?'
        r'<ac:plain-text-body><!\[CDATA\[(.*?)\]\]></ac:plain-text-body>.*?'
        r'</ac:structured-macro>',
        lambda m: f'\n```\n{m.group(1)}\n```\n',
        text, flags=re.DOTALL
    )

    # Remove remaining structured macros (info, warning, etc.)
    text = re.sub(r'<ac:structured-macro[^>]*>.*?</ac:structured-macro>', '', text, flags=re.DOTALL)
    # Remove ac: tags that aren't handled
    text = re.sub(r'</?ac:[^>]*>', '', text)
    # Remove ri: tags
    text = re.sub(r'</?ri:[^>]*>', '', text)

    # Headings
    for level in range(6, 0, -1):
        prefix = "#" * level
        text = re.sub(
            rf'<h{level}[^>]*>(.*?)</h{level}>',
            lambda m, p=prefix: f'\n{p} {_strip_tags(m.group(1))}\n',
            text, flags=re.DOTALL
        )

    # Bold
    text = re.sub(r'<strong>(.*?)</strong>', r'**\1**', text, flags=re.DOTALL)
    text = re.sub(r'<b>(.*?)</b>', r'**\1**', text, flags=re.DOTALL)

    # Italic
    text = re.sub(r'<em>(.*?)</em>', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'<i>(.*?)</i>', r'*\1*', text, flags=re.DOTALL)

    # Inline code
    text = re.sub(r'<code>(.*?)</code>', r'`\1`', text, flags=re.DOTALL)

    # Links
    text = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r'[\2](\1)', text, flags=re.DOTALL)

    # Line breaks
    text = re.sub(r'<br\s*/?>', '\n', text)

    # Horizontal rules
    text = re.sub(r'<hr\s*/?>', '\n---\n', text)

    # Tables
    text = _convert_tables(text)

    # Lists
    text = _convert_lists(text)

    # Paragraphs
    text = re.sub(r'<p[^>]*>(.*?)</p>', lambda m: f'\n{_strip_tags_light(m.group(1))}\n', text, flags=re.DOTALL)

    # Remove remaining HTML tags
    text = re.sub(r'<[^>]+>', '', text)

    # Clean up whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    # Decode HTML entities
    text = text.replace('&amp;', '&')
    text = text.replace('&lt;', '<')
    text = text.replace('&gt;', '>')
    text = text.replace('&quot;', '"')
    text = text.replace('&#39;', "'")
    text = text.replace('&nbsp;', ' ')

    return text


def _strip_tags(text):
    """Remove all HTML tags from text."""
    return re.sub(r'<[^>]+>', '', text)


def _strip_tags_light(text):
    """Remove HTML tags but preserve markdown-like formatting."""
    # Convert inline elements first
    text = re.sub(r'<strong>(.*?)</strong>', r'**\1**', text, flags=re.DOTALL)
    text = re.sub(r'<b>(.*?)</b>', r'**\1**', text, flags=re.DOTALL)
    text = re.sub(r'<em>(.*?)</em>', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'<i>(.*?)</i>', r'*\1*', text, flags=re.DOTALL)
    text = re.sub(r'<code>(.*?)</code>', r'`\1`', text, flags=re.DOTALL)
    text = re.sub(r'<a[^>]*href="([^"]*)"[^>]*>(.*?)</a>', r'[\2](\1)', text, flags=re.DOTALL)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    return text


def _convert_tables(text):
    """Convert HTML tables to markdown tables."""
    def table_replacer(match):
        table_html = match.group(0)

        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL)
        if not rows:
            return table_html

        md_rows = []
        for i, row in enumerate(rows):
            # Handle both th and td
            cells = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', row, re.DOTALL)
            cells = [_strip_tags(c).strip() for c in cells]
            if not cells:
                continue

            md_rows.append("| " + " | ".join(cells) + " |")

            # Add separator after header row
            if i == 0:
                md_rows.append("|" + "|".join(["---"] * len(cells)) + "|")

        return "\n" + "\n".join(md_rows) + "\n"

    text = re.sub(r'<table[^>]*>.*?</table>', table_replacer, text, flags=re.DOTALL)
    return text


def _convert_lists(text):
    """Convert HTML lists to markdown lists."""
    def ul_replacer(match):
        items = re.findall(r'<li[^>]*>(.*?)</li>', match.group(0), re.DOTALL)
        return "\n" + "\n".join(f"- {_strip_tags(item).strip()}" for item in items) + "\n"

    def ol_replacer(match):
        items = re.findall(r'<li[^>]*>(.*?)</li>', match.group(0), re.DOTALL)
        return "\n" + "\n".join(f"{i+1}. {_strip_tags(item).strip()}" for i, item in enumerate(items)) + "\n"

    text = re.sub(r'<ul[^>]*>.*?</ul>', ul_replacer, text, flags=re.DOTALL)
    text = re.sub(r'<ol[^>]*>.*?</ol>', ol_replacer, text, flags=re.DOTALL)
    return text


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Read Confluence page content",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Input source (mutually exclusive)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="Confluence page URL")
    source.add_argument("--page-id", help="Confluence page ID (numeric)")
    source.add_argument("--title", help="Page title (requires --space)")

    parser.add_argument("--space", help="Space key (required with --title)")
    parser.add_argument("--output", "-o", help="Output file path (default: stdout)")
    parser.add_argument(
        "--format", choices=["storage", "markdown"], default="storage",
        help="Output format: 'storage' (raw XHTML) or 'markdown' (best-effort conversion)"
    )
    parser.add_argument(
        "--metadata-only", action="store_true",
        help="Only print page metadata (title, version, labels), no body"
    )

    args = parser.parse_args()

    # Validate --title requires --space
    if args.title and not args.space:
        parser.error("--title requires --space")

    # Load credentials
    load_dotenv()

    base_url = os.environ.get("CONFLUENCE_BASE_URL", "")
    if not base_url:
        jira_base = os.environ.get("JIRA_BASE_URL", "")
        if jira_base:
            base_url = jira_base.rstrip("/") + "/wiki"

    # If using --url, derive base_url from URL if not set
    if args.url and not base_url:
        base_url = extract_base_url_from_url(args.url)

    email = os.environ.get("JIRA_EMAIL", "")
    api_token = os.environ.get("JIRA_API_TOKEN", "")

    if not all([base_url, email, api_token]):
        print("Error: Missing credentials. Set JIRA_EMAIL, JIRA_API_TOKEN, and "
              "CONFLUENCE_BASE_URL (or JIRA_BASE_URL) in .env or environment.", file=sys.stderr)
        sys.exit(1)

    reader = ConfluenceReader(base_url, email, api_token)

    # Fetch page
    try:
        if args.url:
            page_id = extract_page_id_from_url(args.url)
            if not page_id:
                print(f"Error: Could not extract page ID from URL: {args.url}", file=sys.stderr)
                sys.exit(1)
            page = reader.get_page_by_id(page_id)

        elif args.page_id:
            page = reader.get_page_by_id(args.page_id)

        elif args.title:
            page = reader.get_page_by_title(args.space, args.title)
            if page is None:
                print(f"Error: Page not found: '{args.title}' in space '{args.space}'", file=sys.stderr)
                sys.exit(1)

    except requests.HTTPError as e:
        print(f"Error: API request failed: {e}", file=sys.stderr)
        if hasattr(e, 'response') and e.response is not None:
            print(f"  Status: {e.response.status_code}", file=sys.stderr)
            print(f"  Body: {e.response.text[:500]}", file=sys.stderr)
        sys.exit(1)

    # Output
    if args.metadata_only:
        meta = {
            "id": page["id"],
            "title": page["title"],
            "version": page["version"],
            "version_by": page["version_by"],
            "version_when": page["version_when"],
            "space_key": page["space_key"],
            "labels": page["labels"],
            "url": page["url"],
            "body_length": len(page["body_storage"]),
        }
        output = json.dumps(meta, indent=2)
    elif args.format == "markdown":
        output = storage_to_markdown(page["body_storage"])
    else:
        output = page["body_storage"]

    if args.output:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Written to: {args.output}", file=sys.stderr)
        print(f"  Page: {page['title']}", file=sys.stderr)
        print(f"  Version: {page['version']}", file=sys.stderr)
        print(f"  Body length: {len(page['body_storage'])} chars", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
