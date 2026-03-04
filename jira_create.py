#!/usr/bin/env python3
"""
Generic JIRA Issue Creator
===========================
Creates epics, tasks/stories/bugs, and dependency links in JIRA Cloud
from a JSON data file.

Usage:
  1. Create a .env file (or copy .env.example) with your credentials:
       JIRA_BASE_URL=https://yourorg.atlassian.net
       JIRA_EMAIL=you@example.com
       JIRA_API_TOKEN=your-api-token

     Or set them as environment variables (env vars take precedence over .env).

  2. Run the script:
       python3 jira_create.py data.json
       python3 jira_create.py data.json --dry-run
       python3 jira_create.py data.json --validate-only
       python3 jira_create.py data.json --output-mapping mapping.json

  3. Resume after partial failure:
       python3 jira_create.py data.json --input-mapping mapping.json --output-mapping mapping.json

Generate an API token at: https://id.atlassian.com/manage-profile/security/api-tokens
See jira_data/_example.json for the data file schema.
"""

import argparse
import json
import os
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
            # Don't override existing env vars
            if key not in os.environ:
                os.environ[key] = value


# ─── Configuration ────────────────────────────────────────────────────────────

@dataclass
class JiraConfig:
    base_url: str
    email: str
    api_token: str
    project_key: str = ""
    board_id: str = ""
    dry_run: bool = False

    @property
    def auth(self):
        return HTTPBasicAuth(self.email, self.api_token)

    @property
    def headers(self):
        return {"Accept": "application/json", "Content-Type": "application/json"}


# ─── JIRA API Client ─────────────────────────────────────────────────────────

class JiraClient:
    def __init__(self, config: JiraConfig):
        self.config = config
        self._issue_type_cache = None
        self._priority_cache = None
        self._custom_fields = None
        self._dry_run_counter = 0

    def _get(self, path):
        url = f"{self.config.base_url}/rest/api/3/{path}"
        resp = requests.get(url, auth=self.config.auth, headers=self.config.headers)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path, payload):
        url = f"{self.config.base_url}/rest/api/3/{path}"
        resp = requests.post(
            url, auth=self.config.auth, headers=self.config.headers,
            data=json.dumps(payload)
        )
        if not resp.ok:
            print(f"  ERROR {resp.status_code}: {resp.text[:500]}")
        resp.raise_for_status()
        return resp.json()

    # ── Discovery ──

    def get_issue_types(self):
        """Fetch available issue types for the project."""
        if self._issue_type_cache is None:
            data = self._get(f"project/{self.config.project_key}")
            self._issue_type_cache = {it["name"]: it["id"] for it in data.get("issueTypes", [])}
        return self._issue_type_cache

    def get_priorities(self):
        """Fetch available priority levels."""
        if self._priority_cache is None:
            data = self._get("priority")
            self._priority_cache = {p["name"]: p["id"] for p in data}
        return self._priority_cache

    def get_custom_fields(self):
        """Fetch custom fields to find Story Points field."""
        if self._custom_fields is None:
            data = self._get("field")
            self._custom_fields = {}
            for f in data:
                name_lower = f.get("name", "").lower()
                if "story point" in name_lower or "story_point" in name_lower:
                    self._custom_fields["story_points"] = f["id"]
                if "sprint" in name_lower and f.get("custom", False):
                    self._custom_fields["sprint"] = f["id"]
            if "story_points" not in self._custom_fields:
                self._custom_fields["story_points"] = "customfield_10016"
        return self._custom_fields

    def resolve_issue_type(self, desired_type):
        """Resolve issue type name, falling back gracefully."""
        types = self.get_issue_types()
        if desired_type in types:
            return types[desired_type]
        fallbacks = {
            "Epic": ["Epic"],
            "Story": ["Story", "Task", "User Story"],
            "Task": ["Task", "Story"],
            "Bug": ["Bug", "Task"],
        }
        for fallback in fallbacks.get(desired_type, ["Task"]):
            if fallback in types:
                return types[fallback]
        return list(types.values())[0]

    def resolve_priority(self, desired_priority, priority_mapping=None):
        """Map priority labels to JIRA priorities."""
        priorities = self.get_priorities()

        # Direct match against JIRA priorities
        if desired_priority in priorities:
            return priorities[desired_priority]

        # Try custom mapping from data file
        if priority_mapping:
            mapped = priority_mapping.get(desired_priority)
            if mapped and mapped in priorities:
                return priorities[mapped]

        # Built-in fallback chain for common label formats
        builtin_fallbacks = {
            "P0 — Blocker": ["Highest", "Blocker", "Critical"],
            "P0 — Critical": ["Highest", "Critical", "High"],
            "P0 — High": ["High", "Highest"],
            "P1 — High": ["High", "Medium"],
            "P1 — Medium": ["Medium", "High"],
            "P1 — Low": ["Low", "Medium"],
            "P2 — Medium": ["Medium", "Low"],
            "P2 — Low": ["Low", "Lowest"],
        }
        for candidate in builtin_fallbacks.get(desired_priority, ["Medium"]):
            if candidate in priorities:
                return priorities[candidate]

        return list(priorities.values())[0]

    # ── Creation ──

    def create_issue_single(self, issue_data, skip_fields=None, priority_mapping=None, extra_fields=None):
        """Create a single JIRA issue using single-issue endpoint."""
        if self.config.dry_run:
            self._dry_run_counter += 1
            key = f"DRY-{self._dry_run_counter}"
            print(f"  [DRY RUN] Would create: {issue_data['summary']}")
            return key

        payload = self._build_payload(issue_data, skip_fields, priority_mapping, extra_fields)
        result = self._post("issue", {"fields": payload})
        return result.get("key", "UNKNOWN")

    def _build_payload(self, issue_data, skip_fields=None, priority_mapping=None, extra_fields=None):
        """Build JIRA API payload from issue data."""
        skip = set(skip_fields or [])
        field_map = self.get_custom_fields()
        sp_field = field_map.get("story_points", "customfield_10016")

        payload = {
            "project": {"key": self.config.project_key},
            "summary": issue_data["summary"],
            "issuetype": {"id": self.resolve_issue_type(issue_data.get("type", "Task"))},
            "priority": {"id": self.resolve_priority(
                issue_data.get("priority", "Medium"), priority_mapping
            )},
        }

        # Description in Atlassian Document Format (ADF)
        if "description" in issue_data:
            payload["description"] = self._text_to_adf(issue_data["description"])

        # Story points
        if "story_points" not in skip and "story_points" in issue_data:
            payload[sp_field] = issue_data["story_points"]

        # Labels
        if "labels" in issue_data:
            payload["labels"] = issue_data["labels"]

        # Components
        if "components" not in skip and "component" in issue_data:
            payload["components"] = [{"name": issue_data["component"]}]

        # Epic link — next-gen uses parent; classic uses customfield_10014
        if "epic_key" in issue_data and issue_data["epic_key"]:
            if "parent" not in skip:
                payload["parent"] = {"key": issue_data["epic_key"]}
            elif "epic_link" not in skip:
                payload["customfield_10014"] = issue_data["epic_key"]

        # Epic name (for epic issue type)
        if "epic_name" not in skip and issue_data.get("type") == "Epic" and "epic_name" in issue_data:
            payload["customfield_10011"] = issue_data["epic_name"]

        # Extra custom fields (e.g. {"customfield_10472": {"id": "10593"}})
        if extra_fields:
            payload.update(extra_fields)

        return payload

    def _text_to_adf(self, text):
        """Convert markdown-ish text to Atlassian Document Format."""
        content = []
        lines = text.split("\n")
        current_list_items = []
        in_checklist = False

        for line in lines:
            if line.strip().startswith("- [ ]"):
                if not in_checklist:
                    in_checklist = True
                item_text = line.strip()[5:].strip()
                current_list_items.append(item_text)
                continue
            else:
                if in_checklist and current_list_items:
                    content.append(self._make_bullet_list(current_list_items))
                    current_list_items = []
                    in_checklist = False

            if line.startswith("### "):
                content.append({
                    "type": "heading", "attrs": {"level": 3},
                    "content": self._parse_inline_marks(line[4:])
                })
            elif line.startswith("## "):
                content.append({
                    "type": "heading", "attrs": {"level": 2},
                    "content": self._parse_inline_marks(line[3:])
                })
            elif line.strip():
                content.append({
                    "type": "paragraph",
                    "content": self._parse_inline_marks(line)
                })

        if current_list_items:
            content.append(self._make_bullet_list(current_list_items))

        return {"version": 1, "type": "doc", "content": content or [
            {"type": "paragraph", "content": [{"type": "text", "text": text}]}
        ]}

    def _parse_inline_marks(self, text):
        """Parse inline **bold** markers and return ADF text nodes."""
        import re
        parts = re.split(r'(\*\*[^*]+\*\*)', text)
        nodes = []
        for part in parts:
            if not part:
                continue
            if part.startswith("**") and part.endswith("**"):
                nodes.append({"type": "text", "text": part[2:-2], "marks": [{"type": "strong"}]})
            else:
                nodes.append({"type": "text", "text": part})
        return nodes or [{"type": "text", "text": text}]

    def _make_bullet_list(self, items):
        return {
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": item}]}]
                }
                for item in items
            ]
        }

    def link_issues(self, inward_key, outward_key, link_type="Blocks"):
        """Create a link between two issues."""
        if self.config.dry_run:
            print(f"  [DRY RUN] Would link: {inward_key} --{link_type}--> {outward_key}")
            return

        payload = {
            "type": {"name": link_type},
            "inwardIssue": {"key": inward_key},
            "outwardIssue": {"key": outward_key},
        }
        url = f"{self.config.base_url}/rest/api/3/issueLink"
        resp = requests.post(
            url, auth=self.config.auth, headers=self.config.headers,
            data=json.dumps(payload)
        )
        if not resp.ok:
            print(f"  Warning: Could not create link {inward_key} -> {outward_key}: {resp.status_code}")


# ─── Data File Loading ───────────────────────────────────────────────────────

def load_data_file(path):
    """Load and validate a JIRA data file (JSON)."""
    with open(path, "r") as f:
        raw = json.load(f)

    for key in ("epics", "tasks"):
        if key not in raw:
            raise ValueError(f"Data file missing required key: '{key}'")

    project = raw.get("project", {})
    options = raw.get("options", {})

    return {
        "project_key": project.get("key", ""),
        "default_component": project.get("default_component", ""),
        "epics": raw["epics"],
        "tasks": raw["tasks"],
        "dependencies": raw.get("dependencies", []),
        "existing_issues": raw.get("existing_issues", {}),
        "skip_fields": options.get("skip_fields", []),
        "priority_mapping": options.get("priority_mapping", {}),
        "rate_limit": options.get("rate_limit_seconds", 0.3),
        "link_type": options.get("link_type", "Blocks"),
        "custom_fields": options.get("custom_fields", {}),
    }


def validate_data(data):
    """Validate data file references and print report."""
    errors = []
    warnings = []

    # Collect known IDs
    epic_ids = {e["id"] for e in data["epics"]}
    existing_ids = set(data["existing_issues"].keys())
    all_epic_ids = epic_ids | existing_ids
    task_ids = {t["id"] for t in data["tasks"]}
    all_ids = all_epic_ids | task_ids

    enabled_tasks = [t for t in data["tasks"] if t.get("enabled", True)]
    disabled_tasks = [t for t in data["tasks"] if not t.get("enabled", True)]

    # Check epic references in tasks
    for task in data["tasks"]:
        epic_id = task.get("epic_id")
        if epic_id and epic_id not in all_epic_ids:
            errors.append(f"Task '{task['id']}' references unknown epic_id '{epic_id}'")

    # Check required fields in tasks
    for task in data["tasks"]:
        if "summary" not in task:
            errors.append(f"Task '{task.get('id', '?')}' missing 'summary'")
        if "id" not in task:
            errors.append(f"Task with summary '{task.get('summary', '?')}' missing 'id'")

    # Check required fields in epics
    for epic in data["epics"]:
        if "summary" not in epic:
            errors.append(f"Epic '{epic.get('id', '?')}' missing 'summary'")
        if "id" not in epic:
            errors.append(f"Epic with summary '{epic.get('summary', '?')}' missing 'id'")

    # Check dependency references
    for dep in data["dependencies"]:
        blocker = dep.get("blocker")
        blocked = dep.get("blocked")
        if blocker not in all_ids:
            warnings.append(f"Dependency blocker '{blocker}' not found in tasks/epics")
        if blocked not in all_ids:
            warnings.append(f"Dependency blocked '{blocked}' not found in tasks/epics")

    # Check for duplicate IDs
    seen_ids = set()
    for item in data["epics"] + data["tasks"]:
        item_id = item.get("id")
        if item_id in seen_ids:
            errors.append(f"Duplicate ID: '{item_id}'")
        seen_ids.add(item_id)

    # Print report
    print("=" * 60)
    print("VALIDATION REPORT")
    print("=" * 60)
    print(f"\n  Project key:     {data['project_key'] or '(not set — use --project)'}")
    print(f"  Epics:           {len(data['epics'])}")
    print(f"  Tasks (enabled): {len(enabled_tasks)}")
    print(f"  Tasks (disabled):{len(disabled_tasks)}")
    print(f"  Dependencies:    {len(data['dependencies'])}")
    print(f"  Existing issues: {len(data['existing_issues'])}")
    print(f"  Skip fields:     {data['skip_fields'] or '(none)'}")
    print(f"  Priority mapping: {len(data['priority_mapping'])} entries")

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


# ─── Main Execution ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Create JIRA issues from a JSON data file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s data.json --dry-run           Preview without API calls\n"
            "  %(prog)s data.json --validate-only      Validate data file only\n"
            "  %(prog)s data.json --project PROJ       Override project key\n"
            "  %(prog)s data.json -o mapping.json      Save results for resuming\n"
            "  %(prog)s data.json -i mapping.json      Resume partial run\n"
        ),
    )
    parser.add_argument("data_file", help="Path to JSON data file")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print actions without making API calls")
    parser.add_argument("--validate-only", action="store_true",
                        help="Validate data file and exit (no API calls)")
    parser.add_argument("--project", default="",
                        help="Override project key from data file")
    parser.add_argument("--board-id", default="",
                        help="JIRA board ID for sprint assignment")
    parser.add_argument("--skip-links", action="store_true",
                        help="Skip dependency link creation")
    parser.add_argument("--include-disabled", action="store_true",
                        help="Also create tasks marked as enabled=false")
    parser.add_argument("-i", "--input-mapping", default="",
                        help="Load existing id->key mapping (skip already-created issues)")
    parser.add_argument("-o", "--output-mapping", default="",
                        help="Save id->key mapping after creation (for resuming)")
    args = parser.parse_args()

    # ── Load data file ──
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

    # ── Resolve project key ──
    project_key = args.project or data["project_key"]
    if not project_key:
        print("Error: No project key. Set in data file or use --project KEY")
        sys.exit(1)

    # ── Load credentials from .env then environment ──
    load_dotenv()
    base_url = os.environ.get("JIRA_BASE_URL", "")
    email = os.environ.get("JIRA_EMAIL", "")
    api_token = os.environ.get("JIRA_API_TOKEN", "")

    if not args.dry_run and (not base_url or not email or not api_token):
        print("Error: JIRA credentials not found.")
        print("       Create a .env file or set environment variables.")
        print("       Or use --dry-run to preview without API calls.")
        print()
        print("  .env file (place next to jira_create.py):")
        print("    JIRA_BASE_URL=https://yourorg.atlassian.net")
        print("    JIRA_EMAIL=you@example.com")
        print("    JIRA_API_TOKEN=your-api-token")
        sys.exit(1)

    config = JiraConfig(
        base_url=base_url.rstrip("/"),
        email=email,
        api_token=api_token,
        project_key=project_key,
        board_id=args.board_id,
        dry_run=args.dry_run,
    )

    client = JiraClient(config)

    # ── Load input mapping (for resuming) ──
    id_to_key = dict(data["existing_issues"])
    if args.input_mapping:
        try:
            with open(args.input_mapping, "r") as f:
                saved_mapping = json.load(f)
            id_to_key.update(saved_mapping)
            print(f"Loaded {len(saved_mapping)} existing mappings from {args.input_mapping}")
        except FileNotFoundError:
            print(f"Warning: Input mapping file not found: {args.input_mapping}")

    skip_fields = data["skip_fields"]
    priority_mapping = data["priority_mapping"]
    rate_limit = data["rate_limit"]
    link_type = data["link_type"]
    custom_fields = data.get("custom_fields", {})

    # ── Filter tasks ──
    if args.include_disabled:
        active_tasks = data["tasks"]
    else:
        active_tasks = [t for t in data["tasks"] if t.get("enabled", True)]

    # ── Step 1: Create Epics ──
    epics_to_create = [e for e in data["epics"] if e["id"] not in id_to_key]
    epics_skipped = [e for e in data["epics"] if e["id"] in id_to_key]

    print("=" * 60)
    if epics_skipped:
        for e in epics_skipped:
            print(f"  Epic already exists: {e['id']} -> {id_to_key[e['id']]}")
    if epics_to_create:
        print(f"  Creating {len(epics_to_create)} Epic(s) in project {project_key}")
    elif not epics_skipped:
        print("  No epics to create")
    print("=" * 60)

    for epic in epics_to_create:
        print(f"\n  Creating Epic: {epic['summary']}...")
        try:
            key = client.create_issue_single(epic, skip_fields, priority_mapping, custom_fields)
            id_to_key[epic["id"]] = key
            print(f"  -> Created: {key}")
        except Exception as e:
            print(f"  -> FAILED: {e}")
            id_to_key[epic["id"]] = None
        time.sleep(rate_limit)

    # ── Step 2: Create Tasks/Stories ──
    tasks_to_create = [t for t in active_tasks if t["id"] not in id_to_key]
    tasks_skipped = [t for t in active_tasks if t["id"] in id_to_key]

    print("\n" + "=" * 60)
    if tasks_skipped:
        print(f"  Skipping {len(tasks_skipped)} already-created task(s)")
    print(f"  Creating {len(tasks_to_create)} Task(s)/Story(s) in project {project_key}")
    print("=" * 60)

    for task in tasks_to_create:
        epic_key = id_to_key.get(task.get("epic_id"))
        if epic_key:
            task["epic_key"] = epic_key

        print(f"\n  Creating {task.get('type', 'Task')}: {task['summary']}...")
        try:
            key = client.create_issue_single(task, skip_fields, priority_mapping, custom_fields)
            id_to_key[task["id"]] = key
            print(f"  -> Created: {key} (Epic: {epic_key or 'none'})")
        except Exception as e:
            print(f"  -> FAILED: {e}")
            id_to_key[task["id"]] = None
        time.sleep(rate_limit)

    # ── Step 3: Create Dependencies ──
    if not args.skip_links and data["dependencies"]:
        print("\n" + "=" * 60)
        print(f"  Creating Issue Links ({link_type})")
        print("=" * 60)

        links_created = 0
        links_skipped = 0
        for dep in data["dependencies"]:
            blocker_id = dep.get("blocker")
            blocked_id = dep.get("blocked")
            blocker_key = id_to_key.get(blocker_id)
            blocked_key = id_to_key.get(blocked_id)

            if blocker_key and blocked_key:
                comment = dep.get("_comment", "")
                print(f"\n  Linking: {blocker_key} blocks {blocked_key}" +
                      (f" ({comment})" if comment else "") + "...")
                try:
                    client.link_issues(blocker_key, blocked_key, link_type)
                    print(f"  -> Linked")
                    links_created += 1
                except Exception as e:
                    print(f"  -> FAILED: {e}")
            else:
                print(f"\n  Skipping link {blocker_id} -> {blocked_id} (missing keys)")
                links_skipped += 1
            time.sleep(rate_limit * 0.67)

    # ── Step 4: Save output mapping ──
    if args.output_mapping:
        mapping_to_save = {k: v for k, v in id_to_key.items() if v is not None}
        with open(args.output_mapping, "w") as f:
            json.dump(mapping_to_save, f, indent=2)
        print(f"\n  Mapping saved to {args.output_mapping}")

    # ── Summary ──
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    created_count = sum(1 for k, v in id_to_key.items()
                        if v is not None and k not in data["existing_issues"])
    failed_count = sum(1 for v in id_to_key.values() if v is None)
    skipped_count = len(epics_skipped) + len(tasks_skipped)
    disabled_count = len(data["tasks"]) - len(active_tasks)

    print(f"\n  Created:  {created_count} issues")
    print(f"  Skipped:  {skipped_count} (already exist)")
    print(f"  Disabled: {disabled_count} (enabled=false)")
    print(f"  Failed:   {failed_count} issues")
    if not args.skip_links and data["dependencies"]:
        print(f"  Links:    {links_created} created, {links_skipped} skipped")

    if id_to_key:
        print("\n  Issue Keys:")
        for epic in data["epics"]:
            key = id_to_key.get(epic["id"], "FAILED")
            pre = id_to_key.get(epic["id"]) in data["existing_issues"].values()
            marker = " (pre-existing)" if pre else ""
            print(f"    {epic['id']}: {key} — {epic['summary']}{marker}")

        print()
        for task in active_tasks:
            key = id_to_key.get(task["id"], "FAILED")
            epic_key = id_to_key.get(task.get("epic_id"), "?")
            print(f"    {task['id']}: {key} — {task['summary']} (Epic: {epic_key})")

        if disabled_count > 0:
            print(f"\n  ({disabled_count} disabled tasks not shown)")

    if args.dry_run:
        print("\n  [DRY RUN] No actual API calls were made.")
        print("  Remove --dry-run to create tickets for real.")

    if args.output_mapping:
        print(f"\n  To resume a partial run:")
        print(f"    python3 {sys.argv[0]} {args.data_file} -i {args.output_mapping} -o {args.output_mapping}")


if __name__ == "__main__":
    main()
