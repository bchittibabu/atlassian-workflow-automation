# Atlassian Automation Tools

CLI tools to bulk-create JIRA issues and Confluence pages from JSON data files. Both share the same `.env` credentials.

## Setup

**Prerequisites:** Python 3.8+ and the `requests` library.

```bash
pip3 install requests
```

**Credentials:** Copy the example and fill in your values.

```bash
cp .env.example .env
```

```
JIRA_BASE_URL=https://yourorg.atlassian.net
JIRA_EMAIL=you@example.com
JIRA_API_TOKEN=your-api-token
CONFLUENCE_BASE_URL=https://yourorg.atlassian.net/wiki
```

Generate an API token at: https://id.atlassian.com/manage-profile/security/api-tokens

Both tools use `JIRA_EMAIL` and `JIRA_API_TOKEN` for authentication (same Atlassian account). `CONFLUENCE_BASE_URL` is optional — it auto-derives from `JIRA_BASE_URL` + `/wiki` if not set.

---

## JIRA Issue Creator

Bulk-create epics, stories, tasks, bugs, and dependency links.

### Quick Start

```bash
# Validate data file
python3 jira_create.py jira_data/my-project.json --validate-only

# Preview (no API calls)
python3 jira_create.py jira_data/my-project.json --dry-run

# Create issues
python3 jira_create.py jira_data/my-project.json -o mapping.json
```

### Usage

```
python3 jira_create.py <data_file> [options]

Options:
  --dry-run                     Preview without making API calls
  --validate-only               Validate data file and exit
  --project KEY                 Override project key from data file
  --include-disabled            Also create tasks marked enabled=false
  --skip-links                  Skip dependency link creation
  -i, --input-mapping FILE      Resume: load existing id->key mapping
  -o, --output-mapping FILE     Save id->key mapping after creation
```

### Data File Format

See [`jira_data/_example.json`](jira_data/_example.json) for the full schema.

```json
{
  "_schema_version": "1.0",
  "project": { "key": "PROJ" },
  "options": {
    "skip_fields": [],
    "priority_mapping": {},
    "rate_limit_seconds": 0.3,
    "link_type": "Blocks"
  },
  "existing_issues": {},
  "epics": [
    { "id": "EPIC-01", "summary": "Epic title", "priority": "High", "labels": ["label1"], "description": "..." }
  ],
  "tasks": [
    { "id": "1001", "epic_id": "EPIC-01", "type": "Task", "summary": "Task title", "priority": "Medium", "labels": ["label1"], "enabled": true, "description": "..." }
  ],
  "dependencies": [
    { "blocker": "1001", "blocked": "1002" }
  ]
}
```

| Task Field | Required | Description |
|---|---|---|
| `id` | Yes | Internal ID for linking |
| `epic_id` | No | Parent epic reference |
| `type` | No | `Task`, `Story`, `Bug`, `Epic` (default: `Task`) |
| `summary` | Yes | Issue title |
| `priority` | No | JIRA priority or custom label |
| `story_points` | No | Story point estimate |
| `labels` | No | Array of label strings |
| `component` | No | JIRA component name |
| `enabled` | No | Set `false` to skip (default: `true`) |
| `description` | No | Supports `## headings`, `**bold**`, `- [ ] checklists` |

---

## Confluence Page Publisher

Create and update Confluence pages with page hierarchy, labels, and markdown content.

### Quick Start

```bash
# Validate data file
python3 confluence_publish.py confluence_data/my-pages.json --validate-only

# Preview (no API calls)
python3 confluence_publish.py confluence_data/my-pages.json --dry-run

# Publish pages
python3 confluence_publish.py confluence_data/my-pages.json -o mapping.json
```

### Publish a Markdown File Directly

You can pass a `.md` file directly instead of creating a JSON data file:

```bash
# Publish a single markdown file as a Confluence page
python3 confluence_publish.py my-page.md --space ENG --title "My Page Title"

# With a parent page and labels
python3 confluence_publish.py my-page.md --space ENG --title "API Docs" \
  --parent-title "Engineering Home" --labels "api,docs"

# Preview first
python3 confluence_publish.py my-page.md --space ENG --title "My Page" --dry-run
```

If `--title` is omitted, the title is derived from the filename (e.g., `api-docs.md` → "Api Docs").

### Usage

```
python3 confluence_publish.py <data_file|page.md> [options]

Options:
  --dry-run                     Preview without making API calls
  --validate-only               Validate data file and exit
  --space KEY                   Override space key from data file (required for .md input)
  --title TEXT                  Page title (required for .md input, auto-derived if omitted)
  --parent-title TEXT           Parent page title in Confluence (for .md input)
  --labels LIST                 Comma-separated labels (for .md input)
  --include-disabled            Also process pages marked enabled=false
  --create-only                 Skip pages that already exist (no updates)
  -i, --input-mapping FILE      Resume: load existing id->pageId mapping
  -o, --output-mapping FILE     Save id->pageId mapping after processing
```

### Data File Format

See [`confluence_data/_example.json`](confluence_data/_example.json) for the full schema.

```json
{
  "_schema_version": "1.0",
  "space": { "key": "ENG" },
  "options": {
    "rate_limit_seconds": 0.5,
    "update_message": "Updated by confluence_publish.py"
  },
  "existing_pages": {},
  "pages": [
    {
      "id": "ROOT-01",
      "title": "Engineering Wiki Home",
      "parent_id": null,
      "labels": ["engineering"],
      "body": "# Welcome\n\nPage content in markdown."
    },
    {
      "id": "PAGE-01",
      "title": "Architecture Overview",
      "parent_id": "ROOT-01",
      "labels": ["architecture"],
      "body_file": "confluence_data/content/architecture.md"
    }
  ]
}
```

| Page Field | Required | Description |
|---|---|---|
| `id` | Yes | Internal ID for parent references and mapping |
| `title` | Yes | Page title (must be unique within space) |
| `parent_id` | No | Internal ID of parent page |
| `parent_title` | No | Alternative: find parent by title in Confluence |
| `labels` | No | Array of label strings |
| `enabled` | No | Set `false` to skip (default: `true`) |
| `body` | No | Inline markdown content |
| `body_file` | No | Path to external markdown file (relative to data file, takes precedence over `body`) |

### Upsert Behavior

By default, the tool finds pages by title:
- **Page not found** → creates it
- **Page found** → updates it (increments version)
- Use `--create-only` to skip existing pages

### Markdown Support

Body content (inline or from `body_file`) supports:

| Markdown | Confluence Output |
|---|---|
| `# Heading` | `<h1>`, `<h2>`, `<h3>` |
| `**bold**`, `*italic*` | `<strong>`, `<em>` |
| `` `code` `` | `<code>` |
| ` ```lang ... ``` ` | Confluence code block macro |
| `- item` | Bullet list |
| `1. item` | Numbered list |
| `- [ ] item` | Checklist (ballot box) |
| `> quote` | Blockquote |
| `\| col \| col \|` | Table |
| `[text](url)` | Link |
| `---` | Horizontal rule |

---

## Resuming Partial Runs

Both tools support resume via mapping files:

```bash
# First run — saves progress
python3 jira_create.py data.json -o mapping.json

# Resume — skips already-created items
python3 jira_create.py data.json -i mapping.json -o mapping.json
```

## Deferred Items

Mark tasks/pages as `"enabled": false` to defer them:

```bash
# Process only enabled items
python3 jira_create.py data.json

# Include deferred items
python3 jira_create.py data.json --include-disabled
```

## Files

```
jira-create-task/
  jira_create.py                # JIRA issue creator
  confluence_publish.py         # Confluence page publisher
  .env                          # Your credentials (git-ignored)
  .env.example                  # Template for credentials
  .gitignore                    # Ignores .env, mapping.json, __pycache__
  jira_data/
    _example.json               # JIRA data schema template
    meapp-ios-remediation.json  # Example: meApp iOS tasks
  confluence_data/
    _example.json               # Confluence data schema template
```
