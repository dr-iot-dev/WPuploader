"""Independent uploader CLI."""

import argparse
import json
import sqlite3
import time
from pathlib import Path

from .protocol import ProtocolError, process_lock
from .service import Uploader
from .settings import Settings
from .store import Store


def main() -> int:
    parser = argparse.ArgumentParser(prog="wpuploader")
    parser.add_argument("--state-dir", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init")
    initialize.add_argument("--new-install", action="store_true", required=True)
    importing = commands.add_parser("import")
    importing.add_argument("file", type=Path)
    for name in ("run-once", "reconcile", "dry-run", "status"):
        commands.add_parser(name)
    watcher = commands.add_parser("watch")
    watcher.add_argument("--interval", type=float, default=60)
    commands.add_parser("backup").add_argument("file", type=Path)
    recovery = commands.add_parser("recover")
    recovery.add_argument("--confirm-reconciliation-only", action="store_true", required=True)
    finish = commands.add_parser("finish-recovery")
    finish.add_argument("--confirmed-history-reconciled", action="store_true", required=True)
    commands.add_parser("migrate-history").add_argument("file", type=Path)
    replacement = commands.add_parser("replace")
    replacement.add_argument("package_id")
    replacement.add_argument("--expected-remote-hash", required=True)
    resolution = commands.add_parser("resolve")
    resolution.add_argument("package_id")
    choices = resolution.add_mutually_exclusive_group(required=True)
    choices.add_argument("--adopt-post-id")
    choices.add_argument("--confirmed-not-created", action="store_true")
    media_resolution = commands.add_parser("resolve-media")
    media_resolution.add_argument("package_id")
    media_choices = media_resolution.add_mutually_exclusive_group(required=True)
    media_choices.add_argument("--adopt-media-id")
    media_choices.add_argument("--confirmed-not-created", action="store_true")
    args = parser.parse_args()
    store = None
    try:
        settings = Settings.from_env(args.state_dir)
        if args.command == "init":
            if (settings.state_dir / "state.sqlite3").exists():
                raise ProtocolError("STATE_ALREADY_EXISTS")
            store = Store(settings.state_dir, initialize=True)
            print("initialized")
            return 0
        if args.command == "recover":
            store = Store(
                settings.state_dir, initialize=not (settings.state_dir / "state.sqlite3").exists()
            )
            uploader = Uploader(store, settings)
            uploader.scan()
            with process_lock(store.directory / "worker.lock"):
                uploader.prepare(recovery=True)
            print("reconciliation-only recovery; no remote writes performed")
            return 0
        store = Store(settings.state_dir)
        if args.command == "status":
            print(json.dumps(store.status(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "backup":
            store.backup(args.file)
            return 0
        uploader = Uploader(store, settings)
        if args.command == "import":
            print(uploader.import_file(args.file))
        elif args.command == "dry-run":
            with process_lock(store.directory / "worker.lock"):
                errors = uploader.scan()
                uploader.prepare()
            for row in store.db.execute("SELECT package_id,payload,state,error FROM deliveries"):
                print(json.dumps(dict(row), ensure_ascii=False))
            return 1 if errors else 0
        elif args.command == "finish-recovery":
            uploader.finish_recovery()
            print("recovery reconciliation finished")
        elif args.command == "migrate-history":
            print(f"imported: {uploader.import_history(args.file)}")
        elif args.command == "replace":
            uploader.replace(args.package_id, args.expected_remote_hash)
        elif args.command == "resolve-media":
            uploader.resolve_media(
                args.package_id,
                adopt_media_id=args.adopt_media_id,
                confirmed_not_created=args.confirmed_not_created,
            )
        elif args.command == "resolve":
            uploader.resolve(
                args.package_id,
                adopt_post_id=args.adopt_post_id,
                confirmed_not_created=args.confirmed_not_created,
            )
        elif args.command in {"run-once", "reconcile"}:
            errors = uploader.run_once(reconcile_only=args.command == "reconcile")
            print(
                json.dumps({"status": store.status(), "import_errors": errors}, ensure_ascii=False)
            )
            return 1 if errors else 0
        elif args.command == "watch":
            if args.interval < 1:
                raise ProtocolError("WATCH_INTERVAL_INVALID")
            cycles = 0
            while True:
                # Credential changes require creating a new client; target changes are rejected.
                settings = Settings.from_env(args.state_dir)
                uploader = Uploader(store, settings)
                try:
                    uploader.run_once()
                    if cycles % 10 == 0:
                        uploader.run_once(reconcile_only=True)
                except ProtocolError as exc:
                    if exc.code != "WORKER_BUSY":
                        raise
                cycles += 1
                time.sleep(args.interval)
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": getattr(exc, "code", "LOCAL_OPERATION_FAILED")}))
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
