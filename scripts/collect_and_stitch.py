"""Runs once per job, after every shard matrix entry has finished (success
or failure -- generate.yml's collect job uses `if: always()` so a partial
failure still gets reported instead of the workflow just stopping
silently).

This is also the "did the script actually split correctly" check: it
verifies every expected shard index (0..total_shards-1) produced either
real audio or a deliberate EMPTY marker, using the JOB'S OWN manifest and
job_id to build exact expected artifact paths -- never a wildcard glob --
so a mixup with another job's shards would surface as a "missing shard"
rather than silently splicing in the wrong audio.
"""
from __future__ import annotations
import argparse
import json
import subprocess
import time
from pathlib import Path

from common import git_commit_push


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--shards-root", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--channel-id", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--started-at", required=True,
                         help="Unix timestamp the job was dispatched at (client_payload.dispatched_at), for a generation-time metric")
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    total = manifest["total_shards"]
    shards_root = Path(args.shards_root)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    found: dict[int, Path] = {}
    empty: set[int] = set()
    missing: list[int] = []
    for i in range(total):
        # Deterministic path, matching exactly how generate.yml names each
        # shard's artifact (job-<job_id>-shard-<i>) -- see note above on
        # why this is never a glob.
        shard_dir = shards_root / f"job-{args.job_id}-shard-{i}"
        wav_path = shard_dir / f"shard_{i:03d}.wav"
        empty_marker = shard_dir / "EMPTY"
        if wav_path.exists():
            found[i] = wav_path
        elif empty_marker.exists():
            empty.add(i)
        else:
            missing.append(i)

    def write_status(status: dict) -> None:
        (out_dir / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")

    if missing:
        write_status({
            "job_id": args.job_id, "channel_id": args.channel_id, "status": "partial_failed",
            "total_shards": total, "failed_shards": missing,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        git_commit_push([str(out_dir)], f"voice job {args.job_id}: partial failure ({len(missing)} shard(s))")
        print(f"::error::Job {args.job_id} missing shards: {missing}")
        return

    if not found:
        # every shard was an EMPTY pad chunk -- shouldn't normally happen
        # (plan_and_fetch only over-pads, never under-fills real text),
        # but fail loudly rather than "succeeding" with a silent, empty
        # result.
        write_status({
            "job_id": args.job_id, "channel_id": args.channel_id, "status": "partial_failed",
            "total_shards": total, "failed_shards": [], "reason": "no non-empty shards produced",
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        git_commit_push([str(out_dir)], f"voice job {args.job_id}: empty result")
        return

    concat_list = out_dir / "concat.txt"
    concat_list.write_text("".join(f"file '{found[i].resolve()}'\n" for i in sorted(found)), encoding="utf-8")
    final_wav = out_dir / "final.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(final_wav)],
        check=True, capture_output=True,
    )
    concat_list.unlink()

    elapsed = time.time() - float(args.started_at)
    write_status({
        "job_id": args.job_id, "channel_id": args.channel_id, "status": "success",
        "total_shards": total, "empty_shards": sorted(empty), "generation_seconds": round(elapsed, 1),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    git_commit_push([str(out_dir)], f"voice job {args.job_id}: complete")
    print(f"Job {args.job_id} complete in {elapsed:.0f}s.")


if __name__ == "__main__":
    main()
