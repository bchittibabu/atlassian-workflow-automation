# JIRA Issue Creator

A generic CLI tool to bulk-create JIRA Cloud issues (epics, stories, tasks, bugs) and dependency links from a JSON data file.

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
```

Generate an API token at: https://id.atlassian.com/manage-profile/security/api-tokens

## Quick Start

```bash
# 1. Validate your data file
python3 jira_create.py jira_data/my-project.json --validate-only

# 2. Preview what will be created (no API calls)
python3 jira_create.py jira_data/my-project.json --dry-run

# 3. Create issues for real
python3 jira_create.py jira_data/my-project.json -o mapping.json
```

## Usage

```
python3 jira_create.py <data_file> [options]

Positional:
  data_file                     Path to JSON data file

Options:
  --dry-run                     Preview without making API calls
  --validate-only               Validate data file and exit
  --project KEY                 Override project key from data file
  --include-disabled            Also create tasks marked enabled=false
  --skip-links                  Skip dependency link creation
  -i, --input-mapping FILE      Resume: load existing id->key mapping
  -o, --output-mapping FILE     Save id->key mapping after creation
```

## Data File Format

Create a JSON file following the schema in [`jira_data/_example.json`](jira_data/_example.json).

### Top-level structure

```json
{
  "_schema_version": "1.0",
  "_description": "Human-readable description",

  "project": {
    "key": "PROJ",
    "default_component": ""
  },

  "options": { ... },
  "existing_issues": { ... },
  "epics": [ ... ],
  "tasks": [ ... ],
  "dependencies": [ ... ]
}
```

### Project

| Field | Required | Description |
|---|---|---|
| `key` | Yes | JIRA project key (e.g. `"MA"`, `"PROJ"`) |
| `default_component` | No | Default component name |

### Options

| Field | Default | Description |
|---|---|---|
| `skip_fields` | `[]` | Fields to exclude from API payload: `"story_points"`, `"components"`, `"epic_name"` |
| `priority_mapping` | `{}` | Map custom labels to JIRA priority names, e.g. `{"P0 - Blocker": "Highest"}` |
| `rate_limit_seconds` | `0.3` | Delay between API calls |
| `link_type` | `"Blocks"` | JIRA link type for dependencies |

### Existing Issues

Pre-map internal IDs to already-created JIRA keys. These are skipped during creation and used for epic linking.

```json
"existing_issues": {
  "EPIC-01": "PROJ-100"
}
```

### Epics

```json
{
  "id": "EPIC-01",
  "summary": "Epic title",
  "epic_name": "Short name",
  "priority": "High",
  "labels": ["label1"],
  "description": "Markdown-ish description"
}
```

### Tasks

```json
{
  "id": "1001",
  "epic_id": "EPIC-01",
  "type": "Task",
  "summary": "Task title",
  "priority": "Medium",
  "story_points": 3,
  "labels": ["label1"],
  "component": "Backend",
  "enabled": true,
  "description": "Description with:\n- [ ] Checklist items\n**Bold text**\n## Headings"
}
```

| Field | Required | Description |
|---|---|---|
| `id` | Yes | Internal ID for linking |
| `epic_id` | No | Parent epic (references an epic `id` or `existing_issues` key) |
| `type` | No | `"Task"`, `"Story"`, `"Bug"`, `"Epic"` (default: `"Task"`) |
| `summary` | Yes | Issue title |
| `priority` | No | JIRA priority name or custom label (default: `"Medium"`) |
| `story_points` | No | Story point estimate |
| `labels` | No | Array of label strings |
| `component` | No | JIRA component name |
| `enabled` | No | Set `false` to skip (default: `true`) |
| `description` | No | Supports `## headings`, `**bold**`, `- [ ] checklists` |

### Dependencies

```json
{"blocker": "1001", "blocked": "1002", "_comment": "Setup blocks feature work"}
```

## Resuming Partial Runs

If the script fails mid-way (network issue, rate limit), use mappings to resume:

```bash
# First run — saves progress
python3 jira_create.py data.json -o mapping.json

# Resume — skips already-created issues
python3 jira_create.py data.json -i mapping.json -o mapping.json
```

## Deferred Tasks

Mark tasks as `"enabled": false` to defer them. They are skipped by default but preserved in the data file.

```bash
# Create only enabled tasks
python3 jira_create.py data.json

# Create everything including deferred tasks
python3 jira_create.py data.json --include-disabled
```

## Files

```
jira-create-task/
  jira_create.py              # The tool (single file, stdlib + requests)
  .env                        # Your JIRA credentials (git-ignored)
  .env.example                # Template for credentials
  .gitignore                  # Ignores .env, mapping.json, __pycache__
  jira_data/
    _example.json             # Documented schema template
    meapp-ios-remediation.json  # Example: meApp iOS remediation tasks
```
