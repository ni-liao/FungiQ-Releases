"""Exercise the real Velopack download/apply/restart path in an isolated layout."""
from __future__ import annotations

import argparse
import binascii
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import zipfile

import velopack


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.server.requests.append(urlsplit(self.path).path)
        super().do_GET()


def sha256_file(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def clean_environment():
    env = os.environ.copy()
    windows = Path(env["SYSTEMROOT"])
    env["PATH"] = str(windows / "System32") + os.pathsep + str(windows)
    for key in ("PYTHONHOME", "PYTHONPATH", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH"):
        env.pop(key, None)
    env["QT_QPA_PLATFORM"] = "windows"
    return env


def wait_report(path, timeout=360):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                time.sleep(.25)
                continue
            if not value.get("success"):
                raise RuntimeError(f"Startup reported a failure: {path}")
            return value
        time.sleep(.25)
    raise TimeoutError(f"No successful startup report: {path}")


def verify_layout(package, current):
    verified = 0
    expected_internal = set()
    with zipfile.ZipFile(package) as archive:
        for item in archive.infolist():
            if not item.filename.startswith("lib/app/") or item.is_dir():
                continue
            relative = item.filename[len("lib/app/"):]
            if relative.startswith("_internal/"):
                expected_internal.add(relative)
            if relative.endswith("_ExecutionStub.exe") or relative == "Squirrel.exe":
                continue
            path = current / relative
            if not path.is_file() or path.stat().st_size != item.file_size:
                raise RuntimeError(f"Missing or wrong-sized updated file: {path}")
            crc = 0
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    crc = binascii.crc32(chunk, crc)
            if crc != item.CRC:
                raise RuntimeError(f"Updated file differs from release package: {path}")
            verified += 1
    actual_internal = {str(p.relative_to(current)).replace("\\", "/")
                       for p in (current / "_internal").rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    if actual_internal != expected_internal:
        raise RuntimeError("Updated runtime contains missing or leftover library files")
    return verified


def download_and_schedule(args, output):
    root, reports = output / "app", output / "reports"
    current, packages = root / "current", root / "packages"
    locator = velopack.VelopackLocatorConfig(root, root / "Update.exe", packages,
                                           current / "sq.version", current, True)
    manager = velopack.UpdateManager(velopack.HttpSource(args.stage_source),
                                     velopack.UpdateOptions(False, 10, args.edition.lower()), locator)
    if manager.get_current_version() != args.from_version:
        raise RuntimeError("Isolated installation has the wrong base version")
    update = manager.check_for_updates()
    if update is None or update.TargetFullRelease.Version != args.to_version:
        raise RuntimeError("Update manager did not find the target release")
    if not args.full and not update.DeltasToTarget:
        raise RuntimeError("Expected a delta upgrade, but the release offers no delta")
    if args.full and update.DeltasToTarget:
        raise RuntimeError("Expected a full upgrade without a cached base package")
    progress = []
    manager.download_updates(update, lambda value: progress.append(int(value)))
    pending = manager.get_update_pending_restart()
    if pending is None or pending.Version != args.to_version:
        raise RuntimeError("Downloaded release is not staged for application")
    (reports / "download.json").write_text(json.dumps({
        "delta_count": len(update.DeltasToTarget), "download_progress_max": max(progress, default=0),
        "download_process_id": os.getpid(), "cwd_after_download": str(Path.cwd()),
    }, indent=2), encoding="utf-8")
    if args.trim_test_cache:
        # The cached base is no longer used after delta reconstruction.
        (packages / f"FungiQ.{args.edition}-{args.from_version}-{args.edition.lower()}-full.nupkg").unlink(missing_ok=True)
    manager.wait_exit_then_apply_updates(update, True, True,
                                         ["--startup-check", str(reports / "after.json")])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--edition", choices=("CPU", "GPU"), required=True)
    parser.add_argument("--from-version", default="1.0.9")
    parser.add_argument("--to-version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--feed", type=Path)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--stage-source")
    parser.add_argument("--trim-test-cache", action="store_true")
    args = parser.parse_args()
    workspace = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if args.stage_source:
        download_and_schedule(args, output)
        return
    output.mkdir(parents=True, exist_ok=False)
    root, reports = output / "app", output / "reports"
    current, packages = root / "current", root / "packages"
    for directory in (current, packages, reports):
        directory.mkdir(parents=True)
    channel = args.edition.lower()
    feed = args.feed.resolve() if args.feed else workspace / "releases" / f"FungiQ_{args.edition}"
    if args.trim_test_cache and (feed.name != "feed" or feed.parent != output.parent):
        raise ValueError("Cache trimming is only allowed in a disposable sibling test feed")
    base = feed / f"FungiQ.{args.edition}-{args.from_version}-{channel}-full.nupkg"
    target = feed / f"FungiQ.{args.edition}-{args.to_version}-{channel}-full.nupkg"
    started = time.perf_counter()
    with zipfile.ZipFile(base) as archive:
        for item in archive.infolist():
            if not item.filename.startswith("lib/app/") or item.is_dir():
                continue
            destination = current / item.filename[len("lib/app/"):]
            if not destination.resolve().is_relative_to(current.resolve()):
                raise ValueError("Package path escaped the isolated application directory")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(item) as source, destination.open("wb") as stream:
                shutil.copyfileobj(source, stream, 4 * 1024 * 1024)
    # Portable isolation changes only shell registration, not package application.
    # Do not replace the user's installed app, registry entries, or shortcuts.
    (root / ".portable").touch()
    shutil.copy2(current / "Squirrel.exe", root / "Update.exe")
    shutil.copy2(current / f"FungiQ_{args.edition}_ExecutionStub.exe", root / f"FungiQ_{args.edition}.exe")
    if not args.full:
        try:
            os.link(base, packages / base.name)
        except OSError:
            shutil.copy2(base, packages / base.name)
    if args.trim_test_cache:
        base.unlink()
    old_report = reports / "before.json"
    subprocess.run([str(current / f"FungiQ_{args.edition}.exe"), "--startup-check", str(old_report)],
                   cwd=reports, env=clean_environment(), timeout=300, check=True)
    wait_report(old_report)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(feed)))
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        command = [sys.executable, str(Path(__file__).resolve()), "--edition", args.edition,
                   "--from-version", args.from_version, "--to-version", args.to_version,
                   "--output", str(output), "--stage-source", f"http://127.0.0.1:{server.server_port}"]
        if args.full:
            command.append("--full")
        if args.trim_test_cache:
            command.append("--trim-test-cache")
        subprocess.run(command, cwd=reports, env=clean_environment(), timeout=300, check=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    requested_packages = [path.rsplit("/", 1)[-1] for path in server.requests if path.endswith(".nupkg")]
    expected_request = target.name if args.full else target.name.replace("-full.nupkg", "-delta.nupkg")
    if not requested_packages or set(requested_packages) != {expected_request}:
        raise RuntimeError(f"Wrong package download path: {requested_packages}")
    candidates = list(packages.glob(f"*{args.to_version}*full.nupkg"))
    if len(candidates) != 1:
        raise RuntimeError(f"Expected one staged full package, got {candidates}")
    with zipfile.ZipFile(target) as expected, zipfile.ZipFile(candidates[0]) as staged:
        content = lambda archive: {item.filename: (item.file_size, item.CRC) for item in archive.infolist() if not item.is_dir()}
        if content(expected) != content(staged):
            raise RuntimeError("Delta reconstruction differs from the release package")
    new_report = reports / "after.json"
    after = wait_report(new_report)
    if after.get("version") != args.to_version or after.get("edition") != args.edition:
        raise RuntimeError("Restarted application has the wrong version or edition")
    verified = verify_layout(target, current)
    download = json.loads((reports / "download.json").read_text(encoding="utf-8"))
    if (root / "velopack.log").is_file():
        shutil.copy2(root / "velopack.log", reports / "apply.log")
    value = {"success": True, "edition": args.edition, "from_version": args.from_version,
             "to_version": args.to_version, "mode": "full" if args.full else "delta",
             **download, "sdk_wait_exit_then_apply_updates": True,
             "downloaded_packages": requested_packages, "target_package_sha256": sha256_file(target),
             "target_delta_sha256": sha256_file(target.with_name(target.name.replace("-full.nupkg", "-delta.nupkg"))),
             "reconstructed_package_contents_match": True, "verified_app_files": verified,
             "real_update_exe_apply_and_restart": True, "startup": after,
             "shell_registration_isolated": True, "test_root": str(root),
             "elapsed_seconds": round(time.perf_counter() - started, 2)}
    (reports / "upgrade.json").write_text(json.dumps(value, indent=2), encoding="utf-8")
    print(json.dumps(value, indent=2), flush=True)


if __name__ == "__main__":
    main()
