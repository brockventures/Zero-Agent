#!/usr/bin/env python3
"""
evaluate_repo.py - GitHub Repository & Skill Metric Extractor & Ranker
Queries the GitHub REST API to extract quantitative health, velocity, and popularity metrics,
computing a composite quality score.
"""

import sys
import os
import json
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

def fetch_github_api(url):
    headers = {
        "User-Agent": "Antigravity-Repo-Evaluator/1.0",
        "Accept": "application/vnd.github.v3+json"
    }
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        headers["Authorization"] = f"token {github_token}"
    
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"HTTP Error {e.code}: {e.reason} for {url}\n")
        return None
    except Exception as e:
        sys.stderr.write(f"Error fetching {url}: {e}\n")
        return None

def evaluate_repository(repo_full_name):
    url = f"https://api.github.com/repos/{repo_full_name}"
    data = fetch_github_api(url)
    if not data:
        return None

    stars = data.get("stargazers_count", 0)
    forks = data.get("forks_count", 0)
    open_issues = data.get("open_issues_count", 0)
    pushed_at_str = data.get("pushed_at")
    archived = data.get("archived", False)
    license_info = data.get("license") or {}
    license_name = license_info.get("spdx_id") or license_info.get("name") or "None"
    description = data.get("description") or ""
    topics = data.get("topics", [])
    
    # Calculate days since last push
    days_since_push = None
    if pushed_at_str:
        pushed_dt = datetime.fromisoformat(pushed_at_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        days_since_push = (now - pushed_dt).days

    # Scoring algorithm (0 - 100)
    # Popularity (max 40 pts)
    pop_score = 0
    if stars > 10000:
        pop_score = 40
    elif stars > 1000:
        pop_score = 30 + (stars - 1000) / 9000 * 10
    elif stars > 100:
        pop_score = 20 + (stars - 100) / 900 * 10
    elif stars > 10:
        pop_score = 10 + (stars - 10) / 90 * 10
    else:
        pop_score = stars

    # Velocity / Maintenance (max 35 pts)
    vel_score = 0
    if not archived:
        if days_since_push is not None:
            if days_since_push <= 14:
                vel_score = 35
            elif days_since_push <= 60:
                vel_score = 28
            elif days_since_push <= 180:
                vel_score = 20
            elif days_since_push <= 365:
                vel_score = 10
            else:
                vel_score = 2
    else:
        vel_score = 0

    # License / Hygiene (max 25 pts)
    hygiene_score = 0
    if license_name not in ("None", "NOASSERTION", ""):
        hygiene_score += 15
    if description.strip():
        hygiene_score += 5
    if topics:
        hygiene_score += 5

    total_score = round(pop_score + vel_score + hygiene_score, 1)

    return {
        "repo": repo_full_name,
        "score": total_score,
        "stars": stars,
        "forks": forks,
        "open_issues": open_issues,
        "days_since_push": days_since_push,
        "archived": archived,
        "license": license_name,
        "description": description[:120] + ("..." if len(description) > 120 else ""),
        "url": data.get("html_url")
    }

def search_repos(query, limit=5):
    encoded_query = urllib.parse.quote(query)
    url = f"https://api.github.com/search/repositories?q={encoded_query}&sort=stars&order=desc&per_page={limit}"
    data = fetch_github_api(url)
    if not data or "items" not in data:
        return []
    
    results = []
    for item in data.get("items", [])[:limit]:
        eval_res = evaluate_repository(item["full_name"])
        if eval_res:
            results.append(eval_res)
    return results

def main():
    if len(sys.argv) < 2:
        print("Usage: evaluate_repo.py <owner/repo | search 'query'> [repo2 repo3 ...]")
        sys.exit(1)

    if sys.argv[1] == "search" and len(sys.argv) >= 3:
        query = " ".join(sys.argv[2:])
        results = search_repos(query, limit=6)
    else:
        repos = sys.argv[1:]
        results = []
        for r in repos:
            res = evaluate_repository(r.strip())
            if res:
                results.append(res)

    results.sort(key=lambda x: x["score"], reverse=True)

    print("| Rank | Repository | Quality Score | Stars | Forks | Last Push | License | Status |")
    print("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    for i, r in enumerate(results, start=1):
        status = "⚠️ Archived" if r["archived"] else "✅ Active"
        days = f"{r['days_since_push']}d ago" if r['days_since_push'] is not None else "N/A"
        print(f"| {i} | [{r['repo']}]({r['url']}) | **{r['score']}/100** | {r['stars']:,} | {r['forks']:,} | {days} | {r['license']} | {status} |")

    print("\n### Details & Descriptions")
    for r in results:
        print(f"* **[{r['repo']}]({r['url']})** ({r['score']}/100): {r['description']}")

if __name__ == "__main__":
    main()
