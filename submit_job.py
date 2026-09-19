"""
Job Submitter CLI: Pushes code-generation tasks into the Redis queue.
Usage:
    python submit_job.py webapp "Create a tiny Flask API that returns JSON" [--watch]
"""

import os
import sys
import json
import uuid
import time
import argparse
import asyncio
import logging
from datetime import datetime, timezone
import redis.asyncio as aioredis
from dotenv import load_dotenv

from materialize import materialize_job

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("submit_job")

async def submit_job(task_type: str, description: str, watch: bool = False) -> str:
    """Submit a task to Redis and optionally watch its progress."""
    redis_host = os.getenv("REDIS_HOST", "localhost")
    redis_port = int(os.getenv("REDIS_PORT", "6379"))
    redis_db = int(os.getenv("REDIS_DB", "0"))
    redis_pwd = os.getenv("REDIS_PASSWORD") or None

    r = aioredis.Redis(
        host=redis_host,
        port=redis_port,
        db=redis_db,
        password=redis_pwd
    )

    job_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()

    task_payload = {
        "id": job_id,
        "type": task_type,
        "description": description,
        "submitted_at": now_iso
    }

    try:
        # Initialize job metadata hash
        await r.hset(
            f"job:{job_id}",
            mapping={
                "id": job_id,
                "type": task_type,
                "description": description,
                "status": "queued",
                "submitted_at": now_iso
            }
        )

        # Push to the job queue
        await r.lpush("job_queue", json.dumps(task_payload))
        logger.info(f"Job successfully queued! ID: {job_id}")

        if not watch:
            print("\n-----------------------------------------------------------")
            print(f" Job ID: {job_id}")
            print(f" Status: queued")
            print(f" Check status: python materialize.py {job_id}")
            print("-----------------------------------------------------------\n")
            return job_id

        print(f"\nWatching job progress for {job_id}...")
        while True:
            await asyncio.sleep(1.5)
            status_data = await r.hgetall(f"job:{job_id}")
            if not status_data:
                continue

            status = status_data.get(b"status", b"unknown").decode("utf-8")
            if status == "queued":
                print(".", end="", flush=True)
            elif status == "processing":
                print("*", end="", flush=True)
            elif status == "completed":
                print(f"\n[OK] Job completed successfully!")
                tokens = status_data.get(b"tokens", b"0").decode("utf-8")
                print(f"Tokens consumed: {tokens}")
                # Auto-materialize files
                dest_dir = await materialize_job(job_id, redis_client=r)
                print(f"Files written to: {dest_dir}\n")
                break
            elif status == "failed":
                err = status_data.get(b"error", b"Unknown failure").decode("utf-8")
                print(f"\n[FAIL] Job failed: {err}\n")
                break

        return job_id

    finally:
        await r.close()

def main():
    parser = argparse.ArgumentParser(description="Submit code generation tasks to the Groq worker pipeline.")
    parser.add_argument("type", help="Project or task type (e.g., 'webapp', 'cli', 'microservice')")
    parser.add_argument("description", help="Detailed natural language specification for the LLM")
    parser.add_argument("--watch", action="store_true", help="Stream progress and automatically unpack files on completion")

    args = parser.parse_args()

    try:
        asyncio.run(submit_job(args.type, args.description, args.watch))
    except KeyboardInterrupt:
        print("\nJob submission interrupted.")
    except Exception as e:
        logger.error(f"Error submitting job: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
