from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


TZ = timezone(timedelta(hours=8))
MODE_DEFAULTS = {
    "safe": {
        "stale_days": 14,
        "trial_timeout_hours": 240,
        "max_rollbacks": 20,
        "media_enabled": False,
        "media_delete_duplicates": False,
    },
    "balanced": {
        "stale_days": 7,
        "trial_timeout_hours": 168,
        "max_rollbacks": 50,
        "media_enabled": True,
        "media_delete_duplicates": False,
    },
    "aggressive": {
        "stale_days": 3,
        "trial_timeout_hours": 96,
        "max_rollbacks": 200,
        "media_enabled": True,
        "media_delete_duplicates": True,
    },
}
DEFAULT_MEDIA_EXTENSIONS = [
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".webm",
]
DEFAULT_COLLECTOR_PATTERNS = [
    "*.tmp",
    "*.temp",
    "*.bak",
    "*.old",
    "*.log",
    "*.cache",
]
BUILTIN_AGENT_PROFILES = {
    "codex": {
        "persona": "pragmatic_cleaner",
        "extra_protect_keywords": ["OPENSPACE-", "DO_NOT_TOUCH"],
        "collector_exclude_patterns": [".git/*", ".venv/*", "node_modules/*", "__pycache__/*"],
    },
    "claude": {
        "persona": "careful_archivist",
        "extra_protect_keywords": ["OPENSPACE-", "ANTHROPIC", "DO_NOT_TOUCH"],
        "collector_exclude_patterns": [".git/*", ".venv/*", "node_modules/*", "__pycache__/*"],
    },
    "openclaw": {
        "persona": "city_sanitation_guard",
        "extra_protect_keywords": ["OPENSPACE-", "XIAOYU-ANCHOR", "DO_NOT_TOUCH"],
        "collector_exclude_patterns": [".git/*", ".venv/*", "node_modules/*", "__pycache__/*"],
    },
}


def now_dt() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now_dt().isoformat()


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def load_json(path: Path, default: dict | None = None) -> dict:
    if not path.exists():
        return default or {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default or {}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def estimate_json_rows_bytes(rows: list[dict]) -> int:
    total = 0
    for row in rows:
        try:
            total += len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
        except Exception:
            continue
    return total


def choose_better_snapshot(a: dict, b: dict) -> dict:
    a_key = (
        int(a.get("refresh_count", 0)),
        int(a.get("strength_score", 0)),
        str(a.get("last_refreshed_at", "")),
    )
    b_key = (
        int(b.get("refresh_count", 0)),
        int(b.get("strength_score", 0)),
        str(b.get("last_refreshed_at", "")),
    )
    return a if a_key >= b_key else b


def dedupe_snapshots(snapshots: list[dict]) -> tuple[list[dict], list[dict]]:
    by_key: dict[tuple[str, str], dict] = {}
    removed: list[dict] = []
    for row in snapshots:
        key = (str(row.get("line_id", "")), str(row.get("capability_signature", "")))
        existing = by_key.get(key)
        if not existing:
            by_key[key] = row
            continue
        keep = choose_better_snapshot(existing, row)
        drop = row if keep is existing else existing
        by_key[key] = keep
        removed.append(drop)
    return list(by_key.values()), removed


def reap_misc_residue(snapshots: list[dict], stale_days: int) -> tuple[list[dict], list[dict]]:
    cutoff = now_dt() - timedelta(days=stale_days)
    kept: list[dict] = []
    removed: list[dict] = []
    for row in snapshots:
        if row.get("line_id") != "misc_line":
            kept.append(row)
            continue
        refreshed = parse_ts(str(row.get("last_refreshed_at", "")))
        refresh_count = int(row.get("refresh_count", 0))
        stale = refreshed is None or refreshed < cutoff
        if stale and refresh_count <= 1:
            removed.append(row)
        else:
            kept.append(row)
    return kept, removed


def dedupe_external_samples(samples: list[dict]) -> tuple[list[dict], list[dict]]:
    by_id: dict[str, dict] = {}
    removed: list[dict] = []
    for row in samples:
        sid = str(row.get("sample_id", ""))
        if sid not in by_id:
            by_id[sid] = row
            continue
        prev = by_id[sid]
        prev_ts = str(prev.get("discovered_at", ""))
        cur_ts = str(row.get("discovered_at", ""))
        if cur_ts > prev_ts:
            by_id[sid] = row
            removed.append(prev)
        else:
            if len(str(row.get("tool_purpose", ""))) > len(str(prev.get("tool_purpose", ""))):
                by_id[sid] = row
                removed.append(prev)
            else:
                removed.append(row)
    merged = list(by_id.values())
    merged.sort(key=lambda x: str(x.get("sample_id", "")))
    return merged, removed


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def scan_media_duplicates(
    roots: list[Path],
    extensions: list[str],
    min_size_bytes: int,
    keep_strategy: str,
) -> tuple[list[dict], int]:
    ext_set = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in extensions}
    by_size: dict[int, list[Path]] = {}
    for root in roots:
        if not root.exists():
            continue
        for dirpath, _, filenames in os.walk(root):
            for name in filenames:
                p = Path(dirpath) / name
                if p.suffix.lower() not in ext_set:
                    continue
                try:
                    size = p.stat().st_size
                except Exception:
                    continue
                if size < min_size_bytes:
                    continue
                by_size.setdefault(size, []).append(p)

    groups: list[dict] = []
    reclaimable = 0
    for size, files in by_size.items():
        if len(files) < 2:
            continue
        by_hash: dict[str, list[Path]] = {}
        for p in files:
            try:
                h = hash_file(p)
            except Exception:
                continue
            by_hash.setdefault(h, []).append(p)
        for h, same in by_hash.items():
            if len(same) < 2:
                continue
            ordered = sorted(same, key=lambda x: x.stat().st_mtime)
            if keep_strategy == "newest":
                ordered = list(reversed(ordered))
            keep = ordered[0]
            delete = ordered[1:]
            reclaim = size * len(delete)
            reclaimable += reclaim
            groups.append(
                {
                    "hash": h,
                    "size_bytes": size,
                    "keep": str(keep),
                    "delete": [str(x) for x in delete],
                    "count": len(same),
                    "reclaimable_bytes": reclaim,
                }
            )
    groups.sort(key=lambda g: int(g.get("reclaimable_bytes", 0)), reverse=True)
    return groups, reclaimable


def delete_media_duplicates(groups: list[dict]) -> tuple[int, int]:
    deleted_files = 0
    deleted_bytes = 0
    for g in groups:
        size = int(g.get("size_bytes", 0))
        for raw in g.get("delete", []):
            p = Path(str(raw))
            if not p.exists():
                continue
            try:
                p.unlink()
                deleted_files += 1
                deleted_bytes += size
            except Exception:
                continue
    return deleted_files, deleted_bytes


def rollback_stale_trials_guarded(
    records: list[dict],
    trial_timeout_hours: int,
    max_rollbacks: int,
    protect_keywords: list[str],
) -> tuple[list[dict], list[dict]]:
    changed: list[dict] = []
    now = now_dt()
    cutoff = now - timedelta(hours=trial_timeout_hours)

    for row in records:
        if len(changed) >= max_rollbacks:
            break
        if str(row.get("status", "")) != "active_trial":
            continue

        marker = " ".join(
            [
                str(row.get("adjustment_id", "")),
                str(row.get("source_round", "")),
                str(row.get("reason", "")),
            ]
        ).lower()
        if any(k.lower() in marker for k in protect_keywords):
            continue

        trial_end = parse_ts(str(row.get("trial_end", "")))
        ts = parse_ts(str(row.get("timestamp", "")))
        stale = False
        if trial_end:
            stale = trial_end <= now
        elif ts:
            stale = ts <= cutoff
        if not stale:
            continue

        row["status"] = "rolled_back"
        row["evaluation_result"] = "scavenger_timeout_rollback"
        row["rollback_reason"] = "scavenger_stale_active_trial_timeout"
        row["updated_at"] = now_iso()
        changed.append(
            {
                "adjustment_id": row.get("adjustment_id"),
                "source_round": row.get("source_round"),
                "previous_status": "active_trial",
                "new_status": "rolled_back",
            }
        )
    return records, changed


def pick_value(cli_value: int | None, mode_value: int, config_value: int | None) -> int:
    if cli_value is not None:
        return cli_value
    if config_value is not None:
        return config_value
    return mode_value


def resolve_path(root: Path, raw: str) -> Path:
    if str(raw).startswith(("C:", "D:", "/", "\\")):
        return Path(str(raw)).resolve()
    return (root / str(raw)).resolve()


def safe_relative(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return str(path.resolve())


def is_subpath(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def load_agent_profile(profile_name: str, profile_path: Path | None) -> dict:
    profile = dict(BUILTIN_AGENT_PROFILES.get(profile_name.lower(), {}))
    if profile_path and profile_path.exists():
        external = load_json(profile_path, {})
        if isinstance(external, dict):
            profile.update(external)
    return profile


def load_api_binding(config: dict, cli_provider: str | None, cli_key_env: str | None) -> dict:
    llm_cfg = config.get("llm_binding", {}) if isinstance(config, dict) else {}
    provider = str(cli_provider or llm_cfg.get("provider", "none")).lower()
    key_env = str(cli_key_env or llm_cfg.get("api_key_env", "")).strip()
    key_present = bool(key_env and os.getenv(key_env))
    return {
        "provider": provider,
        "api_key_env": key_env or None,
        "api_key_loaded": key_present,
    }


def load_collector_context(config: dict, root: Path) -> dict:
    raw = config.get("collector_context", {}) if isinstance(config, dict) else {}
    allow_roots = [resolve_path(root, str(x)) for x in raw.get("allow_roots", ["scratch", "audit", "state", "trash"])]
    deny_roots = [resolve_path(root, str(x)) for x in raw.get("deny_roots", [".git", "protocols", "residents", "memory_store"])]
    deny_patterns = [str(x) for x in raw.get("deny_patterns", ["*.key", "*.pem", "*.env", "*anchor*", "*identity*"])]
    protected_files = [str(resolve_path(root, str(x))) for x in raw.get("protected_files", [])]
    persona = str(raw.get("persona", "cleaner"))
    principles = [str(x) for x in raw.get("principles", ["collect_first", "review_before_delete", "protect_core_memory"])]
    return {
        "allow_roots": allow_roots,
        "deny_roots": deny_roots,
        "deny_patterns": deny_patterns,
        "protected_files": protected_files,
        "persona": persona,
        "principles": principles,
    }


def context_allows_file(path: Path, root: Path, context: dict) -> tuple[bool, str]:
    rp = path.resolve()
    rel = safe_relative(rp, root).replace("\\", "/")
    if str(rp) in set(context.get("protected_files", [])):
        return False, "protected_file"
    for denied in context.get("deny_roots", []):
        if is_subpath(rp, denied):
            return False, f"deny_root:{safe_relative(denied, root)}"
    if context.get("allow_roots"):
        if not any(is_subpath(rp, ar) for ar in context.get("allow_roots", [])):
            return False, "outside_allow_roots"
    for pattern in context.get("deny_patterns", []):
        p = str(pattern).replace("\\", "/")
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(path.name.lower(), str(pattern).lower()):
            return False, f"deny_pattern:{pattern}"
    return True, "allowed"


def should_exclude_path(rel_path: str, exclude_patterns: list[str]) -> bool:
    rel = rel_path.replace("\\", "/")
    for pattern in exclude_patterns:
        p = str(pattern).replace("\\", "/")
        if fnmatch.fnmatch(rel, p):
            return True
    return False


def scan_stale_files(
    root: Path,
    collector_cfg: dict,
    exclude_patterns: list[str],
    context: dict,
) -> list[dict]:
    candidates: list[dict] = []
    stale_days = int(collector_cfg.get("stale_days", 21))
    include_patterns = list(collector_cfg.get("include_patterns", DEFAULT_COLLECTOR_PATTERNS))
    roots = [resolve_path(root, str(x)) for x in collector_cfg.get("roots", ["scratch", "audit", "state"])]
    cutoff = now_dt() - timedelta(days=stale_days)

    for scan_root in roots:
        if not scan_root.exists():
            continue
        for dirpath, _, filenames in os.walk(scan_root):
            for name in filenames:
                file_path = Path(dirpath) / name
                rel = safe_relative(file_path, root)
                if should_exclude_path(rel, exclude_patterns):
                    continue
                allowed, reason = context_allows_file(file_path, root, context)
                if not allowed:
                    continue
                if include_patterns and not any(fnmatch.fnmatch(name.lower(), p.lower()) for p in include_patterns):
                    continue
                try:
                    st = file_path.stat()
                except Exception:
                    continue
                modified = datetime.fromtimestamp(st.st_mtime, tz=TZ)
                if modified >= cutoff:
                    continue
                candidates.append(
                    {
                        "candidate_id": f"stale-{uuid.uuid4().hex[:12]}",
                        "kind": "stale_artifact",
                        "path": str(file_path.resolve()),
                        "relative_path": rel,
                        "size_bytes": int(st.st_size),
                        "reason": f"older_than_{stale_days}d",
                        "source": "collector",
                        "context_reason": reason,
                    }
                )
    return candidates


def media_groups_to_candidates(groups: list[dict], root: Path, context: dict) -> list[dict]:
    candidates: list[dict] = []
    for g in groups:
        group_id = f"dup-{uuid.uuid4().hex[:12]}"
        size = int(g.get("size_bytes", 0))
        keep = Path(str(g.get("keep", ""))).resolve()
        for raw in g.get("delete", []):
            p = Path(str(raw)).resolve()
            allowed, reason = context_allows_file(p, root, context)
            if not allowed:
                continue
            candidates.append(
                {
                    "candidate_id": f"{group_id}-{uuid.uuid4().hex[:8]}",
                    "kind": "exact_duplicate_media",
                    "path": str(p),
                    "relative_path": safe_relative(p, root),
                    "size_bytes": size,
                    "reason": f"duplicate_of:{keep}",
                    "group_id": group_id,
                    "source": "media_duplicate_scan",
                    "context_reason": reason,
                }
            )
    return candidates


def write_review_markdown(path: Path, bundle: dict) -> None:
    lines: list[str] = []
    lines.append("# OpenClearn Review Report")
    lines.append("")
    lines.append(f"- generated_at: `{bundle.get('generated_at')}`")
    lines.append(f"- root: `{bundle.get('root')}`")
    lines.append(f"- candidate_count: `{len(bundle.get('candidates', []))}`")
    lines.append(f"- estimated_reclaim_mb: `{bundle.get('estimated_reclaim_bytes', 0) / 1024 / 1024:.2f}`")
    lines.append("")
    lines.append("## Top Candidates")
    lines.append("")
    for c in bundle.get("candidates", [])[:100]:
        lines.append(
            f"- `{c.get('candidate_id')}` | `{c.get('kind')}` | `{c.get('size_bytes', 0)}` bytes | `{c.get('relative_path')}`"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_approval_set(path: Path) -> tuple[set[str], set[str]]:
    payload = load_json(path, {})
    approved_ids = {str(x) for x in payload.get("approve_candidate_ids", []) if str(x)}
    approved_paths = {str(Path(str(x)).resolve()) for x in payload.get("approve_paths", []) if str(x)}
    return approved_ids, approved_paths


def move_to_trash(path: Path, trash_root: Path) -> Path:
    trash_root.mkdir(parents=True, exist_ok=True)
    target = trash_root / f"{path.name}.{uuid.uuid4().hex[:8]}.trash"
    shutil.move(str(path), str(target))
    return target


def apply_collector_deletion(
    bundle: dict,
    root: Path,
    approve_file: Path,
    trash_enabled: bool,
    trash_dir: Path,
    hard_delete: bool,
) -> dict:
    approved_ids, approved_paths = load_approval_set(approve_file)
    deleted: list[dict] = []
    skipped: list[dict] = []
    deleted_bytes = 0
    for c in bundle.get("candidates", []):
        cid = str(c.get("candidate_id", ""))
        raw_path = str(c.get("path", ""))
        p = Path(raw_path).resolve()
        allowed = cid in approved_ids or str(p) in approved_paths
        if not allowed:
            skipped.append({"candidate_id": cid, "path": str(p), "reason": "not_approved"})
            continue
        if not is_subpath(p, root):
            skipped.append({"candidate_id": cid, "path": str(p), "reason": "outside_root"})
            continue
        if not p.exists():
            skipped.append({"candidate_id": cid, "path": str(p), "reason": "missing"})
            continue
        size = int(c.get("size_bytes", 0))
        try:
            if hard_delete and not trash_enabled:
                p.unlink()
                deleted.append({"candidate_id": cid, "path": str(p), "action": "hard_delete"})
            else:
                moved = move_to_trash(p, trash_dir)
                deleted.append({"candidate_id": cid, "path": str(p), "action": "move_to_trash", "trash_path": str(moved)})
            deleted_bytes += size
        except Exception as exc:
            skipped.append({"candidate_id": cid, "path": str(p), "reason": f"delete_error:{exc.__class__.__name__}"})

    return {
        "approved_candidate_ids": len(approved_ids),
        "approved_paths": len(approved_paths),
        "deleted_count": len(deleted),
        "deleted_bytes": deleted_bytes,
        "deleted": deleted,
        "skipped_count": len(skipped),
        "skipped": skipped[:200],
    }


def build_cleanup_state(
    mode: str,
    dry_run: bool,
    stale_days: int,
    trial_timeout_hours: int,
    max_rollbacks: int,
    protect_keywords: list[str],
    media_enabled: bool,
    media_delete: bool,
    media_keep: str,
    media_roots: list[Path],
    removed_dup_snap: list[dict],
    removed_misc: list[dict],
    removed_dup_samples: list[dict],
    rolled_back: list[dict],
    media_groups: list[dict],
    media_reclaimable: int,
    media_deleted_files: int,
    media_deleted_bytes: int,
    snapshots_2: list[dict],
    api_binding: dict,
    agent_profile_name: str,
    persona: str,
) -> dict:
    return {
        "version": "v1.1",
        "updated_at": now_iso(),
        "status": "completed",
        "operation": "cleanup",
        "mode": mode,
        "dry_run": dry_run,
        "agent_profile": agent_profile_name,
        "agent_persona": persona,
        "llm_binding": api_binding,
        "metrics": {
            "removed_duplicate_snapshots": len(removed_dup_snap),
            "removed_misc_residue": len(removed_misc),
            "removed_duplicate_samples": len(removed_dup_samples),
            "rolled_back_stale_trials": len(rolled_back),
            "media_duplicate_groups": len(media_groups),
            "media_reclaimable_bytes": media_reclaimable,
            "media_deleted_files": media_deleted_files,
            "media_deleted_bytes": media_deleted_bytes,
            "estimated_reclaim_bytes": (
                estimate_json_rows_bytes(removed_dup_snap)
                + estimate_json_rows_bytes(removed_misc)
                + estimate_json_rows_bytes(removed_dup_samples)
                + media_reclaimable
            ),
        },
        "settings": {
            "stale_days": stale_days,
            "trial_timeout_hours": trial_timeout_hours,
            "max_rollbacks": max_rollbacks,
            "protect_keywords": protect_keywords,
            "media_enabled": media_enabled,
            "media_delete_duplicates": media_delete,
            "media_keep_strategy": media_keep,
            "media_roots": [str(p) for p in media_roots],
        },
        "top_media_groups": media_groups[:20],
        "line_distribution_after": dict(Counter(s.get("line_id", "unknown") for s in snapshots_2)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--mode", choices=["safe", "balanced", "aggressive"], default=None)
    parser.add_argument("--operation", choices=["cleanup", "collect", "review", "delete"], default="cleanup")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--stale-days", type=int, default=None)
    parser.add_argument("--trial-timeout-hours", type=int, default=None)
    parser.add_argument("--max-rollbacks", type=int, default=None)
    parser.add_argument("--enable-media", action="store_true")
    parser.add_argument("--no-media", action="store_true")
    parser.add_argument("--delete-media-duplicates", action="store_true")
    parser.add_argument("--agent-profile", default="openclaw")
    parser.add_argument("--agent-profile-file", default=None)
    parser.add_argument("--provider", default=None)
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--approve-file", default=None)
    parser.add_argument("--hard-delete", action="store_true")
    args = parser.parse_args()

    config = load_json(Path(args.config), {})
    mode = str(args.mode or config.get("mode", "safe")).lower()
    if mode not in MODE_DEFAULTS:
        raise SystemExit(f"Unsupported mode: {mode}")
    mode_cfg = MODE_DEFAULTS[mode]
    root = Path(config.get("root", ".")).resolve()

    profile_path = Path(args.agent_profile_file).resolve() if args.agent_profile_file else None
    agent_profile = load_agent_profile(args.agent_profile, profile_path)
    persona = str(agent_profile.get("persona", "cleaner"))
    api_binding = load_api_binding(config, args.provider, args.api_key_env)
    collector_context = load_collector_context(config, root)

    stale_days = pick_value(
        args.stale_days,
        int(mode_cfg["stale_days"]),
        int(config["stale_days"]) if "stale_days" in config else None,
    )
    trial_timeout_hours = pick_value(
        args.trial_timeout_hours,
        int(mode_cfg["trial_timeout_hours"]),
        int(config["trial_timeout_hours"]) if "trial_timeout_hours" in config else None,
    )
    max_rollbacks = pick_value(
        args.max_rollbacks,
        int(mode_cfg["max_rollbacks"]),
        int(config["max_rollbacks"]) if "max_rollbacks" in config else None,
    )
    protect_keywords = list(config.get("protect_keywords", ["OPENSPACE-"]))
    protect_keywords.extend([str(x) for x in agent_profile.get("extra_protect_keywords", []) if str(x)])

    media_cfg = dict(config.get("media_cleanup", {}))
    media_enabled = bool(media_cfg.get("enabled", mode_cfg["media_enabled"]))
    media_delete = bool(media_cfg.get("delete_duplicates", mode_cfg["media_delete_duplicates"]))
    if args.enable_media:
        media_enabled = True
    if args.no_media:
        media_enabled = False
    if args.delete_media_duplicates:
        media_enabled = True
        media_delete = True
    media_keep = str(media_cfg.get("keep_strategy", "oldest")).lower()
    if media_keep not in {"oldest", "newest"}:
        media_keep = "oldest"
    media_ext = list(media_cfg.get("extensions", DEFAULT_MEDIA_EXTENSIONS))
    media_min_size_bytes = int(media_cfg.get("min_size_kb", 64)) * 1024
    raw_roots = media_cfg.get("roots", ["scratch", "public", "frontend"])
    media_roots = [resolve_path(root, str(r)) for r in raw_roots]

    snap_path = root / str(config["snapshot_file"])
    ext_path = root / str(config["external_specimen_file"])
    adjust_path = root / str(config["structure_adjustment_file"])
    state_path = root / str(config["state_file"])
    report_path = root / str(config["report_jsonl"])

    collector_cfg = dict(config.get("collector", {}))
    candidates_file = resolve_path(root, str(collector_cfg.get("candidate_file", "state/g5_scavenger_candidates.json")))
    review_md = resolve_path(root, str(collector_cfg.get("review_markdown", "audit/g5_scavenger_review.md")))
    default_approve_file = resolve_path(root, str(collector_cfg.get("approve_file", "state/g5_scavenger_approve.json")))
    approve_file = Path(args.approve_file).resolve() if args.approve_file else default_approve_file
    trash_enabled = bool(collector_cfg.get("use_trash", True))
    trash_dir = resolve_path(root, str(collector_cfg.get("trash_dir", "trash/openclearn")))
    exclude_patterns = list(collector_cfg.get("exclude_patterns", []))
    exclude_patterns.extend([str(x) for x in agent_profile.get("collector_exclude_patterns", []) if str(x)])

    # collector/review/delete pipeline
    if args.operation in {"collect", "review", "delete"}:
        media_groups: list[dict] = []
        media_reclaimable = 0
        if media_enabled:
            media_groups, media_reclaimable = scan_media_duplicates(
                roots=media_roots,
                extensions=media_ext,
                min_size_bytes=media_min_size_bytes,
                keep_strategy=media_keep,
            )
        duplicate_candidates = media_groups_to_candidates(media_groups, root, collector_context)
        stale_candidates = scan_stale_files(root, collector_cfg, exclude_patterns, collector_context)
        all_candidates = duplicate_candidates + stale_candidates
        all_candidates.sort(key=lambda x: int(x.get("size_bytes", 0)), reverse=True)
        estimated_reclaim = sum(int(c.get("size_bytes", 0)) for c in all_candidates)
        bundle = {
            "version": "v1.1",
            "generated_at": now_iso(),
            "operation": args.operation,
            "root": str(root),
            "agent_profile": args.agent_profile,
            "agent_persona": persona,
            "llm_binding": api_binding,
            "collector_context": {
                "persona": collector_context.get("persona"),
                "principles": collector_context.get("principles", []),
                "allow_roots": [str(x) for x in collector_context.get("allow_roots", [])],
                "deny_roots": [str(x) for x in collector_context.get("deny_roots", [])],
                "deny_patterns": collector_context.get("deny_patterns", []),
            },
            "candidate_count": len(all_candidates),
            "estimated_reclaim_bytes": estimated_reclaim,
            "candidates": all_candidates,
        }
        write_json(candidates_file, bundle)
        if args.operation == "review":
            write_review_markdown(review_md, bundle)

        delete_result = None
        if args.operation == "delete":
            delete_result = apply_collector_deletion(
                bundle=bundle,
                root=root,
                approve_file=approve_file,
                trash_enabled=trash_enabled,
                trash_dir=trash_dir,
                hard_delete=bool(args.hard_delete),
            )
            bundle["delete_result"] = delete_result
            write_json(candidates_file, bundle)

        state = {
            "version": "v1.1",
            "updated_at": now_iso(),
            "status": "completed",
            "operation": args.operation,
            "mode": mode,
            "dry_run": args.dry_run,
            "agent_profile": args.agent_profile,
            "agent_persona": persona,
            "llm_binding": api_binding,
            "collector": {
                "candidate_file": str(candidates_file),
                "review_markdown": str(review_md),
                "approve_file": str(approve_file),
                "trash_enabled": trash_enabled,
                "trash_dir": str(trash_dir),
                "hard_delete": bool(args.hard_delete),
                "context_persona": collector_context.get("persona"),
                "context_principles": collector_context.get("principles", []),
                "candidate_count": len(all_candidates),
                "estimated_reclaim_bytes": estimated_reclaim,
            },
            "delete_result": delete_result,
        }
        write_json(state_path, state)
        append_jsonl(report_path, state)
        print(json.dumps(state, ensure_ascii=False))
        return

    # legacy cleanup pipeline
    snap_payload = load_json(snap_path, {"version": "v1", "updated_at": None, "snapshots": []})
    ext_payload = load_json(ext_path, {"updated_at": None, "samples": []})
    adjust_payload = load_json(adjust_path, {"records": []})

    snapshots = list(snap_payload.get("snapshots", []))
    samples = list(ext_payload.get("samples", []))
    records = list(adjust_payload.get("records", []))

    snapshots_1, removed_dup_snap = dedupe_snapshots(snapshots)
    snapshots_2, removed_misc = reap_misc_residue(snapshots_1, stale_days=stale_days)
    merged_samples, removed_dup_samples = dedupe_external_samples(samples)
    updated_records, rolled_back = rollback_stale_trials_guarded(
        records=records,
        trial_timeout_hours=trial_timeout_hours,
        max_rollbacks=max_rollbacks,
        protect_keywords=protect_keywords,
    )

    media_groups = []
    media_reclaimable = 0
    media_deleted_files = 0
    media_deleted_bytes = 0
    if media_enabled:
        media_groups, media_reclaimable = scan_media_duplicates(
            roots=media_roots,
            extensions=media_ext,
            min_size_bytes=media_min_size_bytes,
            keep_strategy=media_keep,
        )
        if (not args.dry_run) and media_delete and media_groups:
            media_deleted_files, media_deleted_bytes = delete_media_duplicates(media_groups)

    snap_payload["snapshots"] = snapshots_2
    snap_payload["updated_at"] = now_iso()
    ext_payload["samples"] = merged_samples
    ext_payload["updated_at"] = now_iso()
    adjust_payload["records"] = updated_records
    adjust_payload["updated_at"] = now_iso()

    if not args.dry_run:
        write_json(snap_path, snap_payload)
        write_json(ext_path, ext_payload)
        write_json(adjust_path, adjust_payload)

    state = build_cleanup_state(
        mode=mode,
        dry_run=args.dry_run,
        stale_days=stale_days,
        trial_timeout_hours=trial_timeout_hours,
        max_rollbacks=max_rollbacks,
        protect_keywords=protect_keywords,
        media_enabled=media_enabled,
        media_delete=media_delete,
        media_keep=media_keep,
        media_roots=media_roots,
        removed_dup_snap=removed_dup_snap,
        removed_misc=removed_misc,
        removed_dup_samples=removed_dup_samples,
        rolled_back=rolled_back,
        media_groups=media_groups,
        media_reclaimable=media_reclaimable,
        media_deleted_files=media_deleted_files,
        media_deleted_bytes=media_deleted_bytes,
        snapshots_2=snapshots_2,
        api_binding=api_binding,
        agent_profile_name=args.agent_profile,
        persona=persona,
    )
    if not args.dry_run:
        write_json(state_path, state)
        append_jsonl(report_path, state)

    print(json.dumps(state, ensure_ascii=False))


if __name__ == "__main__":
    main()
