"""
Parse Claude Code session JSONL files and extract usage/token data.
"""
import json
import os
import re
from pathlib import Path
from typing import Generator

import database as db

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Pricing per million tokens (as of 2025) — update if rates change
PRICING = {
    "claude-opus-4-7":      {"input": 15.0,  "output": 75.0,  "cache_read": 1.5,  "cache_write": 18.75},
    "claude-sonnet-4-6":    {"input": 3.0,   "output": 15.0,  "cache_read": 0.3,  "cache_write": 3.75},
    "claude-haiku-4-5":     {"input": 0.8,   "output": 4.0,   "cache_read": 0.08, "cache_write": 1.0},
    "claude-opus-4-5":      {"input": 15.0,  "output": 75.0,  "cache_read": 1.5,  "cache_write": 18.75},
    "claude-sonnet-4-5":    {"input": 3.0,   "output": 15.0,  "cache_read": 0.3,  "cache_write": 3.75},
    "claude-3-5-sonnet":    {"input": 3.0,   "output": 15.0,  "cache_read": 0.3,  "cache_write": 3.75},
    "claude-3-opus":        {"input": 15.0,  "output": 75.0,  "cache_read": 1.5,  "cache_write": 18.75},
    "claude-3-haiku":       {"input": 0.25,  "output": 1.25,  "cache_read": 0.03, "cache_write": 0.3},
    "default":              {"input": 3.0,   "output": 15.0,  "cache_read": 0.3,  "cache_write": 3.75},
}


def get_pricing(model: str) -> dict:
    if not model:
        return PRICING["default"]
    for key in PRICING:
        if key in model:
            return PRICING[key]
    return PRICING["default"]


def estimate_cost(model, input_tokens, output_tokens, cache_read, cache_creation):
    p = get_pricing(model)
    cost = (
        (input_tokens / 1_000_000) * p["input"]
        + (output_tokens / 1_000_000) * p["output"]
        + (cache_read / 1_000_000) * p["cache_read"]
        + (cache_creation / 1_000_000) * p["cache_write"]
    )
    return round(cost, 8)


def extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "tool_use":
                    parts.append(f"[Tool: {item.get('name', '')}]")
                elif item.get("type") == "tool_result":
                    result = item.get("content", "")
                    parts.append(f"[Tool Result: {str(result)[:100]}]")
        return " ".join(parts)
    return ""


def project_name_from_path(project_path: str) -> str:
    """Convert '-home-fa064159-naukri-apply-services' → 'naukri/apply-services'"""
    parts = project_path.lstrip("-").split("-")
    # Drop 'home' and username
    if len(parts) > 2 and parts[0] == "home":
        parts = parts[2:]
    return "/".join(parts) if parts else project_path


def iter_session_files() -> Generator[tuple[str, Path], None, None]:
    """Yield (project_name, jsonl_path) for every Claude session file."""
    if not CLAUDE_PROJECTS_DIR.exists():
        return
    for project_dir in CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        project = project_name_from_path(project_dir.name)
        for jsonl_file in project_dir.glob("*.jsonl"):
            yield project, jsonl_file


def parse_session_file(project: str, jsonl_path: Path) -> int:
    """
    Parse a single session JSONL file and write data to DB.
    Returns number of new messages inserted.
    """
    session_id = jsonl_path.stem
    entries = []

    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except (OSError, PermissionError):
        return 0

    if not entries:
        return 0

    # Build uuid → text map for user messages
    user_messages: dict[str, str] = {}
    cwd = ""
    model = ""
    first_ts = ""
    last_ts = ""

    for entry in entries:
        if entry.get("type") == "user":
            uid = entry.get("uuid", "")
            text = extract_text(entry.get("message", {}).get("content", ""))
            if uid and text:
                user_messages[uid] = text
            if not cwd:
                cwd = entry.get("cwd", "")
            ts = entry.get("timestamp", "")
            if ts:
                if not first_ts or ts < first_ts:
                    first_ts = ts
                if not last_ts or ts > last_ts:
                    last_ts = ts

    new_count = 0

    # Now process assistant messages that have usage data
    # Group by request_id to avoid duplicates (Claude Code emits multiple
    # partial entries per API call — we only want the one with usage)
    seen_request_ids: set[str] = set()

    for entry in entries:
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message", {})
        usage = msg.get("usage")
        if not usage:
            continue

        req_id = entry.get("requestId", "")
        if req_id and req_id in seen_request_ids:
            continue
        if req_id:
            seen_request_ids.add(req_id)

        uuid = entry.get("uuid", "")
        if db.message_exists(uuid):
            continue

        ts = entry.get("timestamp", "")
        entry_model = msg.get("model", model) or model
        if entry_model:
            model = entry_model

        if ts:
            if not first_ts or ts < first_ts:
                first_ts = ts
            if not last_ts or ts > last_ts:
                last_ts = ts

        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_creation = usage.get("cache_creation_input_tokens", 0)

        # Find the user prompt that triggered this assistant message
        parent_uuid = entry.get("parentUuid", "")
        prompt_text = user_messages.get(parent_uuid, "")

        # If not direct parent, walk up the chain looking for user message
        if not prompt_text:
            for e in entries:
                if e.get("uuid") == parent_uuid and e.get("type") == "user":
                    prompt_text = extract_text(e.get("message", {}).get("content", ""))
                    break

        response_text = extract_text(msg.get("content", ""))

        cost = estimate_cost(entry_model, input_tokens, output_tokens, cache_read, cache_creation)

        row = {
            "session_id": session_id,
            "message_uuid": uuid,
            "timestamp": ts,
            "role": "user",
            "prompt_text": prompt_text[:4000] if prompt_text else "",
            "response_text": response_text[:2000] if response_text else "",
            "model": entry_model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
            "cache_creation_tokens": cache_creation,
            "total_tokens": input_tokens + output_tokens + cache_read + cache_creation,
            "estimated_cost_usd": cost,
        }

        db.upsert_session(session_id, project, cwd, ts or first_ts, entry_model)
        db.insert_message(row)
        new_count += 1

    return new_count


def parse_all_sessions() -> int:
    """Parse all Claude sessions. Returns total new messages found."""
    total = 0
    for project, path in iter_session_files():
        total += parse_session_file(project, path)
    return total
