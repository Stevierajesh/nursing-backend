"""Development entrypoint. Production uses gunicorn against `wsgi:app`."""

import os

from app import create_app
from app.config import Config
from app.jobs.scheduler import start_scheduler

app = create_app()

if __name__ == "__main__":
    Config.validate()
    start_scheduler()
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5001")),
        debug=Config.DEBUG,
    )
