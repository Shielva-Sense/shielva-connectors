"""
Shielva Connectors - Distributed Scheduler
Uses APScheduler with Redis Job Store to manage auto-ingestion jobs.
"""

import os

import structlog
from apscheduler.jobstores.redis import RedisJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = structlog.get_logger(__name__)


class ConnectorScheduler:
    """
    Distributed scheduler for connector sync jobs.
    """

    def __init__(self, redis_url: str = None):
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379")

        # Configure Redis Job Store
        # We need to parse redis_url to get host, port, db, password
        # Or simpler: let RedisJobStore handle it, but it takes specific args
        # Let's use a simpler approach: strict args or parsing

        # Parse REDIS_URL "redis://[:password@]host:port/db"
        from urllib.parse import urlparse

        r = urlparse(self.redis_url)

        jobstores = {
            "default": RedisJobStore(
                host=r.hostname,
                port=r.port,
                db=int(r.path.lstrip("/")) if r.path else 0,
                password=r.password,
            )
        }

        self.scheduler = AsyncIOScheduler(jobstores=jobstores)
        self._started = False

        # Configure dedicated log file
        self._setup_logging()

    def _setup_logging(self):
        """Setup independent logging for scheduler to ../logs/scheduler.log"""
        import logging

        # Target the APScheduler logger and our service logger
        # APScheduler uses 'apscheduler'
        # Our service uses 'services.scheduler' (derived from __name__)

        log_file = os.path.abspath(os.path.join(os.getcwd(), "../logs/scheduler.log"))

        try:
            # Create handler
            handler = logging.FileHandler(log_file)
            formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            handler.setFormatter(formatter)
            handler.setLevel(logging.INFO)

            # Attach to 'apscheduler' logger
            ap_logger = logging.getLogger("apscheduler")
            ap_logger.setLevel(logging.INFO)
            ap_logger.addHandler(handler)
            ap_logger.propagate = True  # Ensure it bubbles up to root/console too

            # Attach to this service's logger
            # Note: structlog.get_logger(__name__) might not use standard logging system directly
            # depending on config, but if it does, this works.
            # If standard logging is used as backend for structlog (common), this works.
            # To be safe, we also grab standard logger for this module name.
            svc_logger = logging.getLogger(__name__)
            svc_logger.addHandler(handler)

            logger.info(f"Scheduler logging configured to {log_file}")

        except Exception as e:
            print(f"Failed to setup scheduler logging: {e}")

    def start(self):
        """Start the scheduler"""
        if not self._started:
            self.scheduler.start()
            self._started = True
            logger.info("Connector Scheduler started")

    def shutdown(self):
        """Shutdown the scheduler"""
        if self._started:
            self.scheduler.shutdown()
            self._started = False
            logger.info("Connector Scheduler shutdown")

    def schedule_connector(
        self,
        connector_id: str,
        kb_id: str,
        interval_seconds: int = 10,
        webhook_url: str = None,
    ):
        """
        Schedule a sync job for a connector.
        """
        job_id = f"sync_{connector_id}"

        # Define the job function (needs to be importable or static)
        # We can't pass 'connector' instance directly to RedisJobStore (serialization)
        # So we trigger a function that calls the gateway/connector via registry

        # If job exists, update it?
        if self.scheduler.get_job(job_id):
            logger.info("Updating existing schedule", job_id=job_id, interval=interval_seconds)
            self.scheduler.reschedule_job(job_id, trigger=IntervalTrigger(seconds=interval_seconds))
            # Update args if needed? APScheduler doesn't easily support updating args of existing job without replace
            # Better to remove and add
            self.scheduler.remove_job(job_id)

        # Add new job
        self.scheduler.add_job(
            func="services.scheduler:run_scheduled_sync",
            trigger=IntervalTrigger(seconds=interval_seconds),
            id=job_id,
            name=f"Sync {connector_id}",
            replace_existing=True,
            kwargs={
                "connector_id": connector_id,
                "kb_id": kb_id,
                "webhook_url": webhook_url,
            },
        )

        logger.info(
            "Scheduled connector sync",
            connector_id=connector_id,
            interval=interval_seconds,
            job_id=job_id,
        )

    def unschedule_connector(self, connector_id: str):
        """Remove schedule for a connector"""
        job_id = f"sync_{connector_id}"
        if self.scheduler.get_job(job_id):
            self.scheduler.remove_job(job_id)
            logger.info("Unscheduled connector sync", connector_id=connector_id)
            return True
        return False

    def get_job_status(self, connector_id: str):
        """Get job status"""
        job_id = f"sync_{connector_id}"
        job = self.scheduler.get_job(job_id)
        if job:
            return {
                "status": "active",
                "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
                "interval": str(job.trigger),
            }
        return {"status": "inactive"}


# Global scheduler instance
# Initialize in gateway startup
scheduler = None


# Job Function (Must be top-level for pickling)
async def run_scheduled_sync(connector_id: str, kb_id: str, webhook_url: str = None):
    """
    Execute the sync.
    NOTE: This runs in the scheduler process/thread.
    We need access to the connector registry.
    """
    logger.info("Executing scheduled sync", connector_id=connector_id)

    # Import here to avoid circular deps
    from gateway import registry

    connector = registry.get(connector_id)
    if not connector:
        logger.warning("Connector not found during scheduled sync", connector_id=connector_id)
        return

    try:
        # Run incremental sync
        # Since it's frequent (10s), we trust 'since' or internal state
        # But 'since' needs to be managed.
        # BaseConnector logic: if 'since' is None, it defaults to 30 days. That's bad for 10s interval.
        # We should probably pass 'since' = last_run_time?
        # Or let the connector handle efficient "new only" fetching.
        # SlackConnector uses 'oldest' param.
        # Ideally, we store last_sync in Redis/ConnectorStatus and pass that.

        # Just call sync() - let connector/gateway logic handle 'since' optimization?
        # SlackConnector.sync() Logic:
        # if not since: since = now - 30 days (BAD for freq sync)
        # We need to persist 'last_sync' and use it.

        # FIX: We should fetch last_sync from connector status or redis
        status = connector.get_status()
        since = status.last_sync

        # If never synced, do full history (default behavior)
        # If synced recently, use that time.

        await connector.sync(since=since, full=False, kb_id=kb_id, webhook_url=webhook_url)
    except Exception as e:
        logger.error("Scheduled sync failed", connector_id=connector_id, error=str(e))
