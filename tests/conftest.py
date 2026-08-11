import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from attendance import db


@pytest.fixture()
def app():
    app = create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "WTF_CSRF_ENABLED": False,
        "SECRET_KEY": "test",
    })
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


def finalize_group(app, month="2026-07", group="MONTHLY"):
    """Lock a wage group so pay documents (slips, salary sheet) become downloadable.

    Tests that only care about a document's contents use this instead of driving the
    finalize form, which needs the admin password and a logged-in session.
    """
    from datetime import datetime

    from attendance.models import PayrollMonth
    from attendance.wage_groups import refresh_month_status

    field = "monthly_finalized_at" if group.upper() == "MONTHLY" else "daily_finalized_at"
    with app.app_context():
        payroll_month = db.session.get(PayrollMonth, month)
        setattr(payroll_month, field, datetime.utcnow())
        refresh_month_status(payroll_month)
        db.session.commit()
