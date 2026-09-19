"""
FastAPI HTTP Interface for the Groq multi-agent code generation pipeline.
Endpoints:
  POST /jobs             - Enqueue a new generation task
  GET  /jobs/{job_id}     - Check job execution status and metadata
  GET  /jobs/{job_id}/files - Retrieve generated file structure and README
  GET  /health           - Service and Redis health check
"""

import os
import json
import uuid
import io
import zipfile
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
import redis.asyncio as aioredis
from dotenv import load_dotenv

load_dotenv()

# Redis connection management
redis_client: Optional[aioredis.Redis] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    redis_url = os.getenv("REDIS_URL")
    if redis_url:
        redis_client = aioredis.from_url(redis_url)
    else:
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
    yield
    if redis_client:
        await redis_client.close()

app = FastAPI(
    title="Groq Multi-Agent Pipeline API",
    description="REST API to submit and inspect AI-generated codebases powered by Groq and Redis",
    version="1.0.0",
    lifespan=lifespan
)

class JobSubmitRequest(BaseModel):
    type: str = Field(..., description="Project type (e.g. webapp, api, cli, microservice)")
    description: str = Field(..., description="Natural language specification of what to generate")

class JobSubmitResponse(BaseModel):
    job_id: str
    status: str
    submitted_at: str

class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    type: str
    description: str
    submitted_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    tokens: Optional[str] = None
    error: Optional[str] = None
    git_repo: Optional[str] = None

@app.get("/health", tags=["Health"])
async def health():
    try:
        await redis_client.ping()
        return {"status": "ok", "redis": "connected"}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Redis connection failed: {e}")

@app.post("/jobs", response_model=JobSubmitResponse, status_code=status.HTTP_202_ACCEPTED, tags=["Jobs"])
async def submit_job(req: JobSubmitRequest):
    job_id = str(uuid.uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()

    payload = {
        "id": job_id,
        "type": req.type,
        "description": req.description,
        "submitted_at": now_iso
    }

    # Store initial state in Redis
    await redis_client.hset(
        f"job:{job_id}",
        mapping={
            "id": job_id,
            "type": req.type,
            "description": req.description,
            "status": "queued",
            "submitted_at": now_iso
        }
    )

    # Push to queue
    await redis_client.lpush("job_queue", json.dumps(payload))

    return JobSubmitResponse(
        job_id=job_id,
        status="queued",
        submitted_at=now_iso
    )

@app.get("/jobs/{job_id}", response_model=JobStatusResponse, tags=["Jobs"])
async def get_job_status(job_id: str):
    data = await redis_client.hgetall(f"job:{job_id}")
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    decoded = {k.decode("utf-8"): v.decode("utf-8") for k, v in data.items()}

    return JobStatusResponse(
        job_id=job_id,
        status=decoded.get("status", "unknown"),
        type=decoded.get("type", ""),
        description=decoded.get("description", ""),
        submitted_at=decoded.get("submitted_at"),
        started_at=decoded.get("started_at"),
        completed_at=decoded.get("completed_at"),
        tokens=decoded.get("tokens"),
        error=decoded.get("error"),
        git_repo=decoded.get("git_repo")
    )

@app.get("/jobs/{job_id}/files", tags=["Jobs"])
async def get_job_files(job_id: str):
    data = await redis_client.hgetall(f"job:{job_id}")
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    decoded = {k.decode("utf-8"): v.decode("utf-8") for k, v in data.items()}
    job_status = decoded.get("status")

    if job_status != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Job is not completed yet. Current status: '{job_status}'"
        )

    raw_output = decoded.get("output")
    if not raw_output:
        raise HTTPException(status_code=500, detail="Job completed but output is empty")

    try:
        output_json = json.loads(raw_output)
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Invalid JSON stored in job output")

    return {
        "job_id": job_id,
        "tokens": decoded.get("tokens"),
        "model": decoded.get("model"),
        "structure": output_json.get("structure", {}),
        "readme": output_json.get("readme", "")
    }

@app.get("/jobs/{job_id}/zip", tags=["Jobs"])
async def download_job_zip(job_id: str):
    data = await redis_client.hgetall(f"job:{job_id}")
    if not data:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    decoded = {k.decode("utf-8"): v.decode("utf-8") for k, v in data.items()}
    if decoded.get("status") != "completed":
        raise HTTPException(status_code=400, detail="Job is not completed yet")

    try:
        output_json = json.loads(decoded.get("output", "{}"))
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="Corrupted output")

    structure = output_json.get("structure", {})
    readme = output_json.get("readme", "")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for file_path, content in structure.items():
            clean_path = os.path.normpath(file_path).lstrip("/\\")
            zip_file.writestr(clean_path, content)
        if readme and "README.md" not in structure:
            zip_file.writestr("README.md", readme)

    zip_buffer.seek(0)
    filename = f"codebase-{job_id[:8]}.zip"
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.get("/", response_class=HTMLResponse, tags=["UI"])
async def web_ui():
    """Interactive dashboard UI to submit jobs, preview code, and download archives."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Groq Multi-Agent Studio</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/themes/prism-tomorrow.min.css" />
  <script src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/prism.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/plugins/autoloader/prism-autoloader.min.js"></script>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen flex flex-col font-sans">
  <header class="border-b border-slate-800 bg-slate-900/60 backdrop-blur px-6 py-4 flex items-center justify-between sticky top-0 z-30">
    <div class="flex items-center space-x-3">
      <div class="w-9 h-9 rounded-xl bg-gradient-to-tr from-amber-500 to-orange-600 flex items-center justify-center font-bold text-lg text-slate-950 shadow-lg shadow-orange-500/20">⚡</div>
      <div>
        <h1 class="text-lg font-bold tracking-tight text-white">Groq Agent Pipeline</h1>
        <p class="text-xs text-slate-400">Production-Ready Multi-Agent Code Generation</p>
      </div>
    </div>
    <div class="flex items-center space-x-4">
      <a href="/docs" target="_blank" class="text-xs text-slate-400 hover:text-slate-200 underline">API Docs</a>
      <div id="conn-badge" class="flex items-center space-x-2 text-xs bg-emerald-950/60 text-emerald-400 border border-emerald-800/80 px-3 py-1.5 rounded-full">
        <span class="w-2 h-2 rounded-full bg-emerald-400 animate-pulse"></span>
        <span>Redis Connected</span>
      </div>
    </div>
  </header>

  <main class="flex-1 max-w-7xl w-full mx-auto p-6 grid grid-cols-1 lg:grid-cols-12 gap-6">
    <!-- Left Column: Submit & History -->
    <div class="lg:col-span-5 space-y-6">
      <div class="bg-slate-900 border border-slate-800 rounded-2xl p-5 shadow-xl">
        <h2 class="text-sm font-semibold text-slate-200 uppercase tracking-wider mb-4 flex items-center space-x-2">
          <span>✨</span><span>New Code Generation Task</span>
        </h2>
        <form id="job-form" class="space-y-4">
          <div>
            <label class="block text-xs font-medium text-slate-400 mb-1.5">Project Type</label>
            <select id="task-type" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm text-slate-200 focus:outline-none focus:border-amber-500">
              <option value="webapp">Full-stack Web App</option>
              <option value="api">FastAPI / Express REST API</option>
              <option value="cli">Python CLI Utility</option>
              <option value="microservice">Dockerized Microservice</option>
              <option value="react">React / Next.js Component Library</option>
            </select>
          </div>
          <div>
            <label class="block text-xs font-medium text-slate-400 mb-1.5">System Specification</label>
            <textarea id="task-desc" rows="4" required placeholder="Describe what you want the agent to build in detail..." class="w-full bg-slate-950 border border-slate-700 rounded-lg p-3 text-sm text-slate-200 placeholder-slate-500 focus:outline-none focus:border-amber-500"></textarea>
          </div>
          <button type="submit" id="submit-btn" class="w-full bg-gradient-to-r from-amber-500 to-orange-600 hover:from-amber-400 hover:to-orange-500 text-slate-950 font-semibold py-2.5 px-4 rounded-lg text-sm transition flex items-center justify-center space-x-2 shadow-lg shadow-orange-500/20">
            <span>Dispatch to Groq Pipeline</span>
            <span id="btn-spinner" class="hidden animate-spin">⏳</span>
          </button>
        </form>
      </div>

      <div class="bg-slate-900 border border-slate-800 rounded-2xl p-5 shadow-xl">
        <h3 class="text-sm font-semibold text-slate-200 uppercase tracking-wider mb-3">Recent Jobs</h3>
        <div id="jobs-list" class="space-y-2 max-h-72 overflow-y-auto pr-1">
          <p class="text-xs text-slate-500 italic">No recent jobs in this session yet.</p>
        </div>
      </div>
    </div>

    <!-- Right Column: Inspector & Code Explorer -->
    <div class="lg:col-span-7 space-y-6">
      <div id="job-detail-panel" class="bg-slate-900 border border-slate-800 rounded-2xl p-5 shadow-xl min-h-[560px] flex flex-col">
        <div id="empty-state" class="m-auto text-center py-16">
          <div class="text-4xl mb-3">⚡</div>
          <h3 class="text-base font-medium text-slate-300">No Job Selected</h3>
          <p class="text-xs text-slate-500 mt-1 max-w-sm mx-auto">Submit a project on the left or select a previous job to view generated files, README, and token usage.</p>
        </div>

        <div id="active-state" class="hidden flex-1 flex flex-col space-y-4">
          <div class="flex items-start justify-between border-b border-slate-800 pb-4">
            <div>
              <div class="flex items-center space-x-2">
                <span id="badge-status" class="px-2.5 py-1 text-xs font-semibold rounded-full bg-amber-950/70 text-amber-400 border border-amber-800">queued</span>
                <span id="badge-tokens" class="text-xs text-slate-400">Tokens: -</span>
              </div>
              <h3 id="detail-id" class="text-xs font-mono text-slate-400 mt-1.5">ID: -</h3>
            </div>
            <a id="btn-download-zip" href="#" class="hidden bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-medium py-2 px-3.5 rounded-lg border border-slate-700 transition flex items-center space-x-1.5">
              <span>📦</span>
              <span>Download ZIP</span>
            </a>
          </div>

          <div class="flex-1 grid grid-cols-12 gap-3 min-h-[380px]">
            <!-- File Tree -->
            <div class="col-span-4 border border-slate-800 rounded-xl bg-slate-950 p-2.5 overflow-y-auto max-h-[460px]">
              <div class="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-2 px-1">Files</div>
              <ul id="file-list" class="space-y-1 text-xs"></ul>
            </div>
            <!-- File Code Viewer -->
            <div class="col-span-8 border border-slate-800 rounded-xl bg-slate-950 flex flex-col overflow-hidden">
              <div class="px-3 py-2 border-b border-slate-800 text-xs font-mono text-slate-300 flex justify-between items-center bg-slate-900/50">
                <span id="active-filename">Select a file</span>
              </div>
              <pre class="flex-1 p-3 overflow-auto text-xs m-0 font-mono bg-transparent"><code id="code-content" class="language-python"># Code preview will display here...</code></pre>
            </div>
          </div>
        </div>
      </div>
    </div>
  </main>

  <script>
    let recentJobs = JSON.parse(localStorage.getItem("groq_jobs") || "[]");
    let currentJobId = null;
    let currentFiles = {};
    let pollInterval = null;

    function saveJob(id, type, desc) {
      if (!recentJobs.find(j => j.id === id)) {
        recentJobs.unshift({ id, type, desc, status: "queued", time: new Date().toLocaleTimeString() });
        localStorage.setItem("groq_jobs", JSON.stringify(recentJobs.slice(0, 15)));
        renderRecentJobs();
      }
    }

    function renderRecentJobs() {
      const container = document.getElementById("jobs-list");
      if (!recentJobs.length) {
        container.innerHTML = `<p class="text-xs text-slate-500 italic">No recent jobs yet.</p>`;
        return;
      }
      container.innerHTML = recentJobs.map(j => `
        <div onclick="selectJob('${j.id}')" class="p-2.5 rounded-lg border ${j.id === currentJobId ? 'border-amber-500 bg-amber-950/20' : 'border-slate-800 bg-slate-950/50 hover:border-slate-700'} cursor-pointer transition">
          <div class="flex items-center justify-between text-xs">
            <span class="font-medium text-slate-200 capitalize">${j.type}</span>
            <span class="text-[10px] px-2 py-0.5 rounded-full ${j.status === 'completed' ? 'bg-emerald-950 text-emerald-400 border border-emerald-800' : j.status === 'failed' ? 'bg-red-950 text-red-400' : 'bg-amber-950 text-amber-400 animate-pulse'}">${j.status}</span>
          </div>
          <div class="text-[11px] text-slate-400 truncate mt-1">${j.desc}</div>
        </div>
      `).join("");
    }

    async function selectJob(id) {
      currentJobId = id;
      renderRecentJobs();
      document.getElementById("empty-state").classList.add("hidden");
      document.getElementById("active-state").classList.remove("hidden");
      document.getElementById("detail-id").innerText = `ID: ${id}`;
      pollJobStatus(id);
    }

    async function pollJobStatus(id) {
      if (pollInterval) clearInterval(pollInterval);

      const check = async () => {
        try {
          const res = await fetch(`/jobs/${id}`);
          if (!res.ok) return;
          const data = await res.json();
          
          const statusBadge = document.getElementById("badge-status");
          statusBadge.innerText = data.status;
          if (data.status === "completed") {
            statusBadge.className = "px-2.5 py-1 text-xs font-semibold rounded-full bg-emerald-950/80 text-emerald-400 border border-emerald-700";
            document.getElementById("badge-tokens").innerText = `Tokens: ${data.tokens || 'N/A'}`;
            document.getElementById("btn-download-zip").classList.remove("hidden");
            document.getElementById("btn-download-zip").href = `/jobs/${id}/zip`;
            loadJobFiles(id);
            clearInterval(pollInterval);
          } else if (data.status === "failed") {
            statusBadge.className = "px-2.5 py-1 text-xs font-semibold rounded-full bg-red-950/80 text-red-400 border border-red-700";
            clearInterval(pollInterval);
          } else {
            statusBadge.className = "px-2.5 py-1 text-xs font-semibold rounded-full bg-amber-950/80 text-amber-400 border border-amber-700 animate-pulse";
          }

          // Update local storage status
          const idx = recentJobs.findIndex(j => j.id === id);
          if (idx !== -1 && recentJobs[idx].status !== data.status) {
            recentJobs[idx].status = data.status;
            localStorage.setItem("groq_jobs", JSON.stringify(recentJobs));
            renderRecentJobs();
          }
        } catch (e) {
          console.error(e);
        }
      };

      await check();
      pollInterval = setInterval(check, 2000);
    }

    async function loadJobFiles(id) {
      try {
        const res = await fetch(`/jobs/${id}/files`);
        if (!res.ok) return;
        const data = await res.json();
        currentFiles = data.structure || {};
        if (data.readme) currentFiles["README.md"] = data.readme;

        const listEl = document.getElementById("file-list");
        const paths = Object.keys(currentFiles);
        if (!paths.length) {
          listEl.innerHTML = `<li class="text-slate-500 italic">No files generated</li>`;
          return;
        }

        listEl.innerHTML = paths.map((p, idx) => `
          <li onclick="showFileContent('${p}')" class="p-1.5 rounded cursor-pointer truncate hover:bg-slate-800 text-slate-300 ${idx === 0 ? 'bg-slate-800/80 text-amber-300 font-medium' : ''}">
            📄 ${p}
          </li>
        `).join("");

        showFileContent(paths[0]);
      } catch (e) {
        console.error(e);
      }
    }

    function showFileContent(filePath) {
      document.getElementById("active-filename").innerText = filePath;
      const content = currentFiles[filePath] || "";
      const codeEl = document.getElementById("code-content");
      codeEl.textContent = content;
      Prism.highlightElement(codeEl);
    }

    document.getElementById("job-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const type = document.getElementById("task-type").value;
      const desc = document.getElementById("task-desc").value;
      const btn = document.getElementById("submit-btn");
      const spinner = document.getElementById("btn-spinner");

      btn.disabled = true;
      spinner.classList.remove("hidden");

      try {
        const res = await fetch("/jobs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ type, description: desc })
        });
        if (!res.ok) throw new Error("Failed to submit job");
        const data = await res.json();
        saveJob(data.job_id, type, desc);
        selectJob(data.job_id);
        document.getElementById("task-desc").value = "";
      } catch (err) {
        alert("Error submitting job: " + err.message);
      } finally {
        btn.disabled = false;
        spinner.classList.add("hidden");
      }
    });

    renderRecentJobs();
  </script>
</body>
</html>"""
