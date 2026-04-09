@echo off
setlocal
cd /d "%~dp0"
python patrol.py --config config.example.json --mode balanced --cycles 0 --interval-seconds 1800 --auto-apply --apply-threshold-mb 256 --log-file patrol_reports.jsonl
endlocal
