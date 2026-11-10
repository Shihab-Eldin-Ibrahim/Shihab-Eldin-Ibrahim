#!/usr/bin/env python3
"""Build local profile statistics from public GitHub API data (stdlib only).

Run normally to fetch a fresh snapshot. Use --from-json assets/stats.json to
rebuild the SVGs without network access; add --check to check rather than write.
The API token is optional locally and comes from GITHUB_TOKEN in GitHub Actions.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from html import escape
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


USERNAME = "Shihab-Eldin-Ibrahim"
ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"
SVG_NS = "http://www.w3.org/2000/svg"
PALETTE = ("#5EEAD4", "#A78BFA", "#7DD3FC")
ARTIFACT_NAMES = ("stats-overview.svg", "stats-languages.svg", "stats.json")


class SnapshotError(Exception):
    """A fetch, validation or write failed; do not replace the old snapshot."""


def api_get(path: str):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{USERNAME}-profile-statistics",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(API + path, headers=headers)
    for attempt in range(3):
        try:
            with urlopen(request, timeout=20) as response:
                return json.load(response)
        except HTTPError as exc:
            if attempt == 2 or exc.code not in (429, 500, 502, 503, 504):
                raise SnapshotError(f"GitHub API returned HTTP {exc.code} for {path}.") from exc
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            if attempt == 2:
                # Do not echo request headers or server bodies, which may contain tokens.
                raise SnapshotError(f"Could not read valid GitHub API data for {path}.") from exc
        time.sleep(attempt + 1)
    raise SnapshotError(f"GitHub API request failed for {path}.")


def fetch_snapshot() -> dict:
    user = api_get(f"/users/{USERNAME}")
    if not isinstance(user, dict) or user.get("login", "").lower() != USERNAME.lower():
        raise SnapshotError("GitHub returned an unexpected user identity.")

    repositories = []
    for page in range(1, 101):
        query = urlencode({"type": "owner", "per_page": 100, "page": page})
        batch = api_get(f"/users/{USERNAME}/repos?{query}")
        if not isinstance(batch, list):
            raise SnapshotError("GitHub returned an invalid repository list.")
        repositories.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise SnapshotError("Repository pagination exceeded the safety limit.")

    if any(not isinstance(repo, dict) or repo.get("private") is not False for repo in repositories):
        raise SnapshotError("Repository response contained unexpected private or invalid data.")
    ids = [repo.get("id") for repo in repositories]
    if None in ids or len(set(ids)) != len(ids):
        raise SnapshotError("Repository pagination returned missing or duplicate identities.")
    if user.get("public_repos") != len(repositories):
        raise SnapshotError("Public repository totals changed during retrieval; retry later.")

    projects = [
        repo for repo in repositories
        if not repo.get("fork") and not repo.get("archived")
        and repo.get("name", "").lower() != USERNAME.lower()
    ]
    language_bytes: Counter = Counter()
    primary_languages: Counter = Counter()
    records = []
    for repo in sorted(projects, key=lambda entry: entry["name"].lower()):
        name = repo["name"]
        languages = api_get(f"/repos/{USERNAME}/{quote(name, safe='')}/languages")
        if not isinstance(languages, dict) or any(
            not isinstance(language, str) or not language
            or type(count) is not int or count < 0
            for language, count in languages.items()
        ):
            raise SnapshotError(f"Invalid language-byte response for {name}.")
        language_bytes.update(languages)
        primary = repo.get("language")
        if primary:
            primary_languages[primary] += 1
        records.append({
            "name": name,
            "url": repo["html_url"],
            "primary_language": primary,
            "pushed_at": repo.get("pushed_at"),
            "language_bytes": dict(sorted(languages.items())),
        })

    fetched = datetime.now(timezone.utc).replace(microsecond=0)
    pushes = [repo["pushed_at"] for repo in records if repo["pushed_at"]]
    snapshot = {
        "schema_version": 1,
        "username": USERNAME,
        "fetched_at": fetched.isoformat().replace("+00:00", "Z"),
        "snapshot_date_utc": fetched.date().isoformat(),
        "public_repositories": len(repositories),
        "project_repositories": len(projects),
        "primary_language_counts": dict(sorted(primary_languages.items())),
        "latest_project_push": max(pushes) if pushes else None,
        "language_bytes": dict(sorted(language_bytes.items())),
        "repositories": records,
        "definitions": {
            "public_repositories": "All publicly visible repositories owned by the account, including forks and the profile repository.",
            "project_repositories": "Public repositories excluding forks, archived repositories and the username-named profile repository.",
            "primary_language_counts": "Number of included project repositories assigned each primary language by GitHub.",
            "latest_project_push": "Latest GitHub pushed_at timestamp among included project repositories; not a commit or contribution count.",
            "language_bytes": "Sum of GitHub language API bytes across included project repositories; not proficiency or time spent coding.",
        },
        "source": f"https://api.github.com/users/{USERNAME}/repos",
    }
    validate_snapshot(snapshot)
    return snapshot


def validate_snapshot(snapshot: dict) -> None:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise SnapshotError("Unsupported statistics snapshot schema.")
    if snapshot.get("username") != USERNAME:
        raise SnapshotError("Statistics snapshot belongs to a different account.")
    try:
        fetched = datetime.fromisoformat(snapshot["fetched_at"].replace("Z", "+00:00"))
        if fetched.utcoffset() != timezone.utc.utcoffset(fetched):
            raise ValueError("Snapshot time must be UTC")
        if fetched.date().isoformat() != snapshot["snapshot_date_utc"]:
            raise ValueError("Date does not match snapshot time")
        for key in ("public_repositories", "project_repositories"):
            if type(snapshot[key]) is not int or snapshot[key] < 0:
                raise ValueError("Invalid repository count")
        if snapshot["project_repositories"] > snapshot["public_repositories"]:
            raise ValueError("Project count exceeds public count")
        for key in ("primary_language_counts", "language_bytes"):
            values = snapshot[key]
            if not isinstance(values, dict) or any(
                not isinstance(name, str) or not name or type(value) is not int or value < 0
                for name, value in values.items()
            ):
                raise ValueError("Invalid language data")
        records = snapshot["repositories"]
        if not isinstance(records, list) or len(records) != snapshot["project_repositories"]:
            raise ValueError("Project records do not match count")
        totals: Counter = Counter()
        primary: Counter = Counter()
        names = set()
        pushes = []
        for record in records:
            name = record["name"]
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                raise ValueError("Invalid repository name")
            if name.lower() == USERNAME.lower() or name.lower() in names:
                raise ValueError("Profile or duplicate repository in project records")
            names.add(name.lower())
            if record["url"] != f"https://github.com/{USERNAME}/{name}":
                raise ValueError("Unexpected repository URL")
            totals.update(record["language_bytes"])
            if record["primary_language"]:
                primary[record["primary_language"]] += 1
            if record["pushed_at"]:
                datetime.fromisoformat(record["pushed_at"].replace("Z", "+00:00"))
                pushes.append(record["pushed_at"])
        if dict(totals) != snapshot["language_bytes"] or dict(primary) != snapshot["primary_language_counts"]:
            raise ValueError("Aggregate language data does not match source records")
        if snapshot["latest_project_push"] != (max(pushes) if pushes else None):
            raise ValueError("Latest push does not match source records")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SnapshotError(f"Invalid statistics snapshot: {exc}.") from exc


def svg_card(title: str, description: str, contents: str) -> str:
    return f'''<svg xmlns="{SVG_NS}" width="420" height="208" viewBox="0 0 420 208" role="img" aria-labelledby="title desc">
  <title id="title">{escape(title)}</title>
  <desc id="desc">{escape(description)}</desc>
  <rect x="0.5" y="0.5" width="419" height="207" rx="16" fill="#0D1117" stroke="#30363D"/>
  <g font-family="Segoe UI, Arial, sans-serif" fill="#F0F3F6">
{contents}
  </g>
</svg>
'''


def render_overview(snapshot: dict) -> str:
    date = escape(snapshot["snapshot_date_utc"])
    public_count = snapshot["public_repositories"]
    python_count = snapshot["primary_language_counts"].get("Python", 0)
    latest = escape(snapshot["latest_project_push"][:10] if snapshot["latest_project_push"] else "No pushes")
    description = (
        f"GitHub snapshot on {date} UTC: {public_count} public repositories, "
        f"{python_count} Python-primary project repositories. Latest project repository push: {latest}. "
        "Project metrics exclude forks, archived repositories and this profile repository."
    )
    contents = f'''    <text x="24" y="34" font-size="20" font-weight="600">Engineering in public</text>
    <circle cx="388" cy="27" r="4" fill="#5EEAD4"/>
    <text x="24" y="91" font-size="42" font-weight="700" fill="#5EEAD4">{public_count}</text>
    <text x="24" y="119" font-size="18">Public repositories</text>
    <path d="M219 60V125" stroke="#30363D"/>
    <text x="244" y="91" font-size="42" font-weight="700" fill="#A78BFA">{python_count}</text>
    <text x="244" y="119" font-size="18">Python projects</text>
    <path d="M24 144H396" stroke="#30363D"/>
    <text x="24" y="171" font-size="18" fill="#AAB6C5">Last project push</text>
    <text x="396" y="171" text-anchor="end" font-size="18">{latest}</text>
    <text x="24" y="194" font-size="11" fill="#AAB6C5">Snapshot {date} UTC · public GitHub data</text>'''
    return svg_card("Public GitHub repository snapshot", description, contents)


def render_languages(snapshot: dict) -> str:
    date = escape(snapshot["snapshot_date_utc"])
    languages = sorted(snapshot["language_bytes"].items(), key=lambda item: (-item[1], item[0]))
    languages = [(name, count) for name, count in languages if count > 0]
    total = sum(count for _, count in languages)
    visible = languages[:2]
    if len(languages) > 2:
        visible.append(("Other", sum(count for _, count in languages[2:])))
    rows = []
    segments = []
    x = 24.0
    for index, (name, count) in enumerate(visible):
        percent = 100 * count / total
        width = 372 * count / total
        color = PALETTE[index]
        segments.append(f'    <rect x="{x:.3f}" y="65" width="{width:.3f}" height="10" fill="{color}"/>')
        x += width
        y = 105 + index * 29
        rows.append(f'    <circle cx="28" cy="{y - 6}" r="4" fill="{color}"/>')
        # Long names are shortened only in the card; full names remain in JSON/desc.
        label = name if len(name) <= 23 else name[:21] + "…"
        rows.append(f'    <text x="42" y="{y}" font-size="18">{escape(label)}</text>')
        rows.append(f'    <text x="396" y="{y}" font-size="18" text-anchor="end">{percent:.1f}%</text>')
    if not total:
        rows.append('    <text x="24" y="113" font-size="18" fill="#AAB6C5">No language bytes reported yet.</text>')
    description = (
        f"Public project code by GitHub language bytes, snapshot {date} UTC. "
        + "; ".join(f"{name}: {count:,} bytes" for name, count in languages)
        + ". Excludes forks, archived repositories and this profile repository. These proportions are not proficiency."
    )
    contents = f'''    <text x="24" y="32" font-size="20" font-weight="600">Public code by bytes</text>
    <text x="24" y="51" font-size="11" fill="#AAB6C5">GitHub language bytes, not proficiency</text>
    <rect x="24" y="65" width="372" height="10" fill="#21262D"/>
{chr(10).join(segments)}
{chr(10).join(rows)}
    <text x="24" y="194" font-size="11" fill="#AAB6C5">Snapshot {date} UTC · project repositories only</text>'''
    return svg_card("Public project programming languages", description, contents)


def build_artifacts(snapshot: dict) -> dict[str, bytes]:
    validate_snapshot(snapshot)
    rendered = {
        "stats-overview.svg": render_overview(snapshot),
        "stats-languages.svg": render_languages(snapshot),
        "stats.json": json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
    }
    for name in ARTIFACT_NAMES[:2]:
        ET.fromstring(rendered[name])
    return {name: text.encode("utf-8") for name, text in rendered.items()}


def write_artifacts(directory: Path, artifacts: dict[str, bytes]) -> None:
    """Prepare every output before replacement; roll back a failed replacement."""
    directory.mkdir(parents=True, exist_ok=True)
    old = {name: (directory / name).read_bytes() if (directory / name).exists() else None for name in artifacts}
    replaced = []
    with tempfile.TemporaryDirectory(prefix=".stats-", dir=directory) as temporary:
        staging = Path(temporary)
        for name, contents in artifacts.items():
            (staging / name).write_bytes(contents)
        try:
            for name in artifacts:
                if old[name] != artifacts[name]:
                    os.replace(staging / name, directory / name)
                    replaced.append(name)
        except OSError as exc:
            for name in reversed(replaced):
                if old[name] is None:
                    (directory / name).unlink(missing_ok=True)
                else:
                    (staging / name).write_bytes(old[name])
                    os.replace(staging / name, directory / name)
            raise SnapshotError("Could not write statistics; previous snapshot restored.") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-json", type=Path, help="Rebuild a saved snapshot without network access.")
    parser.add_argument("--check", action="store_true", help="Check generated files without writing them (requires --from-json).")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "assets", help="Output directory; default: repository assets/.")
    args = parser.parse_args(argv)
    if args.check and not args.from_json:
        parser.error("--check requires --from-json to avoid comparing different retrieval timestamps")
    try:
        snapshot = json.loads(args.from_json.read_text(encoding="utf-8")) if args.from_json else fetch_snapshot()
        artifacts = build_artifacts(snapshot)
        if args.check:
            mismatched = [name for name, data in artifacts.items() if not (args.output_dir / name).is_file() or (args.output_dir / name).read_bytes() != data]
            if mismatched:
                raise SnapshotError("Generated statistics differ: " + ", ".join(mismatched))
            print("Statistics snapshot and SVGs are consistent.")
        else:
            write_artifacts(args.output_dir, artifacts)
            print(f"Updated {len(artifacts)} local statistics assets from snapshot {snapshot['snapshot_date_utc']} UTC.")
        return 0
    except (SnapshotError, OSError, ValueError, ET.ParseError) as exc:
        print(f"Statistics update failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
