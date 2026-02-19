#!/usr/bin/env python3
"""
Generic Confluence Page Publisher
==================================
Creates and updates Confluence Cloud pages from a JSON data file.
Supports page hierarchy, labels, markdown-to-storage conversion,
and resume via mapping files.

Usage:
  1. Create a .env file (or copy .env.example) with your credentials:
       JIRA_EMAIL=you@example.com
       JIRA_API_TOKEN=your-api-token
       CONFLUENCE_BASE_URL=https://yourorg.atlassian.net/wiki

     Or set them as environment variables (env vars take precedence over .env).
     If CONFLUENCE_BASE_URL is not set, it auto-derives from JIRA_BASE_URL + /wiki.

  2. Run the script:
       python3 confluence_publish.py data.json
       python3 confluence_publish.py data.json --dry-run
       python3 confluence_publish.py data.json --validate-only
       python3 confluence_publish.py data.json -o mapping.json

  3. Resume after partial failure:
       python3 confluence_publish.py data.json -i mapping.json -o mapping.json

See confluence_data/_example.json for the data file schema.
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass

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


# ─── Configuration ────────────────────────────────────────────────────────────

@dataclass
class ConfluenceConfig:
    base_url: str       # e.g. "https://yourorg.atlassian.net/wiki"
    email: str
    api_token: str
    space_key: str = ""
    space_id: str = ""
    dry_run: bool = False

    @property
    def auth(self):
        return HTTPBasicAuth(self.email, self.api_token)

    @property
    def headers(self):
        return {"Accept": "application/json", "Content-Type": "application/json"}


# ─── Confluence API Client ───────────────────────────────────────────────────

class ConfluenceClient:
    def __init__(self, config: ConfluenceConfig):
        self.config = config
        self._space_cache = None
        self._title_cache = {}
        self._dry_run_counter = 0

    # ── HTTP Helpers ──

    def _get_v1(self, path, params=None):
        """GET against /wiki/rest/api/..."""
        url = f"{self.config.base_url}/rest/api/{path}"
        resp = requests.get(url, auth=self.config.auth,
                            headers=self.config.headers, params=params)
        resp.raise_for_status()
        return resp.json()

    def _get_v2(self, path, params=None):
        """GET against /wiki/api/v2/..."""
        url = f"{self.config.base_url}/api/v2/{path}"
        resp = requests.get(url, auth=self.config.auth,
                            headers=self.config.headers, params=params)
        resp.raise_for_status()
        return resp.json()

    def _post_v1(self, path, payload):
        """POST against /wiki/rest/api/..."""
        url = f"{self.config.base_url}/rest/api/{path}"
        resp = requests.post(url, auth=self.config.auth,
                             headers=self.config.headers, json=payload)
        if not resp.ok:
            print(f"  ERROR {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
        return resp.json()

    def _post_v2(self, path, payload):
        """POST against /wiki/api/v2/..."""
        url = f"{self.config.base_url}/api/v2/{path}"
        resp = requests.post(url, auth=self.config.auth,
                             headers=self.config.headers, json=payload)
        if not resp.ok:
            print(f"  ERROR {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
        return resp.json()

    def _put_v2(self, path, payload):
        """PUT against /wiki/api/v2/..."""
        url = f"{self.config.base_url}/api/v2/{path}"
        resp = requests.put(url, auth=self.config.auth,
                            headers=self.config.headers, json=payload)
        if not resp.ok:
            print(f"  ERROR {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
        return resp.json()

    # ── Discovery ──

    def resolve_space(self):
        """Fetch space info and cache space_id."""
        if self._space_cache:
            return self._space_cache

        if self.config.dry_run:
            self._space_cache = {
                "id": "DRY-SPACE",
                "key": self.config.space_key,
                "name": f"[Dry Run] {self.config.space_key}",
            }
            self.config.space_id = "DRY-SPACE"
            return self._space_cache

        data = self._get_v1(f"space/{self.config.space_key}")
        self._space_cache = {
            "id": str(data["id"]),
            "key": data["key"],
            "name": data.get("name", ""),
        }
        self.config.space_id = str(data["id"])
        return self._space_cache

    def find_page_by_title(self, title):
        """Find a page by title in the configured space. Returns dict or None.

        Returns: {"id": "12345", "version": 3} or None
        """
        if title in self._title_cache:
            return self._title_cache[title]

        if self.config.dry_run:
            return None

        data = self._get_v1("content", params={
            "spaceKey": self.config.space_key,
            "title": title,
            "expand": "version",
        })
        results = data.get("results", [])
        if results:
            page = results[0]
            info = {
                "id": str(page["id"]),
                "version": page["version"]["number"],
            }
            self._title_cache[title] = info
            return info
        return None

    # ── Page CRUD ──

    def create_page(self, title, body_storage, parent_id=None):
        """Create a new page. Returns page_id string."""
        if self.config.dry_run:
            self._dry_run_counter += 1
            key = f"DRY-{self._dry_run_counter}"
            print(f"  [DRY RUN] Would create: {title}")
            return key

        payload = {
            "spaceId": self.config.space_id,
            "status": "current",
            "title": title,
            "body": {
                "representation": "storage",
                "value": body_storage,
            },
        }
        if parent_id:
            payload["parentId"] = str(parent_id)

        result = self._post_v2("pages", payload)
        page_id = str(result["id"])
        self._title_cache[title] = {
            "id": page_id,
            "version": result.get("version", {}).get("number", 1),
        }
        return page_id

    def update_page(self, page_id, title, body_storage, current_version,
                    update_message=""):
        """Update an existing page. Returns page_id string."""
        if self.config.dry_run:
            print(f"  [DRY RUN] Would update: {title} (v{current_version} -> v{current_version + 1})")
            return str(page_id)

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
                "message": update_message or "Updated by confluence_publish.py",
            },
        }

        result = self._put_v2(f"pages/{page_id}", payload)
        self._title_cache[title] = {
            "id": str(page_id),
            "version": current_version + 1,
        }
        return str(result["id"])

    def upsert_page(self, title, body_storage, parent_id=None,
                    update_message="", create_only=False):
        """Find page by title; create if missing, update if exists.

        Returns: (page_id, action) where action is "created", "updated", or "skipped".
        """
        existing = self.find_page_by_title(title)

        if existing:
            if create_only:
                print(f"  [SKIP] Already exists: {title} (id: {existing['id']})")
                return existing["id"], "skipped"
            page_id = self.update_page(
                existing["id"], title, body_storage,
                existing["version"], update_message
            )
            return page_id, "updated"
        else:
            page_id = self.create_page(title, body_storage, parent_id)
            return page_id, "created"

    # ── Labels ──

    def set_labels(self, page_id, labels):
        """Add labels to a page (appends, does not remove existing)."""
        if not labels:
            return

        if self.config.dry_run:
            print(f"  [DRY RUN] Would set labels: {', '.join(labels)}")
            return

        label_payload = [{"prefix": "global", "name": l} for l in labels]
        try:
            self._post_v1(f"content/{page_id}/label", label_payload)
        except Exception as e:
            print(f"  Warning: Could not set labels on {page_id}: {e}")


# ─── Markdown to Confluence Storage Format ───────────────────────────────────

def _inline_format(text):
    """Handle inline markdown: **bold**, *italic*, `code`, [text](url)."""
    # Escape bare & < > for XHTML (before adding tags)
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;").replace(">", "&gt;")

    # Bold: **text**
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    # Italic: *text* (not inside **)
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<em>\1</em>', text)
    # Inline code: `text`
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    # Links: [text](url)
    text = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2">\1</a>', text)

    return text


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
        # Skip separator row (|---|---|)
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
                i += 1  # skip closing ```
            output.append(_make_code_macro("\n".join(code_lines), lang))
            continue

        # Table (consecutive | lines)
        if line.strip().startswith("|") and "|" in line.strip()[1:]:
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i])
                i += 1
            output.append(_make_table(table_lines))
            continue

        # Headings
        if line.startswith("### "):
            output.append(f"<h3>{_inline_format(line[4:])}</h3>")
        elif line.startswith("## "):
            output.append(f"<h2>{_inline_format(line[3:])}</h2>")
        elif line.startswith("# "):
            output.append(f"<h1>{_inline_format(line[2:])}</h1>")

        # Blockquote (consecutive > lines)
        elif line.strip().startswith("> "):
            quote_lines = []
            while i < len(lines) and lines[i].strip().startswith("> "):
                quote_lines.append(lines[i].strip()[2:])
                i += 1
            quote_text = "<br />".join(_inline_format(q) for q in quote_lines)
            output.append(f"<blockquote><p>{quote_text}</p></blockquote>")
            continue

        # Unordered list (consecutive - lines, including - [ ] checklists)
        elif line.strip().startswith("- "):
            list_items = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                item = lines[i].strip()[2:]
                # Checklist: - [ ] or - [x]
                if item.startswith("[ ] "):
                    item = "\u2610 " + item[4:]  # ballot box
                elif item.startswith("[x] ") or item.startswith("[X] "):
                    item = "\u2611 " + item[4:]  # checked ballot box
                list_items.append(item)
                i += 1
            items_html = "".join(
                f"<li>{_inline_format(it)}</li>" for it in list_items
            )
            output.append(f"<ul>{items_html}</ul>")
            continue

        # Ordered list (consecutive 1. lines)
        elif re.match(r'^\d+\.\s', line.strip()):
            list_items = []
            while i < len(lines) and re.match(r'^\d+\.\s', lines[i].strip()):
                item = re.sub(r'^\d+\.\s', '', lines[i].strip())
                list_items.append(item)
                i += 1
            items_html = "".join(
                f"<li>{_inline_format(it)}</li>" for it in list_items
            )
            output.append(f"<ol>{items_html}</ol>")
            continue

        # Horizontal rule
        elif line.strip() in ("---", "***", "___"):
            output.append("<hr />")

        # Empty line (skip)
        elif not line.strip():
            pass

        # Plain text paragraph
        else:
            output.append(f"<p>{_inline_format(line)}</p>")

        i += 1

    return "\n".join(output)


# ─── Data File Loading ───────────────────────────────────────────────────────

def load_data_file(path):
    """Load and validate a Confluence data file (JSON)."""
    with open(path, "r") as f:
        raw = json.load(f)

    if "pages" not in raw:
        raise ValueError("Data file missing required key: 'pages'")

    space = raw.get("space", {})
    options = raw.get("options", {})

    return {
        "space_key": space.get("key", ""),
        "space_id": space.get("id", ""),
        "pages": raw["pages"],
        "existing_pages": raw.get("existing_pages", {}),
        "rate_limit": options.get("rate_limit_seconds", 0.5),
        "update_message": options.get("update_message",
                                      "Updated by confluence_publish.py"),
        "data_file_dir": os.path.dirname(os.path.abspath(path)),
    }


def validate_data(data):
    """Validate data file references and print report."""
    errors = []
    warnings = []

    page_ids = {p["id"] for p in data["pages"] if "id" in p}
    existing_ids = set(data["existing_pages"].keys())
    all_ids = page_ids | existing_ids

    enabled_pages = [p for p in data["pages"] if p.get("enabled", True)]
    disabled_pages = [p for p in data["pages"] if not p.get("enabled", True)]

    for page in data["pages"]:
        # Required fields
        if "id" not in page:
            errors.append(f"Page with title '{page.get('title', '?')}' missing 'id'")
        if "title" not in page:
            errors.append(f"Page '{page.get('id', '?')}' missing 'title'")

        # Parent reference check
        parent_id = page.get("parent_id")
        if parent_id and parent_id not in all_ids:
            errors.append(
                f"Page '{page.get('id', '?')}' references unknown parent_id "
                f"'{parent_id}'"
            )

        # Body source check
        body = page.get("body")
        body_file = page.get("body_file")
        if not body and not body_file:
            warnings.append(
                f"Page '{page.get('id', '?')}' has no body or body_file "
                f"(will create empty page)"
            )

        # Body file existence check
        if body_file:
            resolved = os.path.join(data["data_file_dir"], body_file)
            if not os.path.isfile(resolved):
                errors.append(
                    f"Page '{page.get('id', '?')}' body_file not found: "
                    f"{body_file}"
                )

    # Duplicate IDs
    seen_ids = set()
    for page in data["pages"]:
        pid = page.get("id")
        if pid and pid in seen_ids:
            errors.append(f"Duplicate page ID: '{pid}'")
        if pid:
            seen_ids.add(pid)

    # Duplicate titles
    seen_titles = set()
    for page in data["pages"]:
        title = page.get("title")
        if title and title in seen_titles:
            errors.append(
                f"Duplicate title: '{title}' (must be unique within space)"
            )
        if title:
            seen_titles.add(title)

    # Print report
    print("=" * 60)
    print("VALIDATION REPORT")
    print("=" * 60)
    print(f"\n  Space key:        {data['space_key'] or '(not set — use --space)'}")
    print(f"  Space ID:         {data['space_id'] or '(will resolve from key)'}")
    print(f"  Pages (enabled):  {len(enabled_pages)}")
    print(f"  Pages (disabled): {len(disabled_pages)}")
    print(f"  Existing pages:   {len(data['existing_pages'])}")

    if errors:
        print(f"\n  ERRORS ({len(errors)}):")
        for e in errors:
            print(f"    - {e}")

    if warnings:
        print(f"\n  WARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"    - {w}")

    if not errors and not warnings:
        print("\n  All checks passed.")

    print()
    return len(errors) == 0


# ─── Topological Sort ────────────────────────────────────────────────────────

def topological_sort_pages(pages, existing_ids):
    """Sort pages so parents come before children. Detects cycles."""
    id_to_page = {p["id"]: p for p in pages}
    in_degree = {p["id"]: 0 for p in pages}
    children = {p["id"]: [] for p in pages}

    for p in pages:
        parent = p.get("parent_id")
        if parent and parent in id_to_page:
            in_degree[p["id"]] += 1
            children[parent].append(p["id"])

    queue = [pid for pid, deg in in_degree.items() if deg == 0]
    result = []
    while queue:
        pid = queue.pop(0)
        result.append(id_to_page[pid])
        for child_id in children.get(pid, []):
            in_degree[child_id] -= 1
            if in_degree[child_id] == 0:
                queue.append(child_id)

    if len(result) != len(pages):
        cycle_ids = [pid for pid, deg in in_degree.items() if deg > 0]
        raise ValueError(f"Circular parent_id references detected: {cycle_ids}")

    return result


# ─── Main Execution ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Create/update Confluence pages from a JSON data file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s data.json --dry-run               Preview without API calls\n"
            "  %(prog)s data.json --validate-only          Validate data file only\n"
            "  %(prog)s data.json --space ENG              Override space key\n"
            "  %(prog)s data.json -o mapping.json          Save results for resuming\n"
            "  %(prog)s data.json -i mapping.json          Resume partial run\n"
            "\n"
            "  Markdown file input:\n"
            "  %(prog)s page.md --space ENG --title 'My Page'\n"
            "  %(prog)s page.md --space ENG --title 'My Page' --parent-title 'Parent'\n"
            "  %(prog)s page.md --space ENG --title 'My Page' --labels 'doc,api'\n"
        ),
    )
    parser.add_argument("data_file",
                        help="Path to JSON data file or .md markdown file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print actions without making API calls")
    parser.add_argument("--validate-only", action="store_true",
                        help="Validate data file and exit (no API calls)")
    parser.add_argument("--space", default="",
                        help="Override space key from data file")
    parser.add_argument("--title", default="",
                        help="Page title (required when input is a .md file)")
    parser.add_argument("--parent-title", default="",
                        help="Parent page title in Confluence (for .md input)")
    parser.add_argument("--labels", default="",
                        help="Comma-separated labels (for .md input)")
    parser.add_argument("--include-disabled", action="store_true",
                        help="Also process pages marked as enabled=false")
    parser.add_argument("--create-only", action="store_true",
                        help="Skip pages that already exist (no updates)")
    parser.add_argument("-i", "--input-mapping", default="",
                        help="Load existing id->pageId mapping (skip already-processed)")
    parser.add_argument("-o", "--output-mapping", default="",
                        help="Save id->pageId mapping after processing")
    args = parser.parse_args()

    # ── Detect input type (.md vs .json) ──
    is_markdown = args.data_file.lower().endswith((".md", ".markdown"))

    if is_markdown:
        # Build synthetic data from .md file
        if not os.path.isfile(args.data_file):
            print(f"Error: File not found: {args.data_file}")
            sys.exit(1)

        title = args.title
        if not title:
            # Derive title from filename: "my-page.md" -> "my page"
            title = os.path.splitext(os.path.basename(args.data_file))[0]
            title = title.replace("-", " ").replace("_", " ").title()
            print(f"  No --title provided, using: \"{title}\"")

        with open(args.data_file, "r") as f:
            body_text = f.read()

        labels = [l.strip() for l in args.labels.split(",") if l.strip()] \
            if args.labels else []

        page_entry = {
            "id": "MD-01",
            "title": title,
            "parent_id": None,
            "parent_title": args.parent_title or None,
            "labels": labels,
            "enabled": True,
            "body": body_text,
        }

        space_key = args.space
        if not space_key:
            print("Error: --space KEY is required when input is a .md file")
            sys.exit(1)

        data = {
            "space_key": space_key,
            "space_id": "",
            "pages": [page_entry],
            "existing_pages": {},
            "rate_limit": 0.5,
            "update_message": "Updated by confluence_publish.py",
            "data_file_dir": os.path.dirname(os.path.abspath(args.data_file)),
        }

        if args.validate_only:
            validate_data(data)
            sys.exit(0)
    else:
        # ── Load JSON data file ──
        try:
            data = load_data_file(args.data_file)
        except FileNotFoundError:
            print(f"Error: Data file not found: {args.data_file}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in {args.data_file}: {e}")
            sys.exit(1)
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)

        # ── Validate ──
        valid = validate_data(data)
        if args.validate_only:
            sys.exit(0 if valid else 1)
        if not valid:
            print("Fix validation errors before proceeding.")
            sys.exit(1)

    # ── Resolve space key ──
    space_key = args.space or data["space_key"]
    if not space_key:
        print("Error: No space key. Set in data file or use --space KEY")
        sys.exit(1)

    # ── Load credentials from .env then environment ──
    load_dotenv()
    base_url = os.environ.get("CONFLUENCE_BASE_URL", "")
    jira_base = os.environ.get("JIRA_BASE_URL", "")
    email = os.environ.get("JIRA_EMAIL", "")
    api_token = os.environ.get("JIRA_API_TOKEN", "")

    # Auto-derive Confluence URL from JIRA URL
    if not base_url and jira_base:
        base_url = jira_base.rstrip("/") + "/wiki"

    if not args.dry_run and (not base_url or not email or not api_token):
        print("Error: Confluence credentials not found.")
        print("       Create a .env file or set environment variables.")
        print("       Or use --dry-run to preview without API calls.")
        print()
        print("  .env file (place next to confluence_publish.py):")
        print("    JIRA_EMAIL=you@example.com")
        print("    JIRA_API_TOKEN=your-api-token")
        print("    CONFLUENCE_BASE_URL=https://yourorg.atlassian.net/wiki")
        sys.exit(1)

    config = ConfluenceConfig(
        base_url=base_url.rstrip("/"),
        email=email,
        api_token=api_token,
        space_key=space_key,
        space_id=data.get("space_id", ""),
        dry_run=args.dry_run,
    )

    client = ConfluenceClient(config)

    # ── Resolve space ──
    print("=" * 60)
    space_info = client.resolve_space()
    print(f"  Space: {space_info['name']} ({space_info['key']}, id: {space_info['id']})")
    print("=" * 60)

    # ── Load input mapping (for resuming) ──
    id_to_page_id = dict(data["existing_pages"])
    if args.input_mapping:
        try:
            with open(args.input_mapping, "r") as f:
                saved = json.load(f)
            id_to_page_id.update(saved)
            print(f"  Loaded {len(saved)} existing mappings from {args.input_mapping}")
        except FileNotFoundError:
            print(f"  Warning: Input mapping not found: {args.input_mapping}")

    # ── Filter and sort pages ──
    if args.include_disabled:
        active_pages = data["pages"]
    else:
        active_pages = [p for p in data["pages"] if p.get("enabled", True)]

    pages_to_process = [p for p in active_pages if p["id"] not in id_to_page_id]
    pages_skipped_mapping = [p for p in active_pages if p["id"] in id_to_page_id]

    try:
        sorted_pages = topological_sort_pages(pages_to_process,
                                              set(id_to_page_id.keys()))
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    disabled_count = len(data["pages"]) - len(active_pages)

    if pages_skipped_mapping:
        print(f"  Skipping {len(pages_skipped_mapping)} already-mapped page(s)")
    print(f"  Processing {len(sorted_pages)} page(s)" +
          (f", {disabled_count} disabled" if disabled_count else ""))

    # ── Process pages ──
    update_message = data.get("update_message", "")
    rate_limit = data["rate_limit"]
    created_count = 0
    updated_count = 0
    skipped_count = len(pages_skipped_mapping)
    failed_count = 0

    print("\n" + "=" * 60)
    print(f"  Publishing pages to {space_key}")
    print("=" * 60)

    for page in sorted_pages:
        title = page["title"]

        # Resolve body content
        body_text = ""
        body_file = page.get("body_file")
        if body_file:
            file_path = os.path.join(data["data_file_dir"], body_file)
            try:
                with open(file_path, "r") as f:
                    body_text = f.read()
            except FileNotFoundError:
                print(f"\n  {title}...")
                print(f"  -> FAILED: body_file not found: {body_file}")
                id_to_page_id[page["id"]] = None
                failed_count += 1
                continue
        elif page.get("body"):
            body_text = page["body"]

        body_storage = markdown_to_storage(body_text)

        # Resolve parent
        parent_confluence_id = None
        parent_id = page.get("parent_id")
        parent_title = page.get("parent_title")
        if parent_id:
            parent_confluence_id = id_to_page_id.get(parent_id)
            if not parent_confluence_id:
                print(f"\n  Warning: Parent '{parent_id}' for '{title}' not found in mapping")
        elif parent_title:
            existing_parent = client.find_page_by_title(parent_title)
            if existing_parent:
                parent_confluence_id = existing_parent["id"]
            else:
                print(f"\n  Warning: Parent page '{parent_title}' not found in space")

        # Upsert
        print(f"\n  {title}...")
        try:
            page_id, action = client.upsert_page(
                title, body_storage, parent_confluence_id,
                update_message, args.create_only,
            )
            id_to_page_id[page["id"]] = page_id

            if action == "created":
                print(f"  -> Created: {page_id}")
                created_count += 1
            elif action == "updated":
                print(f"  -> Updated: {page_id}")
                updated_count += 1
            elif action == "skipped":
                skipped_count += 1

            # Set labels
            labels = page.get("labels", [])
            if labels and page_id and not page_id.startswith("DRY-"):
                client.set_labels(page_id, labels)
            elif labels and args.dry_run:
                client.set_labels(page_id, labels)

        except Exception as e:
            print(f"  -> FAILED: {e}")
            id_to_page_id[page["id"]] = None
            failed_count += 1

        time.sleep(rate_limit)

    # ── Save output mapping ──
    if args.output_mapping:
        mapping_to_save = {k: v for k, v in id_to_page_id.items() if v is not None}
        with open(args.output_mapping, "w") as f:
            json.dump(mapping_to_save, f, indent=2)
        print(f"\n  Mapping saved to {args.output_mapping}")

    # ── Summary ──
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    print(f"\n  Created:  {created_count} pages")
    print(f"  Updated:  {updated_count} pages")
    print(f"  Skipped:  {skipped_count} (already exist or mapped)")
    print(f"  Disabled: {disabled_count} (enabled=false)")
    print(f"  Failed:   {failed_count} pages")

    if id_to_page_id:
        print("\n  Page Mapping:")
        for page in active_pages:
            page_id = id_to_page_id.get(page["id"], "FAILED")
            pre = page["id"] in data["existing_pages"]
            marker = " (pre-existing)" if pre else ""
            parent_info = ""
            if page.get("parent_id"):
                parent_cid = id_to_page_id.get(page["parent_id"], "?")
                parent_info = f" (parent: {parent_cid})"
            print(f"    {page['id']}: {page_id} — {page['title']}{parent_info}{marker}")

        if disabled_count > 0:
            print(f"\n  ({disabled_count} disabled pages not shown)")

    if args.dry_run:
        print("\n  [DRY RUN] No actual API calls were made.")
        print("  Remove --dry-run to publish pages for real.")

    if args.output_mapping:
        print(f"\n  To resume a partial run:")
        print(f"    python3 {sys.argv[0]} {args.data_file} "
              f"-i {args.output_mapping} -o {args.output_mapping}")


if __name__ == "__main__":
    main()
