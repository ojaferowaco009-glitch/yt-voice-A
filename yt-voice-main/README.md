# yt-voice

Public, standalone voice-cloning service. Lives on its own GitHub account
(the Student Pack account) on purpose -- it is called by `yt-core`'s
`voice_clone_client.py` from an entirely separate account, and does nothing
except turn `(reference voice, script text)` into a finished `.wav`.

## How a job runs

1. `yt-core` POSTs a `repository_dispatch` (`event_type: voice_job`) to this
   repo with `{job_id, channel_id, script_text, dispatched_at}`.
2. `generate.yml`'s **plan** job reads that payload, fetches the channel's
   reference clip + transcript from the private `yt-core` repo, decides how
   many shards to use, and uploads `manifest.json` + `profile/` as
   artifacts.
3. **shard** (a matrix job, one run per shard) downloads those artifacts and
   synthesizes its slice of the text, with its own retry/backoff.
4. **collect** waits for every shard (`needs: [plan, shard]`, `if: always()`
   so it still runs even if some shards failed), verifies every expected
   shard index is present, stitches with ffmpeg, and commits
   `results/<job_id>/final.wav` + `results/<job_id>/status.json` back to
   this repo.
5. `yt-core` polls `results/<job_id>/status.json` via the Contents API,
   then downloads `final.wav` via `raw.githubusercontent.com`.

## Concurrency -- how "one finishes, the next starts" actually works

No custom scheduler or queue database was built for this, on purpose:
GitHub Actions already queues jobs past your account's concurrency ceiling
(40 on Student Pack) and starts each queued one the moment a slot frees,
first-requested-first-served. So if channel A's job dispatches 40 shards
and channel B's job arrives 2 seconds later needing more, GitHub itself
queues B's shard runs and starts them one-by-one as A's shards finish --
exactly the behavior you described, for free, with no extra code. Each
job's `collect` step only ever looks at ITS OWN `job_id`'s shard artifacts
(matched by exact name, never a wildcard that could cross jobs), so
interleaved execution across jobs never risks mixing one job's audio into
another's.

`MAX_SHARDS_PER_JOB` (default 20, see `plan_and_fetch.py`) caps how much of
the 40-slot pool ONE job can claim, so a single very long script can't
starve every other channel's job out of the pool entirely.

## Factory Pool -- deploying this repo more than once

One account's 40-slot Actions concurrency ceiling is a hard ceiling on
this repo alone. For a single very long script (an hour-long narration,
say) that wants more raw shard throughput than one account comfortably
gives, `yt-core` can be pointed at SEVERAL independent deployments of
this exact repo (different GitHub accounts, identical code -- nothing in
this repo needs to know it's one of several) and treat them as a pool:
it estimates the shard count a long script needs, and if that's more
than one factory's configured budget, splits the script across as many
factories as needed, dispatching each piece as its own ordinary job to a
different deployment of this repo. Each deployment plans/shards/collects
its own piece exactly as described above -- entirely unaware of the
others. `yt-core` downloads each piece's `final.wav` and concatenates
them back in original order.

To add a factory to the pool: repeat steps 1-3 below on a fresh GitHub
account (same repo, same setup), then add its `{repo, pat, max_shards}`
to `VOICE_FACTORIES_JSON` on the `yt-core`/`yt-runner` side instead of
(or in addition to) the single `VOICE_SERVICE_REPO`/
`VOICE_SERVICE_DISPATCH_PAT` pair -- see `voice_clone_client.py`'s
module docstring. `max_shards` is that factory's own configurable
budget; set it to whatever that account's Actions concurrency ceiling
comfortably supports (40 on Student Pack, less on a free/non-Student
account).

## GPU vs CPU -- please read before assuming timing

Standard GitHub-hosted Actions runners (what a public repo gets for free)
have **no GPU**. F5-TTS will run on CPU here, which is meaningfully slower
per shard than the GPU numbers you'll see quoted online. This mostly just
affects wall-clock latency, not cost -- **public repos get unlimited
Actions minutes on standard runners**, which is the whole reason this
service is a public repo in the first place. Still, please run the canary
(step 5 below) as your first real test and look at how long one shard
actually takes on CPU before assuming a specific end-to-end time for a
full video's narration.

## Setup

1. Create this repo on your Student Pack GitHub account, public.
2. Add repo secrets: `YT_CORE_READONLY_PAT` (a fine-grained PAT from your
   OTHER account, scoped to `yt-core`, **Contents: Read-only**, nothing
   else), and optionally `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` if you
   want canary/job alerts here too.
3. Add a repo **variable** `YT_CORE_REPO` (Settings -> Secrets and
   variables -> Actions -> Variables), value like `yourname/yt-core`.
4. On the `yt-core` side, generate `VOICE_SERVICE_DISPATCH_PAT` (a
   fine-grained PAT scoped to THIS repo, **Contents: Read & write** --
   write is needed because `dispatches` and reading `results/` both go
   through it) and set `VOICE_SERVICE_REPO` (e.g. `yourstudentaccount/yt-voice`)
   as an env var in `yt-runner`'s `video-pipeline.yml`. **Or**, to pool
   several deployments of this same repo together (see "Factory Pool"
   below), add this one's `{repo, pat, max_shards}` as an entry in the
   `VOICE_FACTORIES_JSON` secret instead -- see `voice_clone_client.py`'s
   module docstring in `yt-core`.
5. Record a real 8-12 second reference clip in your own voice (a full
   sentence, clean single take), write out its exact transcript, and drop
   both as `scripts/canary/profile/reference.wav` +
   `scripts/canary/profile/reference.json` (`{"transcript": "..."}`) so
   the daily canary has something to test against. Also upload the SAME
   (or a different) clip through the Dashboard's Voice Profile page for
   each real channel -- see `dashboard-additions/`.
6. Manually run the "Voice Service Canary" workflow once (Actions tab ->
   Run workflow) before wiring up a real channel, to confirm the whole
   chain works and to see how long a shard actually takes.

## Upgrading F5-TTS

`requirements.txt` installs straight from F5-TTS's GitHub `main` branch,
which is convenient but means an upstream change can silently alter
behavior or break `run_shard.py`'s CLI flags. Recommended: pin to a
specific commit/tag once things are working
(`git+https://github.com/SWivid/F5-TTS.git@<commit-or-tag>`), and only move
that pin forward deliberately -- the daily canary will tell you if a pin
bump broke anything before it touches a real channel's video.
