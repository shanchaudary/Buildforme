import json
import unittest
from io import BytesIO
from unittest.mock import patch

from buildforme.github_client import GitHubClient, GitHubClientError, _normalize_ci, _repo_path


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def read(self):
        if isinstance(self._payload, (bytes, bytearray)):
            return self._payload
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class GitHubClientTests(unittest.TestCase):
    def test_repo_path_accepts_owner_name(self):
        self.assertEqual(_repo_path("shanchaudary/Buildforme"), "shanchaudary/Buildforme")

    def test_repo_path_accepts_github_url(self):
        self.assertEqual(_repo_path("https://github.com/shanchaudary/Buildforme"), "shanchaudary/Buildforme")

    def test_repo_path_rejects_invalid_value(self):
        with self.assertRaises(ValueError):
            _repo_path("Buildforme")

    def test_token_header_is_internal_only(self):
        client = GitHubClient(token="secret-token")
        headers = client._headers()
        self.assertEqual(headers["Authorization"], "Bearer secret-token")
        self.assertIn("User-Agent", headers)

    @patch.dict("os.environ", {"BUILDFORME_GITHUB_TOKEN": "token-from-env"}, clear=True)
    def test_from_env_uses_buildforme_token(self):
        client = GitHubClient.from_env()
        self.assertEqual(client.token, "token-from-env")

    def test_missing_token_does_not_crash(self):
        client = GitHubClient.from_env() if False else GitHubClient(token=None)
        headers = client._headers()
        self.assertNotIn("Authorization", headers)

    def test_url_encodes_query(self):
        client = GitHubClient(api_base="https://api.example.test")
        url = client._url("/repos/a/b/issues", {"state": "open", "per_page": "10"})
        self.assertTrue(url.startswith("https://api.example.test/repos/a/b/issues?"))
        self.assertIn("state=open", url)
        self.assertIn("per_page=10", url)

    @patch("urllib.request.urlopen")
    def test_list_pull_requests_normal_response(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse(
            [
                {
                    "number": 1,
                    "title": "Add MVP",
                    "state": "open",
                    "draft": False,
                    "html_url": "https://github.com/a/b/pull/1",
                    "body": "hello",
                    "head": {"ref": "feature", "sha": "abc"},
                    "base": {"ref": "main"},
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            ]
        )
        client = GitHubClient(api_base="https://api.example.test", token=None)
        pulls = client.list_pull_requests("a/b")
        self.assertEqual(len(pulls), 1)
        self.assertEqual(pulls[0]["number"], 1)
        self.assertEqual(pulls[0]["head_sha"], "abc")

    @patch("urllib.request.urlopen")
    def test_list_issues_normal_response(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse(
            [
                {
                    "number": 7,
                    "title": "Docs fix",
                    "state": "open",
                    "html_url": "https://github.com/a/b/issues/7",
                    "body": "docs",
                    "labels": [{"name": "docs"}],
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-02T00:00:00Z",
                }
            ]
        )
        client = GitHubClient(api_base="https://api.example.test")
        issues = client.list_issues("a/b")
        self.assertEqual(issues[0]["number"], 7)
        self.assertEqual(issues[0]["labels"], ["docs"])
        self.assertFalse(issues[0]["is_pull_request"])

    @patch("urllib.request.urlopen")
    def test_pr_files_response(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse(
            [{"filename": "docs/a.md", "status": "modified", "additions": 1, "deletions": 0, "changes": 1}]
        )
        client = GitHubClient(api_base="https://api.example.test")
        files = client.list_pull_request_files("a/b", 1)
        self.assertEqual(files[0]["filename"], "docs/a.md")

    @patch("urllib.request.urlopen")
    def test_404_handled(self, mock_urlopen):
        import urllib.error

        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.example.test",
            code=404,
            msg="not found",
            hdrs=None,
            fp=BytesIO(b'{"message":"Not Found"}'),
        )
        client = GitHubClient(api_base="https://api.example.test")
        with self.assertRaises(GitHubClientError) as ctx:
            client.get_repo("a/b")
        self.assertIn("404", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_403_rate_limit_handled(self, mock_urlopen):
        import urllib.error

        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://api.example.test",
            code=403,
            msg="forbidden",
            hdrs=None,
            fp=BytesIO(b'{"message":"API rate limit exceeded"}'),
        )
        client = GitHubClient(api_base="https://api.example.test")
        with self.assertRaises(GitHubClientError) as ctx:
            client.get_repo("a/b")
        self.assertIn("403", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_malformed_json_handled(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse(b"not-json{")
        client = GitHubClient(api_base="https://api.example.test")
        with self.assertRaises(GitHubClientError) as ctx:
            client.get_repo("a/b")
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_normalize_ci_unknown_when_empty(self):
        result = _normalize_ci(statuses=[], check_runs=[], notes=[])
        self.assertEqual(result["status"], "unknown")
        self.assertNotEqual(result["status"], "passing")

    def test_normalize_ci_failing(self):
        result = _normalize_ci(
            statuses=[{"name": "ci", "state": "failure"}],
            check_runs=[],
            notes=[],
        )
        self.assertEqual(result["status"], "failing")

    def test_normalize_ci_passing(self):
        result = _normalize_ci(
            statuses=[],
            check_runs=[{"name": "tests", "status": "completed", "conclusion": "success"}],
            notes=[],
        )
        self.assertEqual(result["status"], "passing")


if __name__ == "__main__":
    unittest.main()
