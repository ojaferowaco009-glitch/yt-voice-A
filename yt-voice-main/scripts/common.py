"""Shared helpers for the voice-generation service's workflow scripts.

Kept dependency-light (requests + stdlib only) since `plan_and_fetch.py`
needs to run and fetch the reference profile BEFORE the (much heavier)
F5-TTS stack is even installed.
"""
from __future__ import annotations
import base64
import json
import random
import re
import subprocess
import time
from pathlib import Path
import requests

API = "https://api.github.com"


def gh_headers(token: str | None) -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def raw_fetch(repo: str, path: str, token: str | None = None, branch: str = "main", timeout: int = 60) -> bytes | None:
    """GET a file's raw bytes via raw.githubusercontent.com. This works for
    PRIVATE repos too when a Bearer token is supplied -- GitHub honors the
    Authorization header on raw.githubusercontent.com the same way most
    Git hosts do (documented independently at
    https://docs.deno.com/runtime/reference/private_repositories/, which
    describes exactly this pattern). Returns None on 404 instead of
    raising, since "not there yet" is routine for a caller that's polling.

    NOT used for tight polling loops (see contents_api_json below) --
    raw.githubusercontent.com sits behind a CDN with a several-minute
    cache, so a 404 seen once can keep being served stale for a while
    after the file actually appears.
    """
    url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
    res = requests.get(url, headers=gh_headers(token), timeout=timeout)
    if res.status_code == 404:
        return None
    res.raise_for_status()
    return res.content


def contents_api_json(repo: str, path: str, token: str, timeout: int = 30) -> dict | None:
    """GET a small JSON file via the live Contents API (not CDN-cached --
    safe to poll repeatedly, unlike raw_fetch above). Returns None on 404."""
    res = requests.get(f"{API}/repos/{repo}/contents/{path}", headers=gh_headers(token), timeout=timeout)
    if res.status_code == 404:
        return None
    res.raise_for_status()
    return json.loads(base64.b64decode(res.json()["content"]).decode("utf-8"))


def git_commit_push(paths: list[str], message: str, repo_dir: str = ".", max_attempts: int = 5) -> None:
    """Same rebase-and-retry loop yt-runner's video-pipeline.yml already
    uses to commit data/ back to yt-core -- reused here so a collector job
    racing another job's collector (two jobs finishing within seconds of
    each other) resolves the same way that codebase already trusts.

    Only the COLLECT job in generate.yml calls this (once per job_id) --
    the 40-way shard jobs never touch git, they hand off results via
    upload-artifact/download-artifact instead, which sidesteps a much
    worse 40-way write race entirely.

    Assumes actions/checkout's default credentials (persist-credentials,
    on by default) already have push rights to THIS repo -- true for the
    collect job because it's committing back to its own checked-out repo,
    not a different one, so no custom PAT is needed here.
    """
    subprocess.run(["git", "add", *paths], cwd=repo_dir, check=True)
    commit = subprocess.run(["git", "commit", "-m", message], cwd=repo_dir)
    if commit.returncode != 0:
        print("Nothing to commit (paths unchanged) -- treating as success.")
        return
    for attempt in range(1, max_attempts + 1):
        pull = subprocess.run(["git", "pull", "--rebase"], cwd=repo_dir)
        push = subprocess.run(["git", "push"], cwd=repo_dir) if pull.returncode == 0 else None
        if push and push.returncode == 0:
            return
        print(f"Push conflict (attempt {attempt}/{max_attempts}); retrying...")
        subprocess.run(["git", "rebase", "--abort"], cwd=repo_dir)
        time.sleep(random.randint(5, 15))
    raise RuntimeError(f"Unable to push after {max_attempts} attempts.")


def split_balanced(text: str, n: int) -> list[str]:
    """Split `text` into exactly `n` chunks, each ending on a sentence
    boundary, as close to equal length as possible.

    Unlike yt-core's voice.py `_split_text` (which packs greedily up to a
    max-chars limit and lets the chunk COUNT fall out of that), this fixes
    the chunk COUNT first -- that count IS the shard count the dispatcher
    already decided on (driven by the 40-slot pool / target seconds per
    shard), not a per-chunk character limit.

    The target chunk size is RECOMPUTED after every chunk closes, from
    whatever text and chunk-slots remain -- not fixed once from the whole
    text up front. A fixed target (len(text) / n, computed once) lets a
    run of sentences shorter than that target close out most buckets
    early, leaving nothing to absorb any sentences shorter than the
    original target that show up later -- for a script whose sentence
    count exceeds n (routine for narration text), every remaining
    sentence past the (n-1)th bucket then has nowhere to go but the very
    last chunk, unconditionally, however many there are. Recomputing the
    target against the shrinking remainder keeps each closed bucket sized
    to what's actually left, so a late run of short sentences gets spread
    across the remaining buckets instead of all landing in the last one.
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
    if not sentences:
        return [text] + [""] * (n - 1)
    if n <= 1:
        return [text]

    chunks: list[str] = []
    remaining = list(sentences)
    for i in range(n - 1):
        chunks_left = n - i
        remaining_len = sum(len(s) for s in remaining) + max(0, len(remaining) - 1)
        target = remaining_len / chunks_left
        current = ""
        while remaining:
            candidate = f"{current} {remaining[0]}".strip()
            if current and len(candidate) >= target:
                break
            current = candidate
            remaining.pop(0)
        if not current and remaining:
            # A single sentence alone already meets/exceeds target -- take
            # it anyway rather than leaving this bucket empty.
            current = remaining.pop(0)
        chunks.append(current)
    chunks.append(" ".join(remaining))
    # Rare edge case: very few/long sentences under-fill the requested
    # count -- pad with empty chunks rather than silently handing back
    # fewer shards than the matrix strategy was already built for.
    while len(chunks) < n:
        chunks.append("")
    return chunks[:n]
