"""
Materializer script to extract generated codebase files from Redis into the local filesystem.
Usage:
    python materialize.py <job_id> [--output-dir ./generated]
"""

import os
import sys
import json
import logging
import asyncio
from typing import Optional, Dict, Any
import redis.asyncio as aioredis
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("materialize")

async def get_job_data(redis_client: aioredis.Redis, job_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve job data from Redis."""
    raw = await redis_client.hgetall(f"job:{job_id}")
    if not raw:
        return None
    
    data = {k.decode("utf-8"): v.decode("utf-8") for k, v in raw.items()}
    if "output" in data:
        try:
            data["output"] = json.loads(data["output"])
        except json.JSONDecodeError:
            pass
    return data

async def materialize_job(
    job_id: str,
    base_output_dir: str = "./generated",
    redis_client: Optional[aioredis.Redis] = None
) -> str:
    """
    Write all files for job_id to disk under base_output_dir/<job_id>/.
    Returns the absolute path of the generated directory.
    """
    own_client = False
    if redis_client is None:
        redis_host = os.getenv("REDIS_HOST", "localhost")
        redis_port = int(os.getenv("REDIS_PORT", "6379"))
        redis_db = int(os.getenv("REDIS_DB", "0"))
        redis_pwd = os.getenv("REDIS_PASSWORD") or None
        redis_client = aioredis.Redis(
            host=redis_host,
            port=redis_port,
            db=redis_db,
            password=redis_pwd
        )
        own_client = True

    try:
        job = await get_job_data(redis_client, job_id)
        if not job:
            raise ValueError(f"Job '{job_id}' was not found in Redis.")

        status = job.get("status", "unknown")
        if status != "completed":
            raise RuntimeError(f"Job '{job_id}' cannot be materialized. Current status: '{status}'")

        output = job.get("output", {})
        structure = output.get("structure", {})
        readme = output.get("readme", "")

        target_dir = os.path.abspath(os.path.join(base_output_dir, job_id))
        os.makedirs(target_dir, exist_ok=True)

        logger.info(f"Materializing {len(structure)} files for job {job_id} into {target_dir}")

        # Write structure files
        for rel_path, content in structure.items():
            # Sanitize rel_path to avoid path traversal
            norm_rel = os.path.normpath(rel_path).lstrip("/\\")
            file_path = os.path.join(target_dir, norm_rel)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            logger.info(f"  -> Created file: {norm_rel}")

        # Write README.md if provided and not already in structure
        if readme and "README.md" not in structure and "readme.md" not in structure:
            readme_path = os.path.join(target_dir, "README.md")
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(readme)
            logger.info("  -> Created file: README.md")

        logger.info(f"Successfully materialized project at: {target_dir}")
        return target_dir

    finally:
        if own_client:
            await redis_client.close()

def main():
    if len(sys.argv) < 2:
        print("Usage: python materialize.py <job_id> [output_dir]")
        sys.exit(1)

    job_id = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else os.getenv("GENERATED_DIR", "./generated")

    try:
        path = asyncio.run(materialize_job(job_id, out_dir))
        print(f"\n[OK] Job materialized successfully at:\n  {path}")
    except Exception as e:
        logger.error(f"Failed to materialize job: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
