"""DB session wiring for the API — identical pattern to run_agent.py/hitl.py:
one shared session_factory, a fresh session opened and closed per request."""

import os

from db.models import get_engine, init_db

session_factory = init_db(get_engine(os.environ.get("DATABASE_URL", "sqlite:///sre_platform.db")))


def get_db():
    db = session_factory()
    try:
        yield db
    finally:
        db.close()
