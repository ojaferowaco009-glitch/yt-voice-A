"""Runs once per job, as generate.yml's `plan` job.

Reads the repository_dispatch payload straight from $GITHUB_EVENT_PATH
(rather than piping user-controlled script_text through workflow YAML
inputs/shell args -- avoids quoting/escaping issues with long, multi-line,
Bengali/English narration text), fetches that channel's reference voice
profile from the private yt-core repo, decides how many shards to use, and
writes:

  manifest.json         -- job_id, channel_id, total_shards, per-shard text
  profile/reference.wav -- always a clean, real WAV, regardless of what
                            format the channel's profile was uploaded in
  profile/reference.json

...which the `shard` and `collect` jobs then pick up as artifacts. Also
writes two GITHUB_OUTPUT values (job_id, shards) so generate.yml's matrix
strategy can fan out.
"""
from __future__ import annotations
import json
import math
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

from common import raw_fetch, split_balanced

# Student Pack concurrency ceiling is 40 (see repo README) -- a single job
# is capped well under that so it never ALONE exhausts the whole account's
# pool. A second job arriving mid-run still gets native GitHub Actions
# queuing (see README "Concurrency" section) instead of starving. This is
# a SOFT cap now -- see the shard-count calculation in main(): a script
# long enough that even MAX_SHARDS_PER_JOB shards would each break
# F5-TTS's 30s-total limit uses more than this anyway, because a job that
# respects the pool-sharing cap but fails to generate valid audio helps
# no one.
MAX_SHARDS_PER_JOB = int(os.environ.get("MAX_SHARDS_PER_JOB", "20"))
# Floor on how much content a single shard may cover -- keeps a short
# script from being sliced into single-word fragments that would sound
# choppy once stitched back together. A short script simply ends up with
# fewer shards; it never goes below this regardless of MAX_SHARDS_PER_JOB.
MIN_SECONDS_PER_SHARD = float(os.environ.get("MIN_SECONDS_PER_SHARD", "6"))
# Rough narration rate, only used to size shards -- doesn't need to be
# exact, just consistent enough that shards come out similarly sized.
WORDS_PER_SECOND = 2.5


def estimate_seconds(text: str) -> float:
    words = len(text.split())
    return words / WORDS_PER_SECOND if words else 1.0


def fetch_and_normalize_reference(yt_core_repo: str, yt_core_token: str, channel_id: str, profile_dir: Path) -> tuple[dict, float]:
    """reference.json is fetched FIRST because it names the actual audio
    file (see "audio_file" below) -- phone recordings are almost never
    real .wav (usually .m4a/.aac/.3gp from the Dashboard's upload page),
    so this never assumes a fixed filename or format. Whatever comes back
    is re-encoded through ffmpeg into a clean, known-good reference.wav --
    that sidesteps ever needing to know whether F5-TTS's own audio loader
    can read the original phone format directly.

    Also measures and returns the converted clip's actual duration -- F5-TTS
    caps a single generation call at 30s TOTAL, prompt (reference) audio
    included, not 30s of output alone (confirmed against the upstream
    README). main() uses this real, measured number -- not an assumed
    8-12s -- to keep shards safely under that ceiling regardless of how
    long any given channel's uploaded clip actually turned out to be.
    """
    ref_json = raw_fetch(yt_core_repo, f"data/voice_profiles/{channel_id}/reference.json", token=yt_core_token)
    if ref_json is None:
        print(f"::error::No voice profile found for channel '{channel_id}' in {yt_core_repo} "
              f"(expected data/voice_profiles/{channel_id}/reference.json).")
        sys.exit(1)
    reference_meta = json.loads(ref_json.decode("utf-8"))
    # "audio_file" is set by the Dashboard's upload route; default here
    # only covers a profile placed by hand before that field existed.
    audio_file = reference_meta.get("audio_file", "reference.wav")

    raw_audio = raw_fetch(yt_core_repo, f"data/voice_profiles/{channel_id}/{audio_file}", token=yt_core_token)
    if raw_audio is None:
        print(f"::error::reference.json for '{channel_id}' points at '{audio_file}' but that file is missing.")
        sys.exit(1)

    profile_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(audio_file).suffix or ".bin"
    original_path = profile_dir / f"original{suffix}"
    original_path.write_bytes(raw_audio)
    (profile_dir / "reference.json").write_bytes(ref_json)

    ref_wav = profile_dir / "reference.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(original_path), "-ar", "24000", "-ac", "1", str(ref_wav)],
        check=True, capture_output=True, text=True,
    )
    original_path.unlink()

    with wave.open(str(ref_wav), "rb") as wf:
        ref_duration = wf.getnframes() / float(wf.getframerate())

    return reference_meta, ref_duration


def main() -> None:
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    event_name = os.environ.get("GITHUB_EVENT_NAME", "repository_dispatch")

    if event_name == "workflow_dispatch":
        # Manual test trigger (Actions tab -> Generate Voice -> Run
        # workflow) -- lets you test one channel's voice generation
        # directly without running the whole main pipeline first. GitHub's
        # manual-dispatch inputs live under event["inputs"], not
        # event["client_payload"] -- everything past this branch treats
        # the two the same way.
        raw = event.get("inputs", {})
        job_id = str(raw.get("job_id") or f"test-{int(time.time())}")
        dispatched_at = time.time()
    else:
        raw = event["client_payload"]
        job_id = str(raw["job_id"])
        dispatched_at = raw.get("dispatched_at", time.time())

    channel_id = str(raw["channel_id"])
    script_text = str(raw["script_text"])

    yt_core_repo = os.environ["YT_CORE_REPO"]
    yt_core_token = os.environ["YT_CORE_READONLY_PAT"]

    profile_dir = Path("profile")
    reference_meta, ref_duration = fetch_and_normalize_reference(yt_core_repo, yt_core_token, channel_id, profile_dir)

    # F5-TTS's hard ceiling is 30s TOTAL per single generation call --
    # reference audio counts against that, not just the output (confirmed
    # against the upstream README). Measured ref_duration (not an assumed
    # 8-12s) is what actually determines how much headroom is left; a
    # 2s safety margin avoids the truncation risk the docs warn about
    # right at the boundary.
    # F5-TTS's hard ceiling is 30s TOTAL per single generation call --
    # reference audio counts against that, not just the output (confirmed
    # against the upstream README). Measured ref_duration (not an assumed
    # 8-12s) is what actually determines how much headroom is left; a
    # 2s safety margin avoids the truncation risk the docs warn about
    # right at the boundary.
    F5TTS_TOTAL_CAP_SECONDS = 30.0
    SAFETY_MARGIN_SECONDS = 2.0
    safe_cap = max(5.0, F5TTS_TOTAL_CAP_SECONDS - ref_duration - SAFETY_MARGIN_SECONDS)

    seconds = estimate_seconds(script_text)

    # Always split as finely as the pool/config usefully allows -- for
    # speed, since shards run in parallel and compute itself is free --
    # subject to two bounds, in this priority order:
    #
    #  1. FLOOR (hard, correctness): no single shard's audio may exceed
    #     safe_cap, or F5-TTS can fail/truncate that call outright. This
    #     can push shard_count ABOVE MAX_SHARDS_PER_JOB for a long enough
    #     script -- deliberately: a job that stays under the
    #     pool-sharing cap but produces broken audio helps no one.
    #  2. CEILING (soft, quality + pool sharing): never split finer than
    #     MIN_SECONDS_PER_SHARD per shard (avoids single-word fragments
    #     sounding choppy once stitched), and never claim more than
    #     MAX_SHARDS_PER_JOB of the shared pool unless #1 forces it.
    #
    # A short script naturally lands on fewer shards through bound #2
    # alone; a long one is spread as wide as MAX_SHARDS_PER_JOB allows,
    # or wider if #1 requires it.
    min_shards_needed = max(1, math.ceil(seconds / safe_cap))
    max_shards_useful = max(1, math.floor(seconds / MIN_SECONDS_PER_SHARD))
    shard_count = max(min_shards_needed, min(MAX_SHARDS_PER_JOB, max_shards_useful))
    if shard_count > MAX_SHARDS_PER_JOB:
        print(f"::warning::This script needs {shard_count} shards to keep each one under "
              f"F5-TTS's {safe_cap:.0f}s-per-call limit, above MAX_SHARDS_PER_JOB="
              f"{MAX_SHARDS_PER_JOB} -- using {shard_count} anyway (correctness over the "
              f"pool-sharing cap).")

    texts = split_balanced(script_text, shard_count)

    manifest = {
        "job_id": job_id,
        "channel_id": channel_id,
        "total_shards": shard_count,
        "reference_text": reference_meta.get("transcript", ""),
        "shards": [{"index": i, "text": t} for i, t in enumerate(texts)],
        "dispatched_at": dispatched_at,
    }
    Path("manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
        fh.write(f"job_id={job_id}\n")
        fh.write(f"channel_id={channel_id}\n")
        fh.write(f"dispatched_at={dispatched_at}\n")
        fh.write(f"shards={json.dumps(list(range(shard_count)))}\n")

    print(f"Planned {shard_count} shard(s) for job {job_id} (channel={channel_id}, ~{seconds:.0f}s estimated text, "
          f"ref_duration={ref_duration:.1f}s, safe_cap={safe_cap:.1f}s/shard, "
          f"min_shards_needed={min_shards_needed}, max_shards_useful={max_shards_useful}).")


if __name__ == "__main__":
    main()
