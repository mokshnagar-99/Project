"""
Unit and integration test suite for the Groq multi-agent pipeline components.
Tests JSON parsing, fallback logic, materialization, and URL sanitization.
"""

import os
import json
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from agent import extract_json_payload
from git_publisher import sanitize_url
from materialize import materialize_job

class TestAgentExtraction(unittest.TestCase):
    def test_direct_valid_json(self):
        sample = json.dumps({
            "structure": {"app.py": "print('hello')"},
            "readme": "# Sample App"
        })
        parsed = extract_json_payload(sample)
        self.assertIn("structure", parsed)
        self.assertEqual(parsed["structure"]["app.py"], "print('hello')")
        self.assertEqual(parsed["readme"], "# Sample App")

    def test_markdown_fence_wrapped_json(self):
        sample = """Here is your code:
```json
{
  "structure": {
    "src/index.js": "console.log('hi');",
    "package.json": "{\\"name\\": \\"demo\\"}"
  },
  "readme": "# Demo Project"
}
```
Enjoy your new codebase!"""
        parsed = extract_json_payload(sample)
        self.assertIn("src/index.js", parsed["structure"])
        self.assertEqual(parsed["readme"], "# Demo Project")

    def test_raw_regex_fallback(self):
        sample = """Prefix commentary...
{"structure": {"config.yaml": "debug: true"}, "readme": "Config guide"}
Suffix commentary..."""
        parsed = extract_json_payload(sample)
        self.assertIn("config.yaml", parsed["structure"])

    def test_invalid_json_raises_error(self):
        sample = "This is not JSON at all."
        with self.assertRaises(ValueError):
            extract_json_payload(sample)

class TestGitPublisherSanitize(unittest.TestCase):
    def test_sanitize_url(self):
        sensitive = "https://ghp_secretToken12345@github.com/org/repo.git"
        safe = sanitize_url(sensitive)
        self.assertNotIn("ghp_secretToken12345", safe)
        self.assertIn("***@github.com/org/repo.git", safe)

class TestMaterializer(unittest.IsolatedAsyncioTestCase):
    async def test_materialize_job_to_disk(self):
        temp_dir = tempfile.mkdtemp()
        job_id = "test-job-uuid-123"
        
        # Mock Redis client
        mock_redis = MagicMock()
        mock_output = json.dumps({
            "structure": {
                "src/main.py": "def run(): pass",
                "docs/guide.txt": "Documentation"
            },
            "readme": "# Test Readme"
        })
        mock_redis.hgetall = AsyncMock(return_value={
            b"id": job_id.encode("utf-8"),
            b"status": b"completed",
            b"output": mock_output.encode("utf-8")
        })

        try:
            target_dir = await materialize_job(
                job_id=job_id,
                base_output_dir=temp_dir,
                redis_client=mock_redis
            )

            self.assertTrue(os.path.exists(os.path.join(target_dir, "src", "main.py")))
            self.assertTrue(os.path.exists(os.path.join(target_dir, "docs", "guide.txt")))
            self.assertTrue(os.path.exists(os.path.join(target_dir, "README.md")))

            with open(os.path.join(target_dir, "src", "main.py"), "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "def run(): pass")

        finally:
            shutil.rmtree(temp_dir)

if __name__ == "__main__":
    unittest.main()
