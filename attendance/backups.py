import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from flask import current_app

from attendance import db

BACKUP_FORMAT = "smartfill-full-backup"
BACKUP_FORMAT_VERSION = 1
DATABASE_ARCHIVE_NAME = "database.sqlite"
MANIFEST_ARCHIVE_NAME = "manifest.json"
FILE_DIRECTORIES = ("uploads", "output")


def database_path():
    path = db.engine.url.database
    if not path or path == ":memory:":
        raise ValueError("Full backup needs a file-based SQLite database.")
    return Path(path)


def create_database_snapshot(target_path):
    """Copy SQLite through its backup API so an active app gets a consistent file."""
    source_path = database_path()
    target_path = Path(target_path)
    with sqlite3.connect(source_path) as source, sqlite3.connect(target_path) as target:
        source.backup(target)


def _add_directory(archive, app_root, directory_name):
    directory = app_root / directory_name
    if not directory.exists():
        return 0
    count = 0
    for path in sorted(item for item in directory.rglob("*") if item.is_file()):
        archive.write(path, path.relative_to(app_root).as_posix())
        count += 1
    return count


def build_full_backup_archive(target_path):
    app_root = Path(current_app.root_path)
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format": BACKUP_FORMAT,
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "app_version": "V1.04",
        "includes": ["database", *FILE_DIRECTORIES],
    }
    with TemporaryDirectory() as temp_dir:
        snapshot_path = Path(temp_dir) / DATABASE_ARCHIVE_NAME
        create_database_snapshot(snapshot_path)
        with ZipFile(target_path, "w", ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST_ARCHIVE_NAME, json.dumps(manifest, indent=2))
            archive.write(snapshot_path, DATABASE_ARCHIVE_NAME)
            file_count = sum(_add_directory(archive, app_root, name) for name in FILE_DIRECTORIES)
    return {"path": target_path, "file_count": file_count}


def validate_backup_archive(path):
    path = Path(path)
    try:
        with ZipFile(path) as archive:
            names = set(archive.namelist())
            if MANIFEST_ARCHIVE_NAME not in names:
                raise ValueError("Backup archive is missing manifest.json.")
            if DATABASE_ARCHIVE_NAME not in names:
                raise ValueError("Backup archive is missing the database snapshot.")
            manifest = json.loads(archive.read(MANIFEST_ARCHIVE_NAME).decode("utf-8"))
            if manifest.get("format") != BACKUP_FORMAT:
                raise ValueError("This is not a SMARTfill full backup archive.")
            if int(manifest.get("format_version", 0)) > BACKUP_FORMAT_VERSION:
                raise ValueError("This backup was created by a newer backup format.")
            for name in names:
                if Path(name).is_absolute() or ".." in Path(name).parts:
                    raise ValueError("Backup archive contains an unsafe file path.")
            with archive.open(DATABASE_ARCHIVE_NAME) as source:
                header = source.read(16)
            if header != b"SQLite format 3\x00":
                raise ValueError("Backup database snapshot is not a SQLite database.")
            return manifest
    except BadZipFile as exc:
        raise ValueError("Backup upload is not a valid ZIP archive.") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("Backup manifest is not valid JSON.") from exc


def _clear_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    for item in path.iterdir():
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()


def restore_full_backup_archive(path):
    manifest = validate_backup_archive(path)
    app_root = Path(current_app.root_path)
    current_database = database_path()
    with TemporaryDirectory() as temp_dir:
        temp_root = Path(temp_dir)
        with ZipFile(path) as archive:
            archive.extractall(temp_root)
        restored_database = temp_root / DATABASE_ARCHIVE_NAME

        db.session.remove()
        db.engine.dispose()
        shutil.copy2(restored_database, current_database)

        for directory_name in FILE_DIRECTORIES:
            destination = app_root / directory_name
            source = temp_root / directory_name
            _clear_directory(destination)
            if not source.exists():
                continue
            for item in source.rglob("*"):
                if not item.is_file():
                    continue
                target = destination / item.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
    return manifest
