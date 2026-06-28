#!/usr/bin/env python3
"""
Weekly Stakeholder Update Generator

Reads recent Asana task comments for active roles, generates a narrative
update in Daryna's style using Claude, and posts to Asana + Slack.

Usage:
  python weekly_update.py              # dry run — preview updates only
  python weekly_update.py --post       # publish to Asana + Slack
  python weekly_update.py --days 14    # look back 14 days instead of 7
  python weekly_update.py --task TASK_ID  # run for a single task
"""

import os
import sys
import argparse
from datetime import datetime, timedelta, timezone

import requests
from anthropic import Anthropic
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ASANA_TOKEN = os.environ.get("ASANA_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")

# Asana project: Licenses Hiring Tracker
PROJECT_ID = "1211182399305214"
ASANA_BASE = "https://app.asana.com/api/1.0"

# Maps keywords in task names (lowercase) → Slack channel name
# Add new countries here as searches are opened
CHANNEL_MAP = {
    "turkey":       "turkey-hiring",
    "chile":        "chile-hiring",
    "brazil":       "brazil-hiring",
    "bahrain":      "bahrain-hiring",
    "azerbaijan":   "azerbaijan-hiring",
    "japan":        "jp-hiring",
    "canada":       "canada-hiring",
    "new zealand":  "new-zealand-hiring",
}

# ---------------------------------------------------------------------------
# Asana helpers
# ---------------------------------------------------------------------------

def _asana_headers() -> dict:
    return {"Authorization": f"Bearer {ASANA_TOKEN}"}


def asana_get(path: str, params: dict = None) -> dict:
    r = requests.get(f"{ASANA_BASE}{path}", headers=_asana_headers(), params=params or {})
    r.raise_for_status()
    return r.json()


def asana_post_comment(task_id: str, text: str) -> dict:
    r = requests.post(
        f"{ASANA_BASE}/tasks/{task_id}/stories",
        headers={**_asana_headers(), "Content-Type": "application/json"},
        json={"data": {"text": text}},
    )
    r.raise_for_status()
    return r.json()


def get_active_tasks() -> list:
    data = asana_get(f"/projects/{PROJECT_ID}/tasks", {
        "opt_fields": "gid,name,completed,assignee.name,permalink_url",
        "limit": 100,
    })
    return [t for t in data["data"] if not t["completed"]]


def get_recent_comments(task_id: str, days: int) -> list:
    data = asana_get(f"/tasks/{task_id}/stories", {
        "opt_fields": "gid,created_at,created_by.name,text,type",
        "limit": 100,
    })
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return [
        s for s in data["data"]
        if s.get("type") == "comment"
        and datetime.fromisoformat(s["created_at"].replace("Z", "+00:00")) >= cutoff
    ]


def get_task(task_id: str) -> dict:
    return asana_get(f"/tasks/{task_id}", {
        "opt_fields": "gid,name,completed,assignee.name,permalink_url"
    })["data"]

# ---------------------------------------------------------------------------
# Slack helper
# ---------------------------------------------------------------------------

def post_to_slack(client: WebClient, channel: str, task_name: str, update: str, task_url: str):
    # Strip leading numbering like "5.1. " from the task name for display
    label = task_name.split(".", 2)[-1].strip() if "." in task_name else task_name
    text = f"*Weekly Update | {label}*\n\n{update}\n\n<{task_url}|View in Asana>"
    client.chat_postMessage(channel=f"#{channel}", text=text)

# ---------------------------------------------------------------------------
# Update generation
# ---------------------------------------------------------------------------

def extract_country_key(task_name: str) -> str | None:
    lower = task_name.lower()
    for keyword in CHANNEL_MAP:
        if keyword in lower:
            return keyword
    return None


def generate_update(task_name: str, comments: list, client: Anthropic) -> str:
    comments_text = "\n\n".join(
        f"[{c['created_by']['name']}, {c['created_at'][:10]}]: {c['text']}"
        for c in comments
    )

    prompt = f"""You are Daryna Sydorenko, Lead Talent Partner at Capital.com.

Role being recruited: {task_name}

Recent comments from this role's Asana task (last {len(comments)} entries):
{comments_text}

Write a concise weekly stakeholder update. Match this exact style:
- Narrative prose; use short bullet points only when listing multiple candidates
- Cover: current status, candidate names and companies if mentioned, any blockers, next steps
- Tone: professional, direct, first-person ("I", "we")
- Length: 3–6 sentences or equivalent bullet points
- Do not invent any information not present in the comments above
- If there is no meaningful new activity, write only: "No significant updates this week."

Output only the update text — no subject line, no preamble."""

    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def validate_env():
    missing = [v for v in ("ASANA_TOKEN", "ANTHROPIC_API_KEY", "SLACK_BOT_TOKEN") if not os.environ.get(v)]
    if missing:
        print(f"Error: missing environment variables: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in your tokens.")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Generate weekly recruiting updates")
    parser.add_argument("--days", type=int, default=7, help="Days of history to include (default: 7)")
    parser.add_argument("--post", action="store_true", help="Publish updates to Asana + Slack (default: dry run)")
    parser.add_argument("--task", type=str, help="Run for a single Asana task ID only")
    args = parser.parse_args()

    validate_env()

    anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
    slack_client = WebClient(token=SLACK_BOT_TOKEN) if args.post else None

    mode = "LIVE" if args.post else "DRY RUN"
    print(f"[{mode}] Weekly Update Generator — looking back {args.days} days\n")
    print("=" * 60)

    tasks = [get_task(args.task)] if args.task else get_active_tasks()
    processed = 0

    for task in tasks:
        country_key = extract_country_key(task["name"])
        if not country_key:
            continue  # no channel mapping — skip silently

        channel = CHANNEL_MAP[country_key]
        task_url = (
            task.get("permalink_url")
            or f"https://app.asana.com/1/89695247570816/project/{PROJECT_ID}/task/{task['gid']}"
        )

        print(f"\nRole:    {task['name']}")
        print(f"Channel: #{channel}")

        comments = get_recent_comments(task["gid"], days=args.days)

        if not comments:
            print(f"Status:  No comments in the last {args.days} days — skipping.")
            continue

        print(f"Found:   {len(comments)} recent comment(s)")
        print("Generating update with Claude...")
        update = generate_update(task["name"], comments, anthropic_client)

        print(f"\n--- Preview ---\n{update}\n---------------")

        if args.post:
            asana_post_comment(task["gid"], update)
            print("Posted to Asana.")

            try:
                post_to_slack(slack_client, channel, task["name"], update, task_url)
                print(f"Posted to Slack #{channel}.")
            except SlackApiError as e:
                print(f"Slack error for #{channel}: {e.response['error']}")

        processed += 1

    print("\n" + "=" * 60)
    print(f"Done. {processed} role(s) processed.")
    if not args.post:
        print("\nReview the previews above, then run with --post to publish.")
