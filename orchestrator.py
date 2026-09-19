"""
Orchestrator service for distributing code-generation jobs to Groq workers.
Features:
 - Concurrency semaphore controls
 - Token budget & rate limit management
 - Prometheus metrics export (:9090)
 - Graceful shutdown on SIGINT/SIGTERM
 - Optional automatic Git repository publishing
"""

import os
import sys
import json
import time
import signal
import asyncio
import logging
from datetime import datetime, timezone
from typing import Set
import redis.asyncio as aioredis
from dotenv import load_dotenv

# Prometheus metrics
from prometheus_client import Counter, Gauge, start_http_server

from agent import run_task
from materialize import materialize_job
from git_publisher import publish_to_git

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [Orchestrator]: %(message)s")
logger = logging.getLogger("orchestrator")

# Metrics definitions
METRIC_JOBS_TOTAL = Counter("groq_jobs_total", "Total number of code generation jobs picked up")
METRIC_JOBS_SUCCESS = Counter("groq_jobs_success", "Total number of successfully completed jobs")
METRIC_JOBS_FAILED = Counter("groq_jobs_failed", "Total number of failed jobs")
METRIC_TOKENS_USED = Counter("groq_tokens_used", "Total LLM tokens consumed across jobs")
METRIC_QUEUE_DEPTH = Gauge("groq_queue_depth", "Current number of jobs pending in the Redis queue")
METRIC_ACTIVE_WORKERS = Gauge("groq_active_workers", "Number of worker tasks currently executing")

class Orchestrator:
    def __init__(self):
        self.redis_host = os.getenv("REDIS_HOST", "localhost")
        self.redis_port = int(os.getenv("REDIS_PORT", "6379"))
        self.redis_db = int(os.getenv("REDIS_DB", "0"))
        self.redis_pwd = os.getenv("REDIS_PASSWORD") or None

        self.max_workers = int(os.getenv("MAX_CONCURRENT_WORKERS", "2"))
        self.token_budget_per_min = int(os.getenv("TOKEN_BUDGET_PER_MIN", "60000"))
        self.metrics_port = int(os.getenv("PROMETHEUS_METRICS_PORT", "9090"))
        self.auto_publish_git = os.getenv("GIT_AUTO_PUBLISH", "false").lower() == "true"
        self.generated_dir = os.getenv("GENERATED_DIR", "./generated")

        self.redis: aioredis.Redis = None
        self.semaphore = asyncio.Semaphore(self.max_workers)
        self.active_tasks: Set[asyncio.Task] = set()
        self.is_running = True

    async def connect(self):
        redis_url = os.getenv("REDIS_URL")
        if redis_url:
            self.redis = aioredis.from_url(redis_url)
            logger.info("Connected to Redis via REDIS_URL")
        else:
            self.redis = aioredis.Redis(
                host=self.redis_host,
                port=self.redis_port,
                db=self.redis_db,
                password=self.redis_pwd
            )
            logger.info(f"Connected to Redis at {self.redis_host}:{self.redis_port}")
        await self.redis.ping()

    async def check_token_budget(self) -> bool:
        """
        Verify that token consumption in the rolling 60-second window does not exceed quota.
        """
        current_minute = int(time.time() // 60)
        token_key = f"groq_tokens_min:{current_minute}"
        used_tokens = await self.redis.get(token_key)
        
        if used_tokens and int(used_tokens) >= self.token_budget_per_min:
            logger.warning(
                f"Token budget reached for current minute ({used_tokens}/{self.token_budget_per_min}). "
                "Pausing worker execution..."
            )
            return False
        return True

    async def record_token_usage(self, tokens: int):
        """Record token consumption in Redis with a 120-second TTL."""
        current_minute = int(time.time() // 60)
        token_key = f"groq_tokens_min:{current_minute}"
        pipe = self.redis.pipeline()
        pipe.incrby(token_key, tokens)
        pipe.expire(token_key, 120)
        await pipe.execute()
        METRIC_TOKENS_USED.inc(tokens)

    async def execute_task(self, task: dict):
        """Worker execution wrapper for a single job."""
        job_id = task["id"]
        METRIC_ACTIVE_WORKERS.inc()
        try:
            # 1. Update status to processing
            started_at = datetime.now(timezone.utc).isoformat()
            await self.redis.hset(
                f"job:{job_id}",
                mapping={
                    "status": "processing",
                    "started_at": started_at
                }
            )
            logger.info(f"Worker started task {job_id} ({task.get('type')})")

            # 2. Rate-limit throttle check
            while not await self.check_token_budget():
                if not self.is_running:
                    return
                await asyncio.sleep(2.0)

            # 3. Call the Groq agent
            result = await run_task(task)

            total_tokens = result.get("usage", {}).get("total_tokens", 0)
            await self.record_token_usage(total_tokens)

            # 4. Mark job completed
            completed_at = datetime.now(timezone.utc).isoformat()
            await self.redis.hset(
                f"job:{job_id}",
                mapping={
                    "status": "completed",
                    "completed_at": completed_at,
                    "output": json.dumps(result.get("output", {})),
                    "tokens": str(total_tokens),
                    "model": result.get("model", "")
                }
            )
            METRIC_JOBS_SUCCESS.inc()
            logger.info(f"Task {job_id} successfully marked as completed.")

            # 5. Optional Auto-Git Publishing
            if self.auto_publish_git:
                try:
                    logger.info(f"Auto-publishing job {job_id} to Git...")
                    dest_dir = await materialize_job(
                        job_id=job_id,
                        base_output_dir=self.generated_dir,
                        redis_client=self.redis
                    )
                    git_res = publish_to_git(dest_dir, job_id)
                    if git_res.get("success"):
                        await self.redis.hset(
                            f"job:{job_id}",
                            mapping={"git_published": "true", "git_repo": git_res.get("repo_url") or "local"}
                        )
                except Exception as e:
                    logger.error(f"Git auto-publishing error for {job_id}: {e}")

        except Exception as e:
            METRIC_JOBS_FAILED.inc()
            logger.error(f"Execution failed for job {job_id}: {e}")
            await self.redis.hset(
                f"job:{job_id}",
                mapping={
                    "status": "failed",
                    "error": str(e),
                    "failed_at": datetime.now(timezone.utc).isoformat()
                }
            )
        finally:
            METRIC_ACTIVE_WORKERS.dec()
            self.semaphore.release()

    async def update_queue_metrics(self):
        """Periodically update queue depth gauge."""
        while self.is_running:
            try:
                depth = await self.redis.llen("job_queue")
                METRIC_QUEUE_DEPTH.set(depth)
            except Exception:
                pass
            await asyncio.sleep(5.0)

    async def run(self):
        """Main dispatcher loop."""
        try:
            start_http_server(self.metrics_port)
            logger.info(f"Prometheus metrics server running on port {self.metrics_port}")
        except Exception as e:
            logger.warning(f"Could not start Prometheus metrics server: {e}")

        metrics_task = asyncio.create_task(self.update_queue_metrics())

        logger.info(f"Orchestrator ready. Concurrency limit: {self.max_workers}. Awaiting jobs on 'job_queue'...")

        while self.is_running:
            try:
                # Acquire slot before taking job off queue
                await self.semaphore.acquire()

                if not self.is_running:
                    self.semaphore.release()
                    break

                # Pop task with 1-second timeout to allow loop cancellation
                item = await self.redis.brpop("job_queue", timeout=1)
                if not item:
                    self.semaphore.release()
                    continue

                _, raw_data = item
                task_data = json.loads(raw_data.decode("utf-8"))
                METRIC_JOBS_TOTAL.inc()

                # Dispatch worker task
                task = asyncio.create_task(self.execute_task(task_data))
                self.active_tasks.add(task)
                task.add_done_callback(self.active_tasks.discard)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in orchestrator loop: {e}")
                await asyncio.sleep(1)

        metrics_task.cancel()
        logger.info("Orchestrator stopping. Draining active jobs...")
        if self.active_tasks:
            await asyncio.gather(*self.active_tasks, return_exceptions=True)
        if self.redis:
            await self.redis.close()
        logger.info("Orchestrator shutdown complete.")

    def stop(self):
        logger.info("Shutdown signal received.")
        self.is_running = False

def handle_signals(orchestrator: Orchestrator):
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, orchestrator.stop)
        except NotImplementedError:
            # Signal handling on Windows (asyncio may not support add_signal_handler)
            signal.signal(sig, lambda s, f: orchestrator.stop())

async def main():
    orchestrator = Orchestrator()
    handle_signals(orchestrator)
    await orchestrator.connect()
    await orchestrator.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
