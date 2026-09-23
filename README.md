# 🤖 AI PR Reviewer

A reusable GitHub Action that posts an **AI-generated code review** as a comment
on every pull request. It fetches the PR diff via the GitHub API, sends it to
any **OpenAI-compatible chat completions endpoint** (OpenAI, Azure OpenAI,
Ollama, …), and posts a structured review — summary, strengths, severity-rated
issues, and suggestions — right on the PR.

> **Note:** AI reviews are a second pair of eyes, not a replacement for human
> review. Always apply human judgment before acting on the feedback.

## How it works

```
pull_request event (opened / synchronize / reopened)
        │
        ▼
┌───────────────────────┐
│  Your workflow        │  .github/workflows/ai-review.yml
│  (GitHub Actions)     │
└───────────┬───────────┘
            │  uses: <this action>
            ▼
┌───────────────────────┐
│  Composite action     │  action.yml
│  python3 scripts/     │
│  review.py            │
└───┬───────────────┬───┘
    │               │
    │ 1. GET PR     │ 2. POST prompt
    │    diff       │    3. parse JSON reply
    ▼               ▼
┌──────────────┐  ┌────────────────────────┐
│  GitHub API  │  │  OpenAI-compatible   │
│              │  │  chat completions    │
│ 4. POST/     │  │  endpoint            │
│    PATCH     │  └────────────────────────┘
│    review    │
│    comment   │
└──────────────┘
```

1. The workflow triggers on `pull_request` and invokes the composite action.
2. `scripts/review.py` downloads the PR's unified diff (`Accept:
   application/vnd.github.diff`).
3. The diff is embedded in a review prompt and sent to the configured chat
   completions endpoint. The model must reply with a single JSON object.
4. The JSON is rendered as Markdown and posted as a PR comment. Re-runs
   **update the existing comment** (matched via a hidden marker) instead of
   spamming new ones.

## Setup

1. **Add your LLM API key as a repository secret.**
   Repo → *Settings → Secrets and variables → Actions → New repository secret*
   - Name: `LLM_API_KEY`
   - Value: your provider's API key
2. **Add the workflow** (already included at
   `.github/workflows/ai-review.yml`) — or reference the action from another
   repo:
   ```yaml
   - uses: zeeth8047/ai-pr-reviewer@v1
     with:
       github-token: ${{ secrets.GITHUB_TOKEN }}
       llm-api-key: ${{ secrets.LLM_API_KEY }}
   ```
3. Open a pull request — the review comment appears automatically.

No secrets ever live in code: the API key is only read from the `LLM_API_KEY`
environment variable, which the action maps from the secret you pass in.

## Configuration

| Input (`with:`)   | Env var          | Default                     | Description |
|-------------------|------------------|-----------------------------|-------------|
| `github-token`    | `GITHUB_TOKEN`   | *(required)*                | Token for reading the diff and posting comments. Use `secrets.GITHUB_TOKEN`. |
| `llm-api-key`     | `LLM_API_KEY`    | *(required)*                | LLM provider API key (repository secret). |
| `llm-base-url`    | `LLM_BASE_URL`   | `https://api.openai.com/v1` | Base URL of the OpenAI-compatible chat completions endpoint. |
| `llm-model`       | `LLM_MODEL`      | `gpt-4o-mini`               | Model name to request. |
| `max-diff-chars`  | `MAX_DIFF_CHARS` | `40000`                     | Max diff characters sent to the LLM; larger diffs are truncated on a line boundary. |

Works with any OpenAI-compatible server — point `llm-base-url` at Azure
OpenAI or a local Ollama instance and pick the matching `llm-model`.

## Trying it locally (dry run)

No API key? Preview exactly what would be sent to the model:

```bash
# From a saved diff file — no network calls at all:
python3 scripts/review.py --diff-file /tmp/changes.diff --dry-run

# Against a real PR, but still without calling the LLM or posting:
GITHUB_TOKEN=... python3 scripts/review.py \
  --repo octo-org/hello-world --pr-number 42 --dry-run
```

If `LLM_API_KEY` is missing in a real (non-dry) run, the script exits with a
clear error telling you how to add the secret — it never fails cryptically.

## Example review output

For a PR that deserializes untrusted data with `pickle`:

> ## 🤖 AI Code Review
>
> **Summary**
>
> This PR adds a `load_user` helper that reads a file and deserializes it with
> `pickle.loads`. The intent is to restore cached user objects, but the input
> is not validated or restricted in any way.
>
> ### ⚠️ Issues
>
> | Severity | Location | Details |
> |---|---|---|
> | 🔴 High | `app.py:6` | **Unsafe deserialization**<br>Unpickling untrusted data allows arbitrary code execution.<br>**Suggestion:** Use `json` or another safe format; if pickle is required, verify integrity with HMAC first. |
> | 🟡 Medium | `app.py:5` | **File handle not closed**<br>`open(...)` without a context manager leaks the handle on error.<br>**Suggestion:** Use `with open(path, "rb") as fh:`. |
>
> ### 💡 Suggestions
>
> - Add a unit test covering malformed input to `load_user`.
>
> ---
> *Automated review — AI output can be wrong. Always apply human judgment;
> this does not replace human code review.*

## Limitations

- Very large diffs are truncated (`max-diff-chars`); the model only sees what
  fits.
- The model must return valid JSON — if it doesn't, the run fails with an
  explanatory error instead of posting garbage.
- Each run costs LLM tokens; consider `paths`/`paths-ignore` filters in the
  workflow for noisy repos.
- **AI reviews complement human review.** They catch common issues fast, but
  they can miss context, misunderstand intent, and occasionally hallucinate.
  Treat every finding as a suggestion until a human confirms it.
