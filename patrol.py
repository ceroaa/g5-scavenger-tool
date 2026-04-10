from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(TZ).isoformat()


def load_json(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return default or {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default or {}


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def run_scavenger(base: Path, config_path: Path, mode: str, operation: str, dry_run: bool) -> tuple[int, dict]:
    script = (base / "scavenger.py").resolve()
    cmd = [sys.executable, str(script), "--config", str(config_path), "--mode", mode, "--operation", operation]
    if dry_run:
        cmd.append("--dry-run")
    cp = subprocess.run(
        cmd,
        cwd=str(base),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = cp.stdout.strip().splitlines()[-1] if cp.stdout.strip() else ""
    payload: dict = {}
    try:
        payload = json.loads(out)
    except Exception:
        payload = {"raw": out, "stderr": cp.stderr.strip()}
    return cp.returncode, payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--mode", choices=["safe", "balanced", "aggressive"], default="balanced")
    parser.add_argument("--operation", choices=["cleanup", "collect", "review"], default="cleanup")
    parser.add_argument("--interval-seconds", type=int, default=1800)
    parser.add_argument("--cycles", type=int, default=1, help="0 means run forever")
    parser.add_argument("--auto-apply", action="store_true")
    parser.add_argument("--apply-threshold-mb", type=int, default=256)
    parser.add_argument("--log-file", default="patrol_reports.jsonl")
    args = parser.parse_args()

    base = Path(__file__).resolve().parent
    cfg_input = Path(args.config)
    if cfg_input.is_absolute():
        config_path = cfg_input
    elif cfg_input.exists():
        config_path = cfg_input.resolve()
    else:
        config_path = (base / cfg_input).resolve()
    log_path = (base / args.log_file).resolve() if not Path(args.log_file).is_absolute() else Path(args.log_file)

    index = 0
    while True:
        index += 1
        rc, dry = run_scavenger(base=base, config_path=config_path, mode=args.mode, operation=args.operation, dry_run=True)
        metrics = dry.get("metrics", {}) if isinstance(dry, dict) else {}
        reclaim_mb = round(float(metrics.get("estimated_reclaim_bytes", 0)) / 1024 / 1024, 2)
        should_apply = bool(args.auto_apply and reclaim_mb >= float(args.apply_threshold_mb))

        applied = None
        if should_apply and rc == 0:
            arc, applied_payload = run_scavenger(
                base=base,
                config_path=config_path,
                mode=args.mode,
                operation=args.operation,
                dry_run=False,
            )
            applied = {"returncode": arc, "payload": applied_payload}

        event = {
            "timestamp": now_iso(),
            "runner": "patrol.py",
            "cycle_index": index,
            "mode": args.mode,
            "operation": args.operation,
            "dry_run_returncode": rc,
            "estimated_reclaim_mb": reclaim_mb,
            "auto_apply": args.auto_apply,
            "apply_threshold_mb": args.apply_threshold_mb,
            "should_apply": should_apply,
            "dry_run_payload": dry,
            "applied": applied,
        }
        append_jsonl(log_path, event)
        print(json.dumps(event, ensure_ascii=False))

        if args.cycles > 0 and index >= args.cycles:
            break
        time.sleep(max(1, args.interval_seconds))


if __name__ == "__main__":
    main()
