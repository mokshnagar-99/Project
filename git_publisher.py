"""
Git publishing module for automatically creating repositories and pushing generated code.
Ensures PAT (Personal Access Token) credentials are sanitized and never logged.
"""

import os
import subprocess
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("git_publisher")

def sanitize_url(url: str) -> str:
    """Mask credentials in URL strings for safe logging."""
    if "@" in url and "://" in url:
        scheme, rest = url.split("://", 1)
        _, host_path = rest.split("@", 1)
        return f"{scheme}://***@{host_path}"
    return url

def publish_to_git(
    repo_dir: str,
    job_id: str,
    commit_message: Optional[str] = None,
    repo_name: Optional[str] = None
) -> Dict[str, Any]:
    """
    Initialize Git repository in repo_dir, commit all files, and optionally push to remote.
    Credentials and settings are fetched from environment variables.
    """
    if commit_message is None:
        commit_message = f"Auto-generated codebase for job {job_id}"
    if repo_name is None:
        repo_name = f"agent-job-{job_id[:8]}"

    git_pat = os.getenv("GIT_PAT")
    git_host = os.getenv("GIT_HOST", "github.com")
    git_org = os.getenv("GIT_ORG_OR_USER") or os.getenv("GIT_USER", "user")

    def run_cmd(cmd: list, cwd: str = repo_dir) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True
        )

    try:
        # 1. Initialize local repository if not already initialized
        if not os.path.exists(os.path.join(repo_dir, ".git")):
            run_cmd(["git", "init", "-b", "main"])
            run_cmd(["git", "config", "user.name", os.getenv("GIT_USER", "GroqAgent")])
            run_cmd(["git", "config", "user.email", "agent@groq-pipeline.local"])

        # 2. Stage and commit files
        run_cmd(["git", "add", "."])
        
        # Check if there are changes to commit
        status = run_cmd(["git", "status", "--porcelain"]).stdout.strip()
        if status:
            run_cmd(["git", "commit", "-m", commit_message])
            logger.info(f"Committed changes for job {job_id}")
        else:
            logger.info(f"No changes to commit for job {job_id}")

        # Get latest commit hash
        commit_hash = run_cmd(["git", "rev-parse", "HEAD"]).stdout.strip()

        # 3. Remote push (only if PAT and remote host are provided)
        remote_url_public = f"https://{git_host}/{git_org}/{repo_name}.git"
        if git_pat and git_pat.strip() and not git_pat.startswith("ghp_your"):
            authenticated_url = f"https://{git_pat}@{git_host}/{git_org}/{repo_name}.git"
            try:
                # Add or set remote origin
                remotes = run_cmd(["git", "remote"]).stdout.splitlines()
                if "origin" in remotes:
                    run_cmd(["git", "remote", "set-url", "origin", authenticated_url])
                else:
                    run_cmd(["git", "remote", "add", "origin", authenticated_url])

                # Push to remote main
                run_cmd(["git", "push", "-u", "origin", "main", "--force"])
                logger.info(f"Successfully pushed code to {sanitize_url(authenticated_url)}")
                return {
                    "success": True,
                    "commit": commit_hash,
                    "repo_url": remote_url_public,
                    "published_remote": True
                }
            except subprocess.CalledProcessError as err:
                logger.warning(f"Git remote push failed (local commit preserved): {err.stderr.strip()}")
                return {
                    "success": True,
                    "commit": commit_hash,
                    "repo_url": remote_url_public,
                    "published_remote": False,
                    "error": "Failed to push to remote. Verify repository exists and PAT has push permissions."
                }
        else:
            logger.info(f"Git PAT not set. Code committed locally in {repo_dir}")
            return {
                "success": True,
                "commit": commit_hash,
                "repo_url": None,
                "published_remote": False
            }

    except Exception as e:
        logger.error(f"Git publication error for job {job_id}: {e}")
        return {
            "success": False,
            "commit": None,
            "repo_url": None,
            "published_remote": False,
            "error": str(e)
        }
