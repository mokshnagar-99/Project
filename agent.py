"""
Agent module for generating production-ready codebases using Groq LLMs.
Implements robust JSON extraction, rate-limit backoff, and token accounting.
"""

import os
import json
import re
import asyncio
import logging
from typing import Dict, Any
from dotenv import load_dotenv

# Load .env file at module import
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("agent")

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
if not GROQ_API_KEY or GROQ_API_KEY.startswith("gsk_your_groq"):
    logger.warning(
        "GROQ_API_KEY is not set or using placeholder! "
        "Please update your .env file with a valid Groq API key."
    )

try:
    from groq import AsyncGroq, RateLimitError, APIError
except ImportError:
    AsyncGroq = None
    RateLimitError = Exception
    APIError = Exception

def get_groq_client() -> Any:
    """Initialize and return the Groq Async client with verification."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key or api_key.startswith("gsk_your_groq"):
        raise RuntimeError(
            "GROQ_API_KEY is missing! Set it in your .env file or export GROQ_API_KEY in your environment."
        )
    if AsyncGroq is None:
        raise RuntimeError("groq package is not installed. Run 'pip install -r requirements.txt'")
    return AsyncGroq(api_key=api_key)

BASE_PROMPT = """You are a senior principal software engineer. You generate complete, production-ready project codebases.
Project Type: {type}

Requirements:
1. Implement the requested system completely with clean, modular, idiomatic code.
2. Include all necessary source files, configuration files, and package manifests (e.g. requirements.txt, package.json, Dockerfile, etc.).
3. Provide a clear README.md with build, test, and run instructions.
4. Output MUST be valid JSON only. Do not enclose the JSON in Markdown code fences. Do not output conversational filler.

JSON Output Schema:
{{
  "structure": {{
    "path/to/file.ext": "full file content as string",
    ...
  }},
  "readme": "# Project Title\\n\\nDetailed setup and run guide..."
}}
"""

def extract_json_payload(raw_text: str) -> Dict[str, Any]:
    """Parse JSON directly or extract using markdown fence and regex fallbacks."""
    text = raw_text.strip()
    
    # 1. Direct JSON decode
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "structure" in data:
            return data
    except json.JSONDecodeError:
        pass

    # 2. Markdown fence removal (```json ... ```)
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if match:
        try:
            data = json.loads(match.group(1).strip())
            if isinstance(data, dict) and "structure" in data:
                return data
        except json.JSONDecodeError:
            pass

    # 3. Outer bracket extraction
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and "structure" in data:
                return data
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Failed to extract valid JSON codebase structure from LLM response. Content sample: {text[:200]}")

async def run_task(task: dict, max_retries: int = 4) -> dict:
    """
    Execute a code generation task using the Groq API.
    Handles rate-limits with exponential backoff and tracks token usage.
    """
    client = get_groq_client()
    model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    task_type = task.get("type", "general")
    task_desc = task.get("description", "")
    task_id = task.get("id", "unknown")

    prompt = BASE_PROMPT.format(type=task_type)
    prompt += f"\nUser Specification:\n{task_desc}\n"

    attempt = 0
    backoff = 2.0

    while attempt < max_retries:
        try:
            logger.info(f"Dispatching task {task_id} to Groq (model: {model}, attempt: {attempt + 1})")
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an automated code generation engine. You always reply with valid JSON conforming to the requested schema."
                    },
                    {"role": "user", "content": prompt}
                ],
                temperature=0.2,
                max_tokens=8192,
                response_format={"type": "json_object"}
            )

            content = response.choices[0].message.content or ""
            result_json = extract_json_payload(content)

            # Extract token metrics if available
            usage = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                "total_tokens": getattr(response.usage, "total_tokens", 0),
            }
            logger.info(f"Task {task_id} completed. Tokens used: {usage['total_tokens']}")

            return {
                "task_id": task_id,
                "output": result_json,
                "usage": usage,
                "model": model,
                "status": "success"
            }

        except RateLimitError as e:
            attempt += 1
            if attempt >= max_retries:
                logger.error(f"Rate limit exceeded for task {task_id} after {max_retries} attempts.")
                raise
            logger.warning(f"Groq RateLimit encountered for task {task_id}. Sleeping {backoff:.1f}s before retry... ({e})")
            await asyncio.sleep(backoff)
            backoff *= 2.0

        except APIError as e:
            logger.error(f"Groq API error on task {task_id}: {e}")
            raise

        except Exception as e:
            logger.error(f"Unexpected error processing task {task_id}: {e}")
            raise
