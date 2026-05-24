#!/usr/bin/env python3
"""
Adaptive GitHub profile README generator.

What it does:
- Reads public repos from the GitHub REST API.
- Pulls each repo's language breakdown.
- Builds a dynamic README section with:
  - detected languages
  - language share table
  - most active projects
  - featured projects scored by recency, completeness, topics, and stars
  - tools inferred from topics/descriptions/languages
- Replaces only the block between:
  <!--START_SECTION:github-pulse-->
  <!--END_SECTION:github-pulse-->

No external Python dependencies.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
README_PATH = ROOT / "README.md"
CONFIG_PATH = ROOT / "profile_config.json"

START = "<!--START_SECTION:github-pulse-->"
END = "<!--END_SECTION:github-pulse-->"


LANG_META = {
    "Python": ("3776AB", "python", "white"),
    "JavaScript": ("F7DF1E", "javascript", "black"),
    "TypeScript": ("3178C6", "typescript", "white"),
    "Jupyter Notebook": ("F37626", "jupyter", "white"),
    "HTML": ("E34F26", "html5", "white"),
    "CSS": ("1572B6", "css3", "white"),
    "SQL": ("4479A1", "postgresql", "white"),
    "Shell": ("4EAA25", "gnubash", "white"),
    "R": ("276DC3", "r", "white"),
    "Java": ("ED8B00", "openjdk", "white"),
    "C": ("A8B9CC", "c", "black"),
    "C++": ("00599C", "cplusplus", "white"),
    "Go": ("00ADD8", "go", "black"),
    "Rust": ("000000", "rust", "white"),
    "Ruby": ("CC342D", "ruby", "white"),
    "PHP": ("777BB4", "php", "white"),
    "Vue": ("4FC08D", "vuedotjs", "white"),
    "Svelte": ("FF3E00", "svelte", "white"),
    "Dockerfile": ("2496ED", "docker", "white"),
    "Makefile": ("427819", "gnu", "white"),
}


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Missing {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def gh_get(path: str, token: str | None = None, retries: int = 3) -> Any:
    """GET GitHub API path and return decoded JSON."""
    url = f"https://api.github.com{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "adaptive-profile-readme-generator",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if exc.code in {403, 429, 500, 502, 503, 504} and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API error {exc.code} for {path}: {body}") from exc
        except urllib.error.URLError as exc:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Network error for {path}: {exc}") from exc


def paginated(path: str, token: str | None = None, max_pages: int = 10) -> List[Dict[str, Any]]:
    """Simple pagination using page/per_page query params."""
    sep = "&" if "?" in path else "?"
    items: List[Dict[str, Any]] = []

    for page in range(1, max_pages + 1):
        chunk = gh_get(f"{path}{sep}per_page=100&page={page}", token=token)
        if not chunk:
            break
        if not isinstance(chunk, list):
            raise TypeError(f"Expected list from GitHub API, got {type(chunk)}")
        items.extend(chunk)
        if len(chunk) < 100:
            break

    return items


def pct(value: int, total: int) -> float:
    return round((value / total) * 100, 1) if total else 0.0


def safe_date(value: str | None) -> datetime:
    if not value:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def days_since(value: str | None) -> int:
    dt = safe_date(value)
    return max(0, (datetime.now(timezone.utc) - dt).days)


def repo_languages(owner: str, repo_name: str, token: str | None) -> Dict[str, int]:
    data = gh_get(f"/repos/{owner}/{repo_name}/languages", token=token)
    return data if isinstance(data, dict) else {}


def normalize_topic(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def language_badge(name: str, percent: float) -> str:
    color, logo, logo_color = LANG_META.get(name, ("6E7681", "github", "white"))
    label = urllib.parse.quote(name.replace("-", "--"))
    message = urllib.parse.quote(f"{percent:g}%")
    logo_q = urllib.parse.quote(logo)
    logo_color_q = urllib.parse.quote(logo_color)
    return (
        f'<img src="https://img.shields.io/badge/{label}-{message}-{color}'
        f'?style=for-the-badge&logo={logo_q}&logoColor={logo_color_q}" />'
    )


def compact_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def format_date(value: str | None) -> str:
    dt = safe_date(value)
    if dt.year == 1970:
        return "unknown"
    return dt.strftime("%Y-%m-%d")


def repo_url(repo: Dict[str, Any]) -> str:
    return repo.get("html_url") or f"https://github.com/{repo.get('full_name', '')}"


def clean_description(desc: str | None, max_len: int = 170) -> str:
    if not desc:
        return "No description yet."
    desc = re.sub(r"\s+", " ", desc).strip()
    if len(desc) <= max_len:
        return desc
    return desc[: max_len - 1].rstrip() + "…"


def infer_tools(repos: List[Dict[str, Any]], config: Dict[str, Any]) -> List[str]:
    text_parts = []
    for repo in repos:
        text_parts.append(repo.get("name", ""))
        text_parts.append(repo.get("description", "") or "")
        text_parts.extend(repo.get("topics", []) or [])
        text_parts.append(repo.get("_primary_language") or "")
        text_parts.extend((repo.get("_languages") or {}).keys())

    haystack = normalize_topic(" ".join(text_parts))
    tools = []

    for label, needles in config.get("known_tools", {}).items():
        if any(normalize_topic(n) in haystack for n in needles):
            tools.append(label)

    return sorted(dict.fromkeys(tools))


def score_repo(repo: Dict[str, Any], config: Dict[str, Any]) -> float:
    """Score repos for 'featured' section. This avoids hard-coded project lists."""
    topics = {normalize_topic(t) for t in repo.get("topics", []) or []}
    boost_topics = {normalize_topic(t) for t in config.get("featured_topic_boost", [])}

    recency = max(0, 120 - days_since(repo.get("pushed_at"))) / 120
    stars = min(int(repo.get("stargazers_count") or 0), 25) / 25
    has_homepage = 1 if repo.get("homepage") else 0
    has_description = 1 if repo.get("description") else 0
    topic_match = len(topics & boost_topics)
    language_count = len(repo.get("_languages", {}) or {})

    return (
        recency * 4
        + stars * 2
        + has_homepage * 1.5
        + has_description * 1.0
        + min(topic_match, 5) * 0.7
        + min(language_count, 4) * 0.25
    )


def render_repo_line(repo: Dict[str, Any]) -> str:
    name = repo["name"]
    url = repo_url(repo)
    desc = clean_description(repo.get("description"))
    lang = repo.get("_primary_language") or repo.get("language") or "Mixed"
    pushed = format_date(repo.get("pushed_at"))
    stars = int(repo.get("stargazers_count") or 0)
    homepage = repo.get("homepage")
    homepage_part = f" · [walkthrough]({homepage})" if homepage else ""
    topics = repo.get("topics", []) or []
    top_topics = " · ".join(f"`{t}`" for t in topics[:4])
    topic_part = f" · {top_topics}" if top_topics else ""
    return (
        f"* **[{name}]({url})**{homepage_part} — {desc} "
        f"`{lang}` · ⭐ {stars} · updated `{pushed}`{topic_part}"
    )


def render_dynamic_section(
    repos: List[Dict[str, Any]],
    language_totals: Dict[str, int],
    config: Dict[str, Any],
) -> str:
    total_lang_bytes = sum(language_totals.values())
    top_languages = sorted(language_totals.items(), key=lambda x: x[1], reverse=True)

    active = sorted(repos, key=lambda r: safe_date(r.get("pushed_at")), reverse=True)
    active = active[: int(config.get("max_active_projects", 6))]

    featured = sorted(repos, key=lambda r: score_repo(r, config), reverse=True)
    featured = featured[: int(config.get("max_featured_projects", 6))]

    tools = infer_tools(repos, config)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    blocks = []
    blocks.append("### 📡 GitHub Pulse")
    blocks.append("")
    blocks.append(
        f"Adaptive section generated from GitHub repo metadata. "
        f"Last update: `{generated_at}`."
    )
    blocks.append("")
    blocks.append(
        f"* Public/source repos tracked: **{len(repos)}**\n"
        f"* Detected languages: **{len(top_languages)}**\n"
        f"* Most recent repo update: **{format_date(active[0].get('pushed_at')) if active else 'unknown'}**"
    )

    if top_languages:
        blocks.append("")
        blocks.append("### 🧰 Stack detected from repositories")
        blocks.append("")
        badges = [language_badge(name, pct(size, total_lang_bytes)) for name, size in top_languages[:8]]
        blocks.append('<div>\n  ' + "\n  ".join(badges) + "\n</div>")

        blocks.append("")
        blocks.append("### 🧬 Language weight")
        blocks.append("")
        blocks.append("| Language | Share | Bytes |")
        blocks.append("|---|---:|---:|")
        for name, size in top_languages[:10]:
            blocks.append(f"| {name} | {pct(size, total_lang_bytes):.1f}% | {compact_number(size)} |")

    if tools:
        blocks.append("")
        blocks.append("### 🔎 Tools inferred from repos")
        blocks.append("")
        blocks.append(" · ".join(f"`{tool}`" for tool in tools[:14]))

    if active:
        blocks.append("")
        blocks.append("### ⚡ Most active projects")
        blocks.append("")
        blocks.extend(render_repo_line(repo) for repo in active)

    if featured:
        blocks.append("")
        blocks.append("### 🌟 Featured projects — auto-ranked")
        blocks.append("")
        blocks.append(
            "Ranked by recent activity, repo completeness, homepage/walkthrough presence, topics, "
            "stars, and language diversity — not by a manually fixed list."
        )
        blocks.append("")
        blocks.extend(render_repo_line(repo) for repo in featured)

    blocks.append("")
    blocks.append(
        "<sub>This block is regenerated by GitHub Actions. "
        "New Python, JavaScript, TypeScript, SQL, notebook, or web projects will appear automatically after the next run.</sub>"
    )

    return "\n".join(blocks).strip()


def update_readme(dynamic: str) -> bool:
    if not README_PATH.exists():
        README_PATH.write_text(f"{START}\n{END}\n", encoding="utf-8")

    current = README_PATH.read_text(encoding="utf-8")

    if START not in current or END not in current:
        current = current.rstrip() + f"\n\n{START}\n{END}\n"

    pattern = re.compile(
        re.escape(START) + r".*?" + re.escape(END),
        flags=re.DOTALL,
    )

    replacement = f"{START}\n{dynamic}\n{END}"
    updated = pattern.sub(replacement, current)

    if updated != current:
        README_PATH.write_text(updated, encoding="utf-8")
        return True

    return False


def main() -> int:
    config = load_config()
    username = os.getenv("GITHUB_USERNAME") or config.get("username")
    if not username:
        raise ValueError("Set username in profile_config.json or GITHUB_USERNAME.")

    token = os.getenv("GITHUB_TOKEN")

    repos = paginated(
        f"/users/{urllib.parse.quote(username)}/repos?sort=pushed&type=owner",
        token=token,
    )

    excluded = set(config.get("exclude_repos", []))
    include_forks = bool(config.get("include_forks", False))
    include_archived = bool(config.get("include_archived", False))

    filtered: List[Dict[str, Any]] = []
    language_totals: Dict[str, int] = {}

    for repo in repos:
        name = repo.get("name", "")
        if name in excluded:
            continue
        if repo.get("private"):
            continue
        if repo.get("fork") and not include_forks:
            continue
        if repo.get("archived") and not include_archived:
            continue

        languages = repo_languages(username, name, token=token)
        repo["_languages"] = languages
        repo["_primary_language"] = max(languages, key=languages.get) if languages else repo.get("language")

        for lang, size in languages.items():
            language_totals[lang] = language_totals.get(lang, 0) + int(size)

        filtered.append(repo)

    dynamic = render_dynamic_section(filtered, language_totals, config)
    changed = update_readme(dynamic)

    print(f"Tracked repos: {len(filtered)}")
    print(f"Detected languages: {', '.join(sorted(language_totals)) or 'none'}")
    print("README updated." if changed else "README already up to date.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
