# Project instructions

- Active work is the `pulse/` package. Read `pulse/MAP.md` first: it maps every component, its API, the state
  files and how to run things. Don't re-read the codebase to rediscover this.
- `argus/` is a trimmed library only (warehouse, metric registry, metric engine, metric graph, statistics). It
  contains just the modules Pulse imports; don't bring the old Argus agents, monitor or dashboard back.
  `.argus/` holds 4 learned-graph files that `argus` reads on load; Pulse's results depend on them, keep them.
- Python: `.venv/Scripts/python.exe`. Tests: `.venv/Scripts/python.exe -m pytest tests/pulse -q`.
- Claude API: only via `pulse/llm.py`, mock mode by default. Live runs (`PULSE_LLM=live`) are capped and done
  deliberately. Don't run `python -m marketplace.*` casually: it rewrites `data/eye/` (use `--out` to test).
- Don't add features beyond the hackathon brief without asking the user. Keep code simple.
- This folder is the official submission repo: only needed files, no scratch notes or generated reports. When a
  change affects how Pulse is run, what it does, or what the UI shows, update `README.md` in the same change.
