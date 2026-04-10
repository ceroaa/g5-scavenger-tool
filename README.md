# OpenClearn

Safe, configurable cleanup tooling for AI agent workspaces (Codex / Claude Code / OpenClaw).

## What Is New In v1.1

- `collect` mode: collect cleanup candidates only, no deletion.
- `review` mode: collect candidates and write a Markdown review report.
- `delete` mode: delete only approved candidates from an approval file.
- Agent profile loading (`codex` / `claude` / `openclaw` / custom JSON).
- API key binding support via environment variable (`--provider`, `--api-key-env`).
- Collector context policy:
  - allow/deny roots
  - deny patterns
  - protected files
  - cleaner persona + principles

## Modes

- `cleanup`: legacy structured cleanup (snapshots/specimens/trials/media dedupe).
- `collect`: candidate collection only.
- `review`: candidate collection + review markdown.
- `delete`: execute approved cleanup actions only.

## Quick Start

```powershell
cd tools\g5_scavenger
python scavenger.py --config config.example.json --mode safe --operation collect
```

Generate review report:

```powershell
python scavenger.py --config config.example.json --mode balanced --operation review
```

Apply approved deletions:

```powershell
python scavenger.py --config config.example.json --mode balanced --operation delete
```

Legacy cleanup pipeline:

```powershell
python scavenger.py --config config.example.json --mode balanced --operation cleanup --dry-run
```

## Approval File

Default path: `state/g5_scavenger_approve.json`

```json
{
  "approve_candidate_ids": [
    "dup-xxxx-xxxx",
    "stale-xxxx-xxxx"
  ],
  "approve_paths": [
    "C:/path/to/file.tmp"
  ]
}
```

`delete` mode only removes entries listed in this file.

## Agent Profiles

Use built-ins:

```powershell
python scavenger.py --config config.example.json --agent-profile openclaw --operation review
```

Use custom profile JSON:

```powershell
python scavenger.py --config config.example.json --agent-profile custom --agent-profile-file .\my_profile.json --operation collect
```

## API Key Binding

```powershell
$env:OPENAI_API_KEY = "sk-..."
python scavenger.py --config config.example.json --provider openai --api-key-env OPENAI_API_KEY --operation review
```

Current v1.1 records provider/key-loaded status in reports for future semantic cleanup extensions.

## Patrol

```powershell
python patrol.py --config config.example.json --mode balanced --cycles 0 --interval-seconds 1800 --auto-apply --apply-threshold-mb 256
```

## Safety

- Start with `collect` or `review`.
- Keep `use_trash=true` to move files into trash first.
- Avoid `--hard-delete` unless you have backups.
- Keep critical areas in `collector_context.deny_roots` and `protected_files`.
