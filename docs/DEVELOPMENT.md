# SMARTfill Development

## Local Setup

```bash
cd "/Users/smit/Desktop/Payroll WebApp"
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python scripts/init_db.py
python app.py
```

Open:

```text
http://localhost:3000
```

## Environment

Copy the example file if local overrides are needed:

```bash
cp .env.example .env
```

Important variables:

```text
APP_HOST=0.0.0.0
APP_PORT=3000
DATABASE_PATH=data/attendance.db
SECRET_KEY=CHANGE_THIS_IN_PRODUCTION
```

## Tests

```bash
source venv/bin/activate
pytest -q
```

## Runtime Files

These are intentionally ignored by Git:

```text
venv/
data/*.db
uploads/*
output/*
tmp/*
__pycache__/
.pytest_cache/
```

Only `uploads/.gitkeep` is tracked so the upload directory exists after clone.
