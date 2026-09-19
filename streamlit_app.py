"""
Streamlit Web UI for the Groq Multi-Agent Code Generation Pipeline.
Provides an interactive studio to:
- Generate complete multi-file codebases using Groq LLMs
- Track Redis job queue tasks
- Browse generated file trees and syntax-highlighted code
- Download projects as ZIP archives with 1-click
- Monitor free-tier token usage
"""

import os
import io
import json
import time
import uuid
import zipfile
import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import streamlit as st
from dotenv import load_dotenv

# Load local environment
load_dotenv()

# Set page config
st.set_page_config(
    page_title="Groq Multi-Agent Studio",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# -----------------------------------------------------------------------------
# Redis Helper & Synchronous Fallback Helpers
# -----------------------------------------------------------------------------
def get_redis_client():
    """Attempt synchronous Redis connection."""
    try:
        import redis
        redis_url = os.getenv("REDIS_URL")
        if redis_url:
            r = redis.from_url(redis_url, decode_responses=True, socket_timeout=2)
        else:
            r = redis.Redis(
                host=os.getenv("REDIS_HOST", "localhost"),
                port=int(os.getenv("REDIS_PORT", "6379")),
                db=int(os.getenv("REDIS_DB", "0")),
                password=os.getenv("REDIS_PASSWORD") or None,
                decode_responses=True,
                socket_timeout=2
            )
        r.ping()
        return r
    except Exception:
        return None

def create_zip_archive(structure: Dict[str, str], readme: Optional[str] = None) -> bytes:
    """Create in-memory zip archive from generated code structure."""
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for file_path, content in structure.items():
            clean_path = os.path.normpath(file_path).lstrip("/\\")
            zip_file.writestr(clean_path, content)
        if readme and "README.md" not in structure and "readme.md" not in structure:
            zip_file.writestr("README.md", readme)
    return zip_buffer.getvalue()

def detect_language(filename: str) -> str:
    """Detect syntax highlighting language from file extension."""
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    mapping = {
        "py": "python",
        "js": "javascript",
        "jsx": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "json": "json",
        "html": "html",
        "css": "css",
        "yml": "yaml",
        "yaml": "yaml",
        "sh": "bash",
        "bash": "bash",
        "dockerfile": "dockerfile",
        "sql": "sql",
        "md": "markdown",
        "txt": "text",
        "env": "bash"
    }
    if filename.lower() == "dockerfile":
        return "dockerfile"
    return mapping.get(ext, "text")

# -----------------------------------------------------------------------------
# Sidebar: Settings & System Status
# -----------------------------------------------------------------------------
with st.sidebar:
    st.title("⚡ Groq Agent Studio")
    st.caption("Industrial-grade Multi-Agent Code Generation")
    st.divider()

    st.subheader("🔑 Groq Credentials")
    env_groq_key = os.getenv("GROQ_API_KEY", "")
    api_key_input = st.text_input(
        "Groq API Key",
        value=env_groq_key,
        type="password",
        help="Free key from https://console.groq.com/keys"
    )
    if api_key_input:
        os.environ["GROQ_API_KEY"] = api_key_input

    model_choice = st.selectbox(
        "LLM Model",
        options=[
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768"
        ],
        index=0,
        help="Llama 3.3 70B produces the highest quality code; 8B Instant is fastest."
    )
    os.environ["GROQ_MODEL"] = model_choice

    st.divider()
    st.subheader("📡 Infrastructure Status")

    redis_conn = get_redis_client()
    if redis_conn:
        st.success("🟢 Redis Queue: Connected")
    else:
        st.warning("🟡 Redis Queue: Offline (Direct Generation available)")

    st.caption("ℹ️ When Redis is running, jobs can be queued for background workers. When offline, Streamlit executes the agent directly.")

# -----------------------------------------------------------------------------
# Main Navigation Tabs
# -----------------------------------------------------------------------------
tab_generate, tab_tracker, tab_quota = st.tabs(["🚀 Generate Codebase", "📋 Job Queue Tracker", "🛡️ Quota & Architecture"])

# -----------------------------------------------------------------------------
# TAB 1: GENERATE CODEBASE
# -----------------------------------------------------------------------------
with tab_generate:
    st.markdown("### Generate a Complete Software Project")
    st.write("Specify your requirements and Groq will build a complete directory structure with code, configurations, and documentation.")

    col1, col2 = st.columns([1, 2])
    with col1:
        task_type = st.selectbox(
            "Project Archetype",
            options=[
                ("webapp", "Full-Stack Web Application"),
                ("api", "FastAPI / REST Microservice"),
                ("cli", "Command Line Utility (CLI)"),
                ("react", "React / Next.js Component System"),
                ("microservice", "Dockerized Microservice"),
                ("data_pipeline", "Data Extraction & ETL Pipeline")
            ],
            format_func=lambda x: x[1]
        )[0]

        execution_mode = st.radio(
            "Execution Mode",
            options=["Direct Execution (Instant)", "Push to Redis Queue"] if redis_conn else ["Direct Execution (Instant)"],
            help="Direct mode streams output immediately. Queue mode enqueues for orchestrator workers."
        )

    with col2:
        # Prompt templates helper
        preset = st.selectbox(
            "Quick Templates (Optional)",
            options=[
                "-- Select a template or write your own below --",
                "FastAPI REST API with JWT Auth, SQLite CRUD, Pydantic v2 validation and Pytest tests",
                "Python CLI tool using Click that parses CSV files, applies transformation rules, and exports formatted JSON or YAML",
                "React + Vite Single Page App for task management with Tailwind CSS, local storage persistence, and filter tags",
                "Production Dockerized microservice for web scraping with rate limiting and automated health check endpoints"
            ]
        )

        initial_desc = preset if preset != "-- Select a template or write your own below --" else ""
        description = st.text_area(
            "Detailed Specification",
            value=initial_desc,
            height=140,
            placeholder="e.g. Create a FastAPI microservice that provides endpoints to manage books, includes SQLite database, migration scripts, Dockerfile, and README..."
        )

    submit_btn = st.button("🚀 Generate Codebase", type="primary", use_container_width=True)

    if submit_btn:
        if not os.getenv("GROQ_API_KEY") or os.getenv("GROQ_API_KEY").startswith("gsk_your_groq"):
            st.error("❌ Please provide a valid Groq API Key in the sidebar or in your `.env` file.")
        elif not description.strip():
            st.error("❌ Please enter a specification for the project.")
        else:
            job_id = str(uuid.uuid4())
            task_payload = {
                "id": job_id,
                "type": task_type,
                "description": description
            }

            if execution_mode == "Direct Execution (Instant)":
                with st.spinner("🤖 Groq Agent is generating your production codebase..."):
                    try:
                        from agent import run_task
                        start_time = time.time()
                        result = asyncio.run(run_task(task_payload))
                        elapsed = time.time() - start_time

                        st.session_state["last_result"] = result
                        st.session_state["last_job_id"] = job_id
                        st.session_state["elapsed"] = elapsed

                        # Also store in Redis if online
                        if redis_conn:
                            now_iso = datetime.now(timezone.utc).isoformat()
                            redis_conn.hset(
                                f"job:{job_id}",
                                mapping={
                                    "id": job_id,
                                    "type": task_type,
                                    "description": description,
                                    "status": "completed",
                                    "completed_at": now_iso,
                                    "output": json.dumps(result.get("output", {})),
                                    "tokens": str(result.get("usage", {}).get("total_tokens", 0)),
                                    "model": result.get("model", "")
                                }
                            )

                        st.success(f"✅ Codebase generated in {elapsed:.2f}s!")
                    except Exception as e:
                        st.error(f"❌ Generation failed: {e}")

            else:
                # Enqueue to Redis
                try:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    redis_conn.hset(
                        f"job:{job_id}",
                        mapping={
                            "id": job_id,
                            "type": task_type,
                            "description": description,
                            "status": "queued",
                            "submitted_at": now_iso
                        }
                    )
                    redis_conn.lpush("job_queue", json.dumps(task_payload))
                    st.session_state["tracking_job_id"] = job_id
                    st.info(f"📬 Job enqueued with ID: `{job_id}`! Go to the 'Job Queue Tracker' tab to watch its status.")
                except Exception as e:
                    st.error(f"❌ Failed to enqueue to Redis: {e}")

    # Display generated output if available
    if "last_result" in st.session_state:
        res = st.session_state["last_result"]
        output = res.get("output", {})
        structure = output.get("structure", {})
        readme = output.get("readme", "")
        usage = res.get("usage", {})
        total_tokens = usage.get("total_tokens", "N/A")

        st.divider()
        st.subheader("📦 Generated Artifacts")

        # Stats Cards
        stat1, stat2, stat3, stat4 = st.columns(4)
        stat1.metric("Files Created", len(structure))
        stat2.metric("Total Tokens", total_tokens)
        stat3.metric("Model", res.get("model", model_choice))
        stat4.metric("Status", "Completed", delta="Success")

        # Download ZIP button
        zip_data = create_zip_archive(structure, readme)
        st.download_button(
            label="⬇️ Download Entire Project as ZIP",
            data=zip_data,
            file_name=f"groq-project-{st.session_state.get('last_job_id', 'code')[:8]}.zip",
            mime="application/zip",
            type="primary"
        )

        # File Viewer Layout
        view_col1, view_col2 = st.columns([1, 2])
        all_files = list(structure.keys())
        if readme and "README.md" not in structure:
            all_files.insert(0, "README.md")

        with view_col1:
            st.markdown("#### 📂 Project Structure")
            selected_file = st.selectbox("Select file to inspect:", options=all_files)

        with view_col2:
            st.markdown(f"#### 📄 `{selected_file}`")
            if selected_file == "README.md" and readme and "README.md" not in structure:
                st.markdown(readme)
            else:
                code_content = structure.get(selected_file, "")
                lang = detect_language(selected_file)
                st.code(code_content, language=lang, line_numbers=True)

# -----------------------------------------------------------------------------
# TAB 2: JOB QUEUE TRACKER
# -----------------------------------------------------------------------------
with tab_tracker:
    st.markdown("### 📋 Redis Background Job Tracker")

    if not redis_conn:
        st.warning("Redis is not connected. Start Redis (`docker run -d -p 6379:6379 redis:7-alpine`) or configure `REDIS_HOST`/`REDIS_URL` in `.env`.")
    else:
        # Check queue length
        q_len = redis_conn.llen("job_queue")
        st.metric("Pending Queue Depth", q_len)

        track_id = st.text_input(
            "Enter Job UUID to Inspect",
            value=st.session_state.get("tracking_job_id", "")
        )

        poll_col1, poll_col2 = st.columns([1, 4])
        with poll_col1:
            refresh_btn = st.button("🔄 Check Status")

        if track_id:
            job_data = redis_conn.hgetall(f"job:{track_id}")
            if not job_data:
                st.error(f"Job `{track_id}` not found in Redis.")
            else:
                st.json(job_data)
                status = job_data.get("status")
                if status == "completed":
                    st.success("🎉 Job completed!")
                    raw_out = job_data.get("output")
                    if raw_out:
                        try:
                            parsed = json.loads(raw_out)
                            struct = parsed.get("structure", {})
                            rdme = parsed.get("readme", "")
                            zip_bytes = create_zip_archive(struct, rdme)
                            st.download_button(
                                label="⬇️ Download Completed Project ZIP",
                                data=zip_bytes,
                                file_name=f"job-{track_id[:8]}.zip",
                                mime="application/zip"
                            )
                        except Exception as e:
                            st.error(f"Failed to unpack output: {e}")
                elif status == "failed":
                    st.error(f"❌ Job failed: {job_data.get('error')}")
                else:
                    st.info(f"⏳ Job is currently `{status}`. The orchestrator worker is processing it...")

# -----------------------------------------------------------------------------
# TAB 3: QUOTA & ARCHITECTURE GUIDE
# -----------------------------------------------------------------------------
with tab_quota:
    st.markdown("### 🛡️ Free Tier Quota Management & Architecture")
    st.markdown("""
    #### 💡 Free Quota Optimization Strategies
    1. **Strict JSON Schema Enforcement**: Preambles and conversational filler are suppressed (`response_format={"type": "json_object"}`), saving hundreds of tokens per call.
    2. **Rolling Token-Bucket Rate Limiter**: The orchestrator tracks per-minute token usage (`TOKEN_BUDGET_PER_MIN=60000`) in Redis to stay safely under Groq TPM limits.
    3. **Low Temperature (0.2)**: Encourages deterministic, concise code and eliminates hallucinated retries.
    4. **Exponential Backoff**: If an HTTP 429 rate limit is encountered, the worker automatically pauses and retries with doubling backoff.

    #### 🚢 Deployment Blueprint
    - **Streamlit Community Cloud**: Push to GitHub and deploy directly from [share.streamlit.io](https://share.streamlit.io).
    - **Docker Compose**: Run `docker compose up --build` to launch Redis, Orchestrator, FastAPI, Prometheus, and Grafana.
    """)
