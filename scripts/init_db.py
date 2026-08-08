import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from attendance import db
from attendance.authentication import init_admin_user

app = create_app()

with app.app_context():
    db.create_all()
    init_admin_user()
    print("SMARTfill database initialized.")
