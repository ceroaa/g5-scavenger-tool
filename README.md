# OpenClearn

Safe, configurable cleanup tooling for AI agent workspaces (Codex / Claude Code / OpenClaw).

## Install

Install from GitHub with `pipx`:

```powershell
pipx install git+https://github.com/ceroaa/OpenClearn.git
openclearn --version
openclearn --init-config openclearn.config.json
openclearn --config openclearn.config.json --operation doctor
```

Install in a Python environment:

```powershell
python -m pip install git+https://github.com/ceroaa/OpenClearn.git
openclearn --version
```

## What Is New In v1.7

- installable command-line package via `pipx` / `pip`.
- `openclearn`: main cleanup command.
- `openclearn-system-scan`: system disk growth scan.
- `openclearn-chrome-cache-clean`: Chrome local AI cache cleanup helper.
- `--init-config PATH`: create a starter config after install.
- `--version`: print the installed OpenClearn version.
- `doctor` mode: read-only health check for config, root paths, approval file paths, trash safety, allow roots, and document scan roots.
- `collect` mode: collect cleanup candidates only, no deletion.
- `review` mode: collect candidates and write a Markdown review report.
- `delete` mode: delete only approved candidates from an approval file.
- `system_scan.py`: system-level disk growth scan (top folders + recent large files).
- `clean_chrome_ai_cache.py`: safe cleanup of Chrome local AI cache folders.
- `doc_cleanup`: document-quality scan for:
  - `garbled_document` candidates (mojibake/encoding-noise text files)
  - `exact_duplicate_document` candidates (hash-verified duplicates)
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

Read-only tool check:

```powershell
python scavenger.py --version
python scavenger.py --init-config openclearn.config.json
python scavenger.py --config config.example.json --operation doctor
```

System-level growth scan (last 24h):

```powershell
python system_scan.py
python system_scan.py --include-recent-large-files --recent-hours 24 --min-file-mb 200
```

Chrome local AI cache cleanup:

```powershell
python clean_chrome_ai_cache.py --kill-chrome
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

Current v1.3 keeps provider/key-loaded status in reports and adds a documented lockfile resolution playbook from production use.

v1.7 adds installable CLI commands and starter config generation so OpenClearn can be used like a standalone external tool.

## Case Notes

- 2026-04-13: Added real-world case note for staged destructive devour cleanup and locked-packfile handling.
- See: `docs/cases/2026-04-13-g5-virus-devour-case.md`

## Patrol

```powershell
python patrol.py --config config.example.json --mode balanced --cycles 0 --interval-seconds 1800 --auto-apply --apply-threshold-mb 256
```

## Windows Shortcuts

```bat
run_system_scan.bat
run_clean_chrome_ai_cache.bat
```

## Safety

- Start with `collect` or `review`.
- Keep `use_trash=true` to move files into trash first.
- Avoid `--hard-delete` unless you have backups.
- Keep critical areas in `collector_context.deny_roots` and `protected_files`.
