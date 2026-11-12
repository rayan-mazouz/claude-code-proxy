"""Bounded raw-JSON MinIO to Borg retention worker."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config

from app.config import config_file_path
from app.utils import config_file, config_watch


def cycle() -> None:
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["ARCHIVE_S3_ENDPOINT_URL"],
        aws_access_key_id=os.environ["ARCHIVE_S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["ARCHIVE_S3_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("ARCHIVE_S3_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 5, "mode": "standard"}),
    )
    bucket = os.environ["ARCHIVE_S3_BUCKET"]
    prefix = os.environ.get("ARCHIVE_S3_PREFIX", "").strip("/")
    work = Path(os.environ.get("ARCHIVE_RETENTION_WORK_DIR", "/borg/work"))
    repo = Path(os.environ.get("ARCHIVE_BORG_REPOSITORY", "/borg/repository"))
    work.mkdir(parents=True, exist_ok=True)
    repo.parent.mkdir(parents=True, exist_ok=True)
    floor = int(os.environ.get("ARCHIVE_RETENTION_MIN_FREE_GIB", "10")) * 1024**3
    limit = int(os.environ.get("ARCHIVE_RETENTION_BATCH_BYTES", str(512 * 1024 * 1024)))
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(os.environ.get("ARCHIVE_HOT_RETENTION_DAYS", "4")))
    if shutil.disk_usage(work).free < floor:
        print("retention paused: disk floor", flush=True)
        return
    if not (repo / "config").exists():
        subprocess.run(["borg", "init", "--encryption=none", str(repo)], check=True)
    root = work / "batch"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir()
    batch = []
    total = 0
    seq = 0

    def flush() -> None:
        nonlocal total, seq
        if not batch:
            return
        name = f"raw-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{seq:06d}"
        subprocess.run(["borg", "create", "--compression", "zstd,3", "--stats", f"{repo}::{name}", str(root)], check=True)
        subprocess.run(["borg", "list", f"{repo}::{name}"], check=True, stdout=subprocess.DEVNULL)
        if os.environ.get("ARCHIVE_RETENTION_DELETE_MINIO", "false").lower() == "true":
            for key in batch:
                s3.delete_object(Bucket=bucket, Key=key)
        print(f"retention archive={name} objects={len(batch)} bytes={total}", flush=True)
        shutil.rmtree(root)
        root.mkdir()
        batch.clear()
        total = 0
        seq += 1

    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{prefix}/" if prefix else ""):
        for item in page.get("Contents", []):
            key = str(item.get("Key", ""))
            parts = key.split("/")
            if not key.endswith(".json"):
                continue
            try:
                partitions = {part.split("=", 1)[0]: part.split("=", 1)[1] for part in parts if "=" in part}
                event = datetime(
                    int(partitions["year"]),
                    int(partitions["month"]),
                    int(partitions["day"]),
                    int(partitions["hour"]),
                    tzinfo=timezone.utc,
                )
            except (KeyError, ValueError):
                continue
            if event >= cutoff:
                continue
            if shutil.disk_usage(work).free < floor + limit:
                print("retention paused: batch floor", flush=True)
                return
            dest = root.joinpath(*parts[1:] if parts and parts[0] == "raw" else parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            body = s3.get_object(Bucket=bucket, Key=key)["Body"]
            with dest.open("wb", buffering=1024 * 1024) as out:
                while chunk := body.read(8 * 1024 * 1024):
                    out.write(chunk)
            body.close()
            batch.append(key)
            total += dest.stat().st_size
            if total >= limit:
                flush()
    flush()


def _refresh_environment() -> None:
    """Apply the editable config file over the container environment (the file wins), so edits apply next cycle."""
    try:
        config_file.overlay_environ(config_file_path())
    except (OSError, ValueError) as exc:
        print(f"config file unreadable, keeping previous values: {type(exc).__name__}", flush=True)


def _interval() -> int:
    return max(60, int(os.environ.get("ARCHIVE_RETENTION_INTERVAL_SECONDS", "3600")))


def main() -> None:
    _refresh_environment()
    # Exits (status 0) when a restart-class setting changes, so Docker restarts the worker with the new config.
    watcher = config_watch.RestartWatcher(use_settings=False)
    announced_disabled = False
    while True:
        _refresh_environment()
        if os.environ.get("ARCHIVE_RETENTION_ENABLED", "false").lower() != "true":
            if not announced_disabled:
                print("retention disabled", flush=True)
                announced_disabled = True
        else:
            announced_disabled = False
            try:
                cycle()
            except Exception as exc:
                print(f"retention cycle failed: {exc!r}", flush=True)
        watcher.sleep(_interval())


if __name__ == "__main__":
    main()
