# Production-Ready Groq Multi-Agent Code Generation Pipeline

An industrial-grade, token-aware multi-agent software engineering pipeline powered by **Groq**, **Redis**, and **FastAPI**, with **Prometheus** observability, automated **Git publishing**, and file materialization.

Designed to operate reliably and safely inside the **Groq Free Tier limits** while scaling smoothly to high throughput.

---

## 🏗️ Architecture

```mermaid
flowchart TD
    subgraph Ingestion
        CLI["CLI (submit_job.py)"] -->|LPUSH job_queue| RQ[("Redis: job_queue")]
        API["FastAPI (api_server.py)"] -->|LPUSH job_queue| RQ
    end

    subgraph Orchestrator & Monitoring
        RQ -->|Worker Pool (BLPOP)| ORCH["orchestrator.py"]
        ORCH -->|Metrics :9090| PROM["Prometheus"]
        PROM --> GRAF["Grafana (:3000)"]
    end

    subgraph Execution & LLM
        ORCH -->|Rate limit & Token Budget Gate| AGENT["agent.py"]
        AGENT -->|Chat Completions (JSON Mode)| GROQ["Groq Cloud API"]
        GROQ -->|Codebase JSON + Usage| AGENT
        AGENT -->|Status + Output + Tokens| RSTORE[("Redis: job:{id}")]
    end

    subgraph Delivery & Artifacts
        AGENT -->|Auto-Publish if GIT_PAT| GIT["git_publisher.py"]
        GIT --> REMOTE["GitHub / GitLab Remote Repo"]
        RSTORE -->|Materialize| MAT["materialize.py"]
        MAT --> DISK["./generated/{job_id}/"]
        RSTORE -->|GET /jobs/{id}/files| API
    end
```

---

## 📁 Repository Structure

```
├── agent.py               # Groq LLM agent with JSON mode, retries & token tracking
├── orchestrator.py        # Redis worker pool, token-bucket rate limiter, Prometheus metrics
├── submit_job.py          # CLI command to submit generation requests & watch live
├── materialize.py         # Extracts and writes codebase files & README to disk
├── git_publisher.py       # Auto-initializes Git repos and safely pushes to remote with PAT
├── api_server.py          # Production FastAPI REST API (POST /jobs, GET /jobs/{id})
├── prometheus.yml         # Prometheus scrape configuration for orchestrator metrics
├── Dockerfile             # Multi-stage Python 3.12 container
├── docker-compose.yml     # Complete 1-click stack (Redis, Orchestrator, API, Prom, Grafana)
├── requirements.txt       # Python dependencies
├── .env.example           # Configuration template
├── test_pipeline.py       # Automated unit & integration tests
└── README.md              # Project documentation
```

---

## ⚡ Quick Start

### 1️⃣ Configure Environment

Copy the template and set your Groq API key:

```bash
cp .env.example .env
```

Edit `.env` and set your key from [Groq Console](https://console.groq.com/keys):
```ini
GROQ_API_KEY=gsk_your_actual_groq_api_key_here
GROQ_MODEL=llama-3.3-70b-versatile
```

---

### 2️⃣ Option A: Run via Docker Compose (Recommended)

Start the complete stack (Redis, Orchestrator, FastAPI, Prometheus, and Grafana) with one command:

```bash
docker compose up --build
```

**Services available:**
* **FastAPI Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)
* **Prometheus**: [http://localhost:9091](http://localhost:9091)
* **Grafana**: [http://localhost:3000](http://localhost:3000) (Login: `admin` / `admin`)
* **Orchestrator Metrics**: [http://localhost:9090/metrics](http://localhost:9090/metrics)

---

### 3️⃣ Option B: Run Locally with Python

#### Step 1: Install Dependencies
```bash
python -m venv .venv
# On Windows:
.venv\Scripts\activate
# On Linux/macOS:
source .venv/bin/activate

pip install -r requirements.txt
```

#### Step 2: Start Redis
```bash
docker run -d --name redis -p 6379:6379 redis:7-alpine
```

#### Step 3: Start the Orchestrator
```bash
python orchestrator.py
```

#### Step 4: (Optional) Start the FastAPI Server
In another terminal:
```bash
uvicorn api_server:app --host 0.0.0.0 --port 8000 --reload
```

---

## 🚀 Submitting Code Generation Jobs

### Method 1: CLI (submit_job.py)

Submit a task and watch progress until files are unpacked directly into `./generated/<job_id>/`:

```bash
python submit_job.py webapp "Create a lightweight FastAPI microservice for user authentication with JWT" --watch
```

Or enqueue asynchronously without waiting:

```bash
python submit_job.py cli "Build a click CLI tool that converts CSV to JSON"
```

To extract the codebase files later:
```bash
python materialize.py <job_id>
```

---

### Method 2: REST API (FastAPI)

#### Submit a Job:
```bash
curl -X POST http://localhost:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "type": "webapp",
    "description": "A responsive React + Vite landing page for a SaaS product with Tailwind CSS"
  }'
```

Response:
```json
{
  "job_id": "7f09c629-9e8a-4d43-9877-c917bf394a11",
  "status": "queued",
  "submitted_at": "2026-09-19T10:20:00.000Z"
}
```

#### Inspect Job Status:
```bash
curl http://localhost:8000/jobs/7f09c629-9e8a-4d43-9877-c917bf394a11
```

#### Fetch Generated Files JSON:
```bash
curl http://localhost:8000/jobs/7f09c629-9e8a-4d43-9877-c917bf394a11/files
```

---

## 🔒 Automated Git-Backed Artifact Store

To have every generated project automatically committed and pushed to a remote GitHub / GitLab repository:

1. In `.env`, set:
   ```ini
   GIT_AUTO_PUBLISH=true
   GIT_HOST=github.com
   GIT_USER=your_github_username
   GIT_PAT=ghp_your_personal_access_token_here
   GIT_ORG_OR_USER=your_github_username
   ```
2. The orchestrator will automatically:
   - Create local git commit with the generated files
   - Push to `https://github.com/your_username/agent-job-<short_id>.git`
   - Store the public repository link in Redis and FastAPI responses
   - Mask the Personal Access Token (`PAT`) from all logs and standard outputs

---

## 📊 Observability & Metrics

The orchestrator exports Prometheus metrics on port `9090`:
- `groq_jobs_total`: Total jobs dispatched
- `groq_jobs_success`: Completed jobs count
- `groq_jobs_failed`: Failed jobs count
- `groq_tokens_used`: Cumulative LLM tokens consumed
- `groq_queue_depth`: Current pending tasks waiting in Redis
- `groq_active_workers`: Jobs actively processing right now

Inspect raw metrics at: `http://localhost:9090/metrics`.

---

## 🛡️ Staying Inside the Free Groq Quota

1. **Model Selection**: Default to `llama-3.3-70b-versatile` or `llama-3.1-8b-instant`. Both offer extremely fast token generation and robust JSON formatting.
2. **Token Bucket Rate Limiting**: The orchestrator checks the rolling 60-second token budget (`TOKEN_BUDGET_PER_MIN=60000`) before launching each task, preventing HTTP 429 quota exhaustion.
3. **Low Temperature (0.2)**: Encourages deterministic, concise, bug-free code generation without wasted tokens.
4. **Native JSON Object Mode**: Uses `response_format={"type": "json_object"}` to eliminate conversational filler and preamble tokens.
5. **Backoff Retries**: If a transient 429 rate limit is reached, `agent.py` automatically performs exponential backoff before failing.
