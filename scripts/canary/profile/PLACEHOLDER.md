Drop your canary test voice here (kept separate from real channel profiles
on purpose, so a canary failure never touches production data):

- `reference.wav` -- any short (8-12s), clean, single-take clip, already
  a real .wav (the canary calls run_shard.py directly and skips the
  plan_and_fetch.py fetch/convert step real channel jobs go through, so
  this one file needs to already be in the format F5-TTS expects)
- `reference.json` -- `{"transcript": "...exact words spoken..."}`

Until both files exist, canary.yml's "Report result" step sends a
"skipped, no fixture yet" notice instead of failing -- so the empty
scheduled workflow won't spam you with false alarms before setup is done.
