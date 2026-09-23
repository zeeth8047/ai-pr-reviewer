#!/usr/bin/env python3
"""
AI PR Reviewer.

Fetches the diff of a pull request from the GitHub API, sends it to an
OpenAI-compatible chat completions endpoint for review, and posts the
result as a comment on the pull request.

Configuration (environment variables):
    GITHUB_TOKEN       Token with pull-request read + comment write access.
    GITHUB_REPOSITORY  "owner/repo" (set automatically in GitHub Actions).
    GITHUB_EVENT_PATH  Path to the webhook payload (set automatically in
                       GitHub Actions); used to discover the PR number.
    LLM_API_KEY        API key for the LLM provider (set as a repo secret).
    LLM_BASE_URL       Base URL of the chat completions endpoint
                       (default: https://api.openai.com/v1).
    LLM_MODEL          Model name to request (default: gpt-4o-mini).
    MAX_DIFF_CHARS     Max diff characters sent to the LLM; larger diffs are
                       truncated (default: 40000).

Examples:
    # Preview the prompt without calling the API or posting anything:
    python scripts/review.py --diff-file /tmp/changes.diff --dry-run

    # Full run against a real PR (needs GITHUB_TOKEN + LLM_API_KEY):
    GITHUB_TOKEN=... LLM_API_KEY=... \\
        python scripts/review.py --repo octo-org/hello-world --pr-number 42
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

MARKER = "<!-- ai-pr-reviewer -->"
GITHUB_API = "https://api.github.com"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_MAX_DIFF_CHARS = 40000
USER_AGENT = "ai-pr-reviewer/1.0"

SEVERITY_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}

SYSTEM_PROMPT = (
    "You are a senior software engineer performing a pull request code review. "
    "Be thorough but fair: flag real correctness, security, performance, and "
    "readability problems, and praise genuinely good patterns. Do not nitpick "
    "formatting that a linter would catch. Always respond with exactly one "
    "JSON object and nothing else."
)


def die(message):
    """Print an error to stderr and exit non-zero."""
    print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def github_request(method, url, token, data=None,
                   accept="application/vnd.github+json"):
    """Make an authenticated GitHub API request; return decoded JSON or text."""
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Accept", accept)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = resp.read()
            if "json" in resp.headers.get("Content-Type", ""):
                return json.loads(payload.decode("utf-8"))
            return payload.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        die(f"GitHub API {method} {url} failed (HTTP {exc.code}): {detail}")
    except urllib.error.URLError as exc:
        die(f"GitHub API request failed: {exc.reason}")


def fetch_pr_diff(repo, pr_number, token):
    """Download the unified diff of a pull request."""
    url = f"{GITHUB_API}/repos/{repo}/pulls/{pr_number}"
    return github_request("GET", url, token,
                          accept="application/vnd.github.diff")


def find_existing_comment(repo, pr_number, token):
    """Return the id of our previous review comment, if one exists."""
    url = (f"{GITHUB_API}/repos/{repo}/issues/{pr_number}"
           f"/comments?per_page=100")
    comments = github_request("GET", url, token)
    for comment in comments:
        body = comment.get("body") or ""
        login = ((comment.get("user") or {}).get("login") or "")
        # Only touch comments that carry our marker and were posted by a bot,
        # so we never overwrite a human's comment.
        if MARKER in body and login.endswith("[bot]"):
            return comment.get("id")
    return None


def post_comment(repo, pr_number, token, body, comment_id=None):
    """Create (or update, if comment_id is given) the review comment."""
    if comment_id:
        url = f"{GITHUB_API}/repos/{repo}/issues/comments/{comment_id}"
        github_request("PATCH", url, token, data={"body": body})
        print(f"Updated existing review comment on {repo}#{pr_number}.")
    else:
        url = f"{GITHUB_API}/repos/{repo}/issues/{pr_number}/comments"
        github_request("POST", url, token, data={"body": body})
        print(f"Posted review comment on {repo}#{pr_number}.")


# ---------------------------------------------------------------------------
# Event / input handling
# ---------------------------------------------------------------------------

def event_payload():
    """Load the GitHub Actions event payload, or return an empty dict."""
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def pr_number_from_event():
    return (event_payload().get("pull_request") or {}).get("number")


def repo_from_event():
    return (event_payload().get("repository") or {}).get("full_name")


def maybe_truncate(diff, limit):
    """Cap the diff at `limit` chars, cutting on a line boundary.

    Returns (diff, original_length_or_None).
    """
    if len(diff) <= limit:
        return diff, None
    cut = diff[:limit].rsplit("\n", 1)[0]
    return cut, len(diff)


# ---------------------------------------------------------------------------
# LLM interaction
# ---------------------------------------------------------------------------

def build_prompts(diff, truncated_from):
    """Build the (system, user) prompts sent to the chat completions API."""
    note = ""
    if truncated_from:
        note = (
            f"\n\nNote: the diff was truncated from {truncated_from} to "
            f"{len(diff)} characters; review only what is visible."
        )
    user_prompt = (
        "Review the following pull request diff and return a single JSON "
        "object (no markdown fences, no extra text) with this shape:\n"
        "{\n"
        '  "summary": "2-4 sentence overview of what changed and why",\n'
        '  "strengths": ["things done well"],\n'
        '  "issues": [\n'
        '    {"severity": "high|medium|low", "file": "path/to/file", "line": 123,\n'
        '     "title": "short title", "detail": "what is wrong and why",\n'
        '     "suggestion": "how to fix it"}\n'
        "  ],\n"
        '  "suggestions": ["broader improvements or follow-ups"]\n'
        "}\n"
        "Severity guide: high = bug, security issue, or data-loss risk; "
        "medium = likely bug, performance problem, or maintainability "
        "concern; low = nit, style, or minor improvement. Be concrete and "
        "reference the code. If the diff looks fine, return empty arrays.\n"
        f"\n```diff\n{diff}\n```{note}"
    )
    return SYSTEM_PROMPT, user_prompt


def call_llm(base_url, api_key, model, system_prompt, user_prompt):
    """Call the chat completions endpoint; return the assistant message."""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("User-Agent", USER_AGENT)
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        die(f"LLM API call failed (HTTP {exc.code}): {detail}")
    except urllib.error.URLError as exc:
        die(f"LLM API request failed: {exc.reason}")
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        die(f"Unexpected LLM response shape: {str(data)[:300]}")


def extract_json(text):
    """Pull the JSON object out of the model's reply (tolerates fences)."""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    candidate = text[start:end + 1] if 0 <= start < end else text
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        die("The model did not return valid JSON. "
            "Try again or switch models. Raw reply was logged above.")


# ---------------------------------------------------------------------------
# Comment rendering
# ---------------------------------------------------------------------------

def format_comment(review):
    """Render the structured review as a Markdown PR comment."""
    lines = [MARKER, "## 🤖 AI Code Review", ""]
    lines += ["**Summary**", "", review.get("summary") or "No summary provided.", ""]

    strengths = review.get("strengths") or []
    if strengths:
        lines += ["### ✅ Strengths", ""]
        lines += [f"- {s}" for s in strengths]
        lines += [""]

    issues = review.get("issues") or []
    lines += ["### ⚠️ Issues", ""]
    if issues:
        lines += ["| Severity | Location | Details |",
                  "|---|---|---|"]
        for issue in issues:
            sev = str(issue.get("severity", "low")).lower()
            emoji = SEVERITY_EMOJI.get(sev, "⚪")
            loc = f"`{issue.get('file', '?')}:{issue.get('line', '?')}`"
            cell = str(issue.get("title", "") or "")
            detail = issue.get("detail") or ""
            suggestion = issue.get("suggestion") or ""
            if detail:
                cell += f"<br>{detail}"
            if suggestion:
                cell += f"<br>**Suggestion:** {suggestion}"
            cell = cell.replace("|", "\\|")  # keep the table intact
            lines.append(f"| {emoji} {sev.capitalize()} | {loc} | {cell} |")
    else:
        lines.append("No issues found. 🎉")
    lines += [""]

    suggestions = review.get("suggestions") or []
    if suggestions:
        lines += ["### 💡 Suggestions", ""]
        lines += [f"- {s}" for s in suggestions]
        lines += [""]

    lines += [
        "---",
        "<sub>Automated review — AI output can be wrong. Always apply human "
        "judgment; this does not replace human code review.</sub>",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Post an AI-generated review comment on a pull request.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the LLM prompt instead of calling the API "
                             "or posting a comment.")
    parser.add_argument("--diff-file",
                        help="Read the diff from a local file instead of the "
                             "GitHub API (handy for testing).")
    parser.add_argument("--repo",
                        help='Repository as OWNER/NAME '
                             '(default: $GITHUB_REPOSITORY).')
    parser.add_argument("--pr-number", type=int,
                        help="Pull request number "
                             "(default: from $GITHUB_EVENT_PATH).")
    parser.add_argument("--max-diff-chars", type=int,
                        default=int(os.environ.get("MAX_DIFF_CHARS",
                                                   DEFAULT_MAX_DIFF_CHARS)),
                        help="Max diff characters sent to the LLM "
                             "(default: 40000).")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    repo = args.repo or os.environ.get("GITHUB_REPOSITORY") or repo_from_event()
    pr_number = args.pr_number or pr_number_from_event()

    if args.diff_file:
        with open(args.diff_file, encoding="utf-8", errors="replace") as fh:
            diff = fh.read()
    else:
        token = os.environ.get("GITHUB_TOKEN")
        missing = [name for name, val in
                   (("GITHUB_TOKEN", token),
                    ("repository", repo),
                    ("PR number", pr_number)) if not val]
        if missing:
            die("Missing: " + ", ".join(missing) + ". In GitHub Actions these "
                "come from the workflow and event payload; locally, pass "
                "--repo/--pr-number or use --diff-file.")
        diff = fetch_pr_diff(repo, pr_number, token)

    if not diff.strip():
        print("Diff is empty — nothing to review.")
        return 0

    diff, truncated_from = maybe_truncate(diff, args.max_diff_chars)
    system_prompt, user_prompt = build_prompts(diff, truncated_from)

    base_url = os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL)
    model = os.environ.get("LLM_MODEL", DEFAULT_MODEL)

    if args.dry_run:
        print("=== DRY RUN: no API calls, no comment posted ===\n")
        print(f"Would call: POST {base_url.rstrip('/')}/chat/completions")
        print(f"Would use model: {model}")
        if repo and pr_number:
            print(f"Would post review comment to: {repo}#{pr_number}")
        print("\n----- SYSTEM PROMPT -----\n")
        print(system_prompt)
        print("\n----- USER PROMPT -----\n")
        print(user_prompt)
        return 0

    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        die("LLM_API_KEY is not set. Add it as a repository secret "
            "(Settings → Secrets and variables → Actions), or re-run with "
            "--dry-run to preview the prompt without calling the API.")

    print(f"Requesting review from {model} ...", flush=True)
    reply = call_llm(base_url, api_key, model, system_prompt, user_prompt)
    review = extract_json(reply)
    comment = format_comment(review)

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        die("GITHUB_TOKEN is not set; cannot post the review comment.")
    existing_id = find_existing_comment(repo, pr_number, token)
    post_comment(repo, pr_number, token, comment, existing_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
