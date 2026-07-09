import unittest
from unittest.mock import patch

from buildforme.github_client import GitHubClient, _repo_path


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

    def test_url_encodes_query(self):
        client = GitHubClient(api_base="https://api.example.test")
        url = client._url("/repos/a/b/issues", {"state": "open", "per_page": "10"})
        self.assertTrue(url.startswith("https://api.example.test/repos/a/b/issues?"))
        self.assertIn("state=open", url)
        self.assertIn("per_page=10", url)


if __name__ == "__main__":
    unittest.main()
