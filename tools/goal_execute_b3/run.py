"""Run a finite native scenario set in independent child processes."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from .probe import SCENARIOS


def close_owned_player(manifest):
    """Timeout recovery verifies PID reuse, binary and exact unique IPC first."""
    import psutil
    if not manifest.is_file():
        return
    data = json.loads(manifest.read_text(encoding="utf8"))
    try:
        process = psutil.Process(data["pid"])
        if (process.create_time() != data["created"] or
                Path(process.exe()).resolve() != Path(data["binary"]).resolve() or
                "--input-ipc-server=" + data["pipe"] not in process.cmdline()):
            raise RuntimeError("owned_process_identity_changed_no_termination")
        process.terminate()
        try:
            process.wait(2)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(2)
    except psutil.NoSuchProcess:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--mpv", required=True)
    parser.add_argument("--case", choices=SCENARIOS)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if args.case:
        # probe imports below eventually construct app singletons; make even
        # their default relative logs/config local to this NEW evidence parent.
        if output.exists():
            raise SystemExit("new_case_directory_required")
        output.parent.mkdir(parents=True, exist_ok=True)
        os.chdir(output.parent)
        from .probe import run_case
        run_case(args.case, output, args.mpv)
        return
    if output.exists():
        raise SystemExit("new_evidence_directory_required")
    output.mkdir(parents=True)
    root = Path(__file__).resolve().parents[2]
    for case in SCENARIOS:
        env = dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        with (output / (case + ".log")).open("wb") as log:
            try:
                result = subprocess.run([sys.executable, "-B", "-X", "faulthandler", "-m",
                    "tools.goal_execute_b3.run", "--case", case, "--output", str(output / case),
                    "--mpv", args.mpv], cwd=output, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=20)
            except subprocess.TimeoutExpired:
                for manifest in (output / case / "state").glob("owned*process.json"):
                    close_owned_player(manifest)
                raise SystemExit("bounded_child_timeout:" + case)
        print(json.dumps(dict(case=case, exit=result.returncode)), flush=True)
        for manifest in (output / case / "state").glob("owned*process.json"):
            close_owned_player(manifest)
        if result.returncode:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
