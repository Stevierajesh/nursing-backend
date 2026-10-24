"""Production entrypoint: gunicorn -w 2 -b 0.0.0.0:5001 wsgi:app"""

from app import create_app
from app.config import Config
from app.jobs.scheduler import start_scheduler

Config.validate()
app = create_app()

# The scheduler self-guards with a lock file, so only one gunicorn worker
# actually owns the timers.
start_scheduler()
