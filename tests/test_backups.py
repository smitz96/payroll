from decimal import Decimal
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from app import create_app
from attendance import db
from attendance.models import AuditLog, Employee, User


def file_database_app(path):
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///" + str(path),
        "WTF_CSRF_ENABLED": False,
        "SECRET_KEY": "test",
    })
    return app


def test_full_backup_download_contains_manifest_and_database(tmp_path, monkeypatch):
    monkeypatch.setattr("attendance.backups.FILE_DIRECTORIES", ())
    app = file_database_app(tmp_path / "source.db")
    client = app.test_client()
    with app.app_context():
        db.session.add(Employee(id="5", name="Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000")))
        db.session.commit()

    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.get("/settings/backup/download")

    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/zip"
    assert "smartfill-full-backup-" in response.headers["Content-Disposition"]
    with ZipFile(BytesIO(response.data)) as archive:
        assert "manifest.json" in archive.namelist()
        assert "database.sqlite" in archive.namelist()
        assert archive.read("database.sqlite").startswith(b"SQLite format 3\x00")


def test_restore_backup_requires_password_phrase_and_replaces_database(tmp_path, monkeypatch):
    monkeypatch.setattr("attendance.backups.FILE_DIRECTORIES", ())
    source_app = file_database_app(tmp_path / "source.db")
    with source_app.app_context():
        db.session.add(Employee(id="5", name="Restored Worker", salary_type="Monthly", normalized_salary_type="MONTHLY", salary=Decimal("30000")))
        db.session.commit()
        from attendance.backups import build_full_backup_archive

        backup_path = tmp_path / "backup.zip"
        build_full_backup_archive(backup_path)

    target_app = file_database_app(tmp_path / "target.db")
    client = target_app.test_client()
    with target_app.app_context():
        db.session.add(Employee(id="9", name="Old Worker", salary_type="Daily", normalized_salary_type="DAILY", salary=Decimal("500")))
        db.session.commit()

    client.post("/login", data={"username": "admin", "password": "12345"})
    blocked = client.post(
        "/settings/backup/restore",
        data={
            "restore_confirmation": "restore backup",
            "admin_password": "wrong",
            "backup_zip": (BytesIO(backup_path.read_bytes()), "backup.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert b"Admin password is incorrect" in blocked.data

    with target_app.app_context():
        assert db.session.get(Employee, "9") is not None
        assert db.session.get(Employee, "5") is None

    restored = client.post(
        "/settings/backup/restore",
        data={
            "restore_confirmation": "restore backup",
            "admin_password": "12345",
            "backup_zip": (BytesIO(backup_path.read_bytes()), "backup.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )
    assert restored.status_code == 302

    with target_app.app_context():
        assert db.session.get(Employee, "5").name == "Restored Worker"
        assert db.session.get(Employee, "9") is None
        assert db.session.get(User, 1).username == "admin"
        audit = AuditLog.query.filter_by(action="Backup Restored").one()
        assert audit.actor == "admin"


def test_restore_rejects_non_backup_zip(tmp_path, monkeypatch):
    monkeypatch.setattr("attendance.backups.FILE_DIRECTORIES", ())
    app = file_database_app(tmp_path / "target.db")
    client = app.test_client()
    bad_zip = tmp_path / "bad.zip"
    with ZipFile(bad_zip, "w") as archive:
        archive.writestr("note.txt", "not a backup")

    client.post("/login", data={"username": "admin", "password": "12345"})
    response = client.post(
        "/settings/backup/restore",
        data={
            "restore_confirmation": "restore backup",
            "admin_password": "12345",
            "backup_zip": (BytesIO(bad_zip.read_bytes()), "bad.zip"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert b"Backup archive is missing manifest.json" in response.data
