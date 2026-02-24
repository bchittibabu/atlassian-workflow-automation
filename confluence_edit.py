#!/usr/bin/env python3
"""
Confluence Page Editor
======================
Updates an existing Confluence Cloud page's content from a markdown file,
a raw storage-format file, or stdin.

Target page by URL, page ID, or title+space. Supports dry-run,
version message, and optional title rename.

Usage:
  # Update page from a markdown file (identified by URL)
  python3 confluence_edit.py --url "https://org.atlassian.net/wiki/spaces/TT/pages/12345/Page" \\
      --file content.md

  # Update page from a markdown file (identified by page ID)
  python3 confluence_edit.py --page-id 12345 --file content.md

  # Update page by title + space
  python3 confluence_edit.py --title "Page Title" --space TT --file content.md

  # Dry run (preview without making changes)
  python3 confluence_edit.py --url "..." --file content.md --dry-run

  # Update with a version message
  python3 confluence_edit.py --url "..." --file content.md --message "Updated observation format"

  # Upload raw storage format (XHTML) instead of markdown
  python3 confluence_edit.py --url "..." --file content.html --input-format storage

  # Read content from stdin
  echo "# Hello" | python3 confluence_edit.py --url "..." --stdin

  # Rename the page title while updating
  python3 confluence_edit.py --url "..." --file content.md --rename "New Title"

  # Append content to existing page instead of replacing
  python3 confluence_edit.py --url "..." --file extra.md --append

  # Replace only a section (by heading) in the existing page
  python3 confluence_edit.py --url "..." --file chapter3.md \\
      --replace-section "Chapter 3"
"""

import argparse
import json
import os
import re
import sys
import time

try:
    import requests
    from requests.auth import HTTPBasicAuth
except ImportError:
    print("Error: 'requests' package required. Install with: pip3 install requests")
    sys.exit(1)


# ─── .env File Loading ───────────────────────────────────────────────────────

def load_dotenv(env_path=None):
    """Load variables from a .env file into os.environ (without overriding)."""
    if env_path is None:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key not in os.environ:
                os.environ[key] = value


# ─── URL Parsing ──────────────────────────────────────────────────────────────

def extract_page_id_from_url(url):
    """Extract page ID from a Confluence URL."""
    match = re.search(r'/pages/(\d+)', url)
    return match.group(1) if match else None


def extract_base_url_from_url(url):
    """Extract base URL (up to /wiki) from a Confluence page URL."""
    match = re.match(r'(https?://[^/]+/wiki)', url)
    return match.group(1) if match else None


# ─── Confluence API Client ────────────────────────────────────────────────────

class ConfluenceEditor:
    def __init__(self, base_url, email, api_token):
        self.base_url = base_url.rstrip("/")
        self.auth = HTTPBasicAuth(email, api_token)
        self.headers = {"Accept": "application/json", "Content-Type": "application/json"}

    def _get_v1(self, path, params=None):
        url = f"{self.base_url}/rest/api/{path}"
        resp = requests.get(url, auth=self.auth, headers=self.headers, params=params)
        resp.raise_for_status()
        return resp.json()

    def _put_v2(self, path, payload):
        url = f"{self.base_url}/api/v2/{path}"
        resp = requests.put(url, auth=self.auth, headers=self.headers, json=payload)
        if not resp.ok:
            print(f"  ERROR {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
        resp.raise_for_status()
        return resp.json()

    def _post_v1(self, path, payload):
        url = f"{self.base_url}/rest/api/{path}"
        resp = requests.post(url, auth=self.auth, headers=self.headers, json=payload)
        if not resp.ok:
            print(f"  ERROR {resp.status_code}: {resp.text[:500]}", file=sys.stderr)
        resp.raise_for_status()
        return resp.json()

    def get_page(self, page_id):
        """Fetch page metadata + body by ID."""
        data = self._get_v1(f"content/{page_id}", params={
            "expand": "body.storage,version,space,metadata.labels"
        })
        return {
            "id": str(data["id"]),
            "title": data.get("title", ""),
            "version": data.get("version", {}).get("number", 0),
            "space_key": data.get("space", {}).get("key", ""),
            "body_storage": data.get("body", {}).get("storage", {}).get("value", ""),
            "labels": [
                lbl["name"] for lbl in
                data.get("metadata", {}).get("labels", {}).get("results", [])
            ],
        }

    def find_page_by_title(self, space_key, title):
        """Find a page by title in a space. Returns page dict or None."""
        data = self._get_v1("content", params={
            "spaceKey": space_key,
            "title": title,
            "expand": "body.storage,version,space,metadata.labels",
        })
        results = data.get("results", [])
        if not results:
            return None
        page = results[0]
        return {
            "id": str(page["id"]),
            "title": page.get("title", ""),
            "version": page.get("version", {}).get("number", 0),
            "space_key": page.get("space", {}).get("key", ""),
            "body_storage": page.get("body", {}).get("storage", {}).get("value", ""),
            "labels": [
                lbl["name"] for lbl in
                page.get("metadata", {}).get("labels", {}).get("results", [])
            ],
        }

    def update_page(self, page_id, title, body_storage, current_version, message=""):
        """Update an existing page. Returns the new version number."""
        payload = {
            "id": str(page_id),
            "status": "current",
            "title": title,
            "body": {
                "representation": "storage",
                "value": body_storage,
            },
            "version": {
                "number": current_version + 1,
                "message": message or "Updated by confluence_edit.py",
            },
        }
        result = self._put_v2(f"pages/{page_id}", payload)
        return result.get("version", {}).get("number", current_version + 1)

    def set_labels(self, page_id, labels):
        """Add labels to a page."""
        if not labels:
            return
        label_payload = [{"prefix": "global", "name": lbl} for lbl in labels]
        try:
            self._post_v1(f"content/{page_id}/label", label_payload)
        except Exception as e:
            print(f"  Warning: Could not set labels: {e}", file=sys.stderr)


# ─── Markdown to Confluence Storage Format ────────────────────────────────────
# (Reused from confluence_publish.py)

def _inline_format(text):
    """Handle inline markdown: **bold**, *italic*, `code`, [text](url)."""
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', text)
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    text = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2">\1</a>', text)
    return text


def _join_multiline_links(text):
    """Pre-process markdown to join multi-line link syntax into single lines.

    Handles patterns like:
        - [Chapter 2 GHS Interconnect
          Profile](#Chapter-2-GHS-Interconnect-Profile)
    """
    lines = text.split('\n')
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if '[' in line:
            brackets = line.count('[') - line.count(']')
            while brackets > 0 and i + 1 < len(lines):
                i += 1
                continuation = lines[i].strip()
                line = line.rstrip() + ' ' + continuation
                brackets = line.count('[') - line.count(']')
        result.append(line)
        i += 1
    return '\n'.join(result)


def _build_nested_ul(items):
    """Convert list of (indent_level, text) tuples into nested <ul><li> HTML."""
    if not items:
        return ""

    # Map raw indent values to normalized depth levels
    indent_vals = sorted(set(ind for ind, _ in items))
    depth_map = {v: i for i, v in enumerate(indent_vals)}

    result = []
    depth = -1

    for indent, text in items:
        target = depth_map[indent]

        while depth < target:
            result.append("<ul>")
            depth += 1

        while depth > target:
            result.append("</li></ul>")
            depth -= 1

        if depth == target and result and not result[-1].endswith("<ul>"):
            result.append("</li>")

        result.append(f"<li>{_inline_format(text)}")

    while depth >= 0:
        result.append("</li></ul>")
        depth -= 1

    return "".join(result)


def _make_code_macro(code, language=""):
    """Build a Confluence code block macro."""
    lang_param = ""
    if language:
        lang_param = f'<ac:parameter ac:name="language">{language}</ac:parameter>'
    return (
        f'<ac:structured-macro ac:name="code" ac:schema-version="1">'
        f'{lang_param}'
        f'<ac:plain-text-body><![CDATA[{code}]]></ac:plain-text-body>'
        f'</ac:structured-macro>'
    )


def _make_table(lines):
    """Convert markdown table lines to HTML table."""
    rows = []
    for idx, line in enumerate(lines):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if idx == 1 and all(set(c.strip()) <= set("-: ") for c in cells):
            continue
        tag = "th" if idx == 0 else "td"
        cells_html = "".join(f"<{tag}>{_inline_format(c)}</{tag}>" for c in cells)
        rows.append(f"<tr>{cells_html}</tr>")
    return f'<table><tbody>{"".join(rows)}</tbody></table>'


def markdown_to_storage(text):
    """Convert markdown text to Confluence storage format (XHTML)."""
    if not text:
        return ""

    text = _join_multiline_links(text)
    lines = text.split("\n")
    output = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # Fenced code block
        if line.strip().startswith("```"):
            lang = line.strip()[3:].strip()
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            output.append(_make_code_macro("\n".join(code_lines), lang))
            continue

        # Table
        if line.strip().startswith("|") and "|" in line.strip()[1:]:
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i])
                i += 1
            output.append(_make_table(table_lines))
            continue

        # Headings
        if line.startswith("#### "):
            output.append(f"<h4>{_inline_format(line[5:])}</h4>")
        elif line.startswith("### "):
            output.append(f"<h3>{_inline_format(line[4:])}</h3>")
        elif line.startswith("## "):
            output.append(f"<h2>{_inline_format(line[3:])}</h2>")
        elif line.startswith("# "):
            output.append(f"<h1>{_inline_format(line[2:])}</h1>")

        # Blockquote
        elif line.strip().startswith("> "):
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith("> "):
                quote_lines.append(lines[i].strip()[2:])
                i += 1
            quote_text = "<br />".join(_inline_format(q) for q in quote_lines)
            output.append(f"<blockquote><p>{quote_text}</p></blockquote>")
            continue

        # Unordered list (supports nesting and blank lines between items)
        elif re.match(r'^(\s*)- ', line):
            items = []
            while i < len(lines):
                m = re.match(r'^(\s*)- (.+)$', lines[i])
                if m:
                    indent = len(m.group(1))
                    item_text = m.group(2)
                    if item_text.startswith("[ ] "):
                        item_text = "\u2610 " + item_text[4:]
                    elif item_text.startswith("[x] ") or item_text.startswith("[X] "):
                        item_text = "\u2611 " + item_text[4:]
                    items.append((indent, item_text))
                    i += 1
                    continue
                if not lines[i].strip():
                    j = i + 1
                    while j < len(lines) and not lines[j].strip():
                        j += 1
                    if j < len(lines) and re.match(r'^\s*- ', lines[j]):
                        i = j
                        continue
                break
            output.append(_build_nested_ul(items))
            continue

        # Ordered list
        elif re.match(r'^\d+\.\s', line.strip()):
            list_items = []
            while i < len(lines) and re.match(r'^\d+\.\s', lines[i].strip()):
                item = re.sub(r'^\d+\.\s', '', lines[i].strip())
                list_items.append(item)
                i += 1
            items_html = "".join(f"<li>{_inline_format(it)}</li>" for it in list_items)
            output.append(f"<ol>{items_html}</ol>")
            continue

        # Horizontal rule
        elif line.strip() in ("---", "***", "___"):
            output.append("<hr />")

        # Empty line
        elif not line.strip():
            pass

        # Plain paragraph
        else:
            output.append(f"<p>{_inline_format(line)}</p>")

        i += 1

    return "\n".join(output)


# ─── Section Replacement ─────────────────────────────────────────────────────

def replace_section_in_storage(existing_body, section_heading, new_section_storage):
    """Replace a section (from its heading to the next heading of same or higher level)
    in existing Confluence storage-format content.

    Finds the heading by text content match. Replaces everything from that heading
    up to (but not including) the next heading of equal or higher level.
    """
    # Determine heading level by searching for the heading text
    heading_pattern = re.compile(
        r'(<h(\d)[^>]*>.*?' + re.escape(section_heading) + r'.*?</h\2>)',
        re.DOTALL | re.IGNORECASE
    )
    match = heading_pattern.search(existing_body)
    if not match:
        print(f"  Warning: Section heading '{section_heading}' not found in page.",
              file=sys.stderr)
        return None

    heading_tag = match.group(1)
    heading_level = int(match.group(2))
    section_start = match.start()

    # Find the next heading of same or higher (lower number) level
    rest = existing_body[match.end():]
    next_heading_pattern = re.compile(
        r'<h([1-' + str(heading_level) + r'])[^>]*>',
        re.IGNORECASE
    )
    next_match = next_heading_pattern.search(rest)

    if next_match:
        section_end = match.end() + next_match.start()
    else:
        section_end = len(existing_body)

    # Build the replacement: heading + new content
    replaced = (
        existing_body[:section_start]
        + new_section_storage
        + existing_body[section_end:]
    )
    return replaced


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Edit a Confluence page's content",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Target page
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--url", help="Confluence page URL")
    target.add_argument("--page-id", help="Page ID (numeric)")
    target.add_argument("--title", help="Page title (requires --space)")

    parser.add_argument("--space", help="Space key (required with --title)")

    # Content source
    content = parser.add_mutually_exclusive_group(required=True)
    content.add_argument("--file", "-f", help="Markdown or storage-format file to upload")
    content.add_argument("--stdin", action="store_true", help="Read content from stdin")

    # Options
    parser.add_argument(
        "--input-format", choices=["markdown", "storage"], default="markdown",
        help="Format of the input file (default: markdown)"
    )
    parser.add_argument("--message", "-m", help="Version message for the update")
    parser.add_argument("--rename", help="Rename the page title")
    parser.add_argument("--dry-run", action="store_true", help="Preview without saving")
    parser.add_argument(
        "--append", action="store_true",
        help="Append content to existing page instead of replacing"
    )
    parser.add_argument(
        "--replace-section",
        help="Replace only a specific section (matched by heading text)"
    )
    parser.add_argument("--labels", help="Comma-separated labels to add after update")

    args = parser.parse_args()

    if args.title and not args.space:
        parser.error("--title requires --space")

    # Load credentials
    load_dotenv()

    base_url = os.environ.get("CONFLUENCE_BASE_URL", "")
    if not base_url:
        jira_base = os.environ.get("JIRA_BASE_URL", "")
        if jira_base:
            base_url = jira_base.rstrip("/") + "/wiki"
    if args.url and not base_url:
        base_url = extract_base_url_from_url(args.url)

    email = os.environ.get("JIRA_EMAIL", "")
    api_token = os.environ.get("JIRA_API_TOKEN", "")

    if not all([base_url, email, api_token]):
        print("Error: Missing credentials. Set JIRA_EMAIL, JIRA_API_TOKEN, and "
              "CONFLUENCE_BASE_URL in .env or environment.", file=sys.stderr)
        sys.exit(1)

    editor = ConfluenceEditor(base_url, email, api_token)

    # ── Read input content ──
    if args.stdin:
        raw_content = sys.stdin.read()
    else:
        if not os.path.isfile(args.file):
            print(f"Error: File not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        with open(args.file, "r") as f:
            raw_content = f.read()

    if args.input_format == "markdown":
        new_body_storage = markdown_to_storage(raw_content)
    else:
        new_body_storage = raw_content

    # ── Fetch existing page ──
    try:
        if args.url:
            page_id = extract_page_id_from_url(args.url)
            if not page_id:
                print(f"Error: Could not extract page ID from URL: {args.url}", file=sys.stderr)
                sys.exit(1)
            page = editor.get_page(page_id)
        elif args.page_id:
            page = editor.get_page(args.page_id)
        elif args.title:
            page = editor.find_page_by_title(args.space, args.title)
            if page is None:
                print(f"Error: Page not found: '{args.title}' in space '{args.space}'",
                      file=sys.stderr)
                sys.exit(1)
    except requests.HTTPError as e:
        print(f"Error: API request failed: {e}", file=sys.stderr)
        if hasattr(e, 'response') and e.response is not None:
            print(f"  Status: {e.response.status_code}", file=sys.stderr)
            print(f"  Body: {e.response.text[:500]}", file=sys.stderr)
        sys.exit(1)

    # ── Build final body ──
    title = args.rename if args.rename else page["title"]

    if args.replace_section:
        final_body = replace_section_in_storage(
            page["body_storage"], args.replace_section, new_body_storage
        )
        if final_body is None:
            print(f"Error: Section '{args.replace_section}' not found in page. "
                  f"No changes made.", file=sys.stderr)
            sys.exit(1)
        mode = f"replace-section '{args.replace_section}'"
    elif args.append:
        final_body = page["body_storage"] + "\n" + new_body_storage
        mode = "append"
    else:
        final_body = new_body_storage
        mode = "replace"

    # ── Preview / Execute ──
    print(f"  Page:    {page['title']} (id: {page['id']})", file=sys.stderr)
    print(f"  Version: {page['version']} -> {page['version'] + 1}", file=sys.stderr)
    print(f"  Mode:    {mode}", file=sys.stderr)
    if args.rename:
        print(f"  Rename:  '{page['title']}' -> '{title}'", file=sys.stderr)
    print(f"  Old body: {len(page['body_storage']):,} chars", file=sys.stderr)
    print(f"  New body: {len(final_body):,} chars", file=sys.stderr)

    if args.dry_run:
        print(f"\n  [DRY RUN] No changes made.", file=sys.stderr)
        # Print first 500 chars of new body as preview
        print(f"\n  Preview (first 500 chars):", file=sys.stderr)
        print(f"  {final_body[:500]}", file=sys.stderr)
        sys.exit(0)

    # ── Update ──
    try:
        message = args.message or "Updated by confluence_edit.py"
        new_version = editor.update_page(
            page["id"], title, final_body, page["version"], message
        )
        print(f"  Updated: v{new_version}", file=sys.stderr)

        # Set labels if requested
        if args.labels:
            labels = [lbl.strip() for lbl in args.labels.split(",") if lbl.strip()]
            editor.set_labels(page["id"], labels)
            print(f"  Labels:  {', '.join(labels)}", file=sys.stderr)

        print(f"  Done.", file=sys.stderr)

    except requests.HTTPError as e:
        print(f"Error: Update failed: {e}", file=sys.stderr)
        if hasattr(e, 'response') and e.response is not None:
            print(f"  Status: {e.response.status_code}", file=sys.stderr)
            print(f"  Body: {e.response.text[:500]}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
