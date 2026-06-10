"""Tests for bulk_post.py — run with: python -m unittest discover tests/"""

import csv
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import bulk_post


# ---------------------------------------------------------------------------
# _get_suggestion
# ---------------------------------------------------------------------------

class TestGetSuggestion(unittest.TestCase):
    def test_slash_matches_first_command(self):
        # "/" is a prefix of all commands; first in list wins
        self.assertEqual(bulk_post._get_suggestion("/"), "pause")

    def test_p_prefix(self):
        self.assertEqual(bulk_post._get_suggestion("/p"), "ause")
        self.assertEqual(bulk_post._get_suggestion("/pa"), "use")

    def test_r_prefix(self):
        self.assertEqual(bulk_post._get_suggestion("/r"), "esume")

    def test_e_prefix(self):
        self.assertEqual(bulk_post._get_suggestion("/e"), "xit")

    def test_exact_match_returns_empty(self):
        self.assertEqual(bulk_post._get_suggestion("/pause"), "")
        self.assertEqual(bulk_post._get_suggestion("/resume"), "")
        self.assertEqual(bulk_post._get_suggestion("/exit"), "")

    def test_no_slash_returns_empty(self):
        self.assertEqual(bulk_post._get_suggestion(""), "")
        self.assertEqual(bulk_post._get_suggestion("hello"), "")

    def test_no_matching_command(self):
        self.assertEqual(bulk_post._get_suggestion("/z"), "")
        self.assertEqual(bulk_post._get_suggestion("/pz"), "")


# ---------------------------------------------------------------------------
# substitute
# ---------------------------------------------------------------------------

class TestSubstitute(unittest.TestCase):
    def test_single_placeholder(self):
        result, err = bulk_post.substitute("https://api.com/{{id}}", {"id": "123"})
        self.assertEqual(result, "https://api.com/123")
        self.assertIsNone(err)

    def test_multiple_placeholders(self):
        result, err = bulk_post.substitute("{{a}}/{{b}}", {"a": "x", "b": "y"})
        self.assertEqual(result, "x/y")
        self.assertIsNone(err)

    def test_repeated_placeholder(self):
        result, err = bulk_post.substitute("{{id}}-{{id}}", {"id": "9"})
        self.assertEqual(result, "9-9")
        self.assertIsNone(err)

    def test_no_placeholders(self):
        result, err = bulk_post.substitute("https://api.com/static", {"id": "1"})
        self.assertEqual(result, "https://api.com/static")
        self.assertIsNone(err)

    def test_missing_column_returns_error(self):
        template = "https://api.com/{{id}}"
        result, err = bulk_post.substitute(template, {})
        self.assertEqual(result, template)
        self.assertIsNotNone(err)
        self.assertIn("id", err)

    def test_extra_columns_are_ignored(self):
        result, err = bulk_post.substitute("{{id}}", {"id": "1", "extra": "ignored"})
        self.assertEqual(result, "1")
        self.assertIsNone(err)

    def test_multiple_missing_columns_listed(self):
        _, err = bulk_post.substitute("{{a}}/{{b}}", {})
        self.assertIn("a", err)
        self.assertIn("b", err)


# ---------------------------------------------------------------------------
# count_csv_rows
# ---------------------------------------------------------------------------

class TestCountCsvRows(unittest.TestCase):
    def _tmp_csv(self, content):
        f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        f.write(content)
        f.close()
        return f.name

    def test_two_data_rows(self):
        path = self._tmp_csv("id,name\n1,alice\n2,bob\n")
        try:
            self.assertEqual(bulk_post.count_csv_rows(path), 2)
        finally:
            os.unlink(path)

    def test_single_data_row(self):
        path = self._tmp_csv("id\n1\n")
        try:
            self.assertEqual(bulk_post.count_csv_rows(path), 1)
        finally:
            os.unlink(path)

    def test_header_only_returns_zero(self):
        path = self._tmp_csv("id,name\n")
        try:
            self.assertEqual(bulk_post.count_csv_rows(path), 0)
        finally:
            os.unlink(path)

    def test_nonexistent_file_returns_zero(self):
        self.assertEqual(bulk_post.count_csv_rows("/tmp/__no_such_file__.csv"), 0)


# ---------------------------------------------------------------------------
# resolve_token
# ---------------------------------------------------------------------------

class TestResolveToken(unittest.TestCase):
    def test_flag_value_returned_directly(self):
        self.assertEqual(bulk_post.resolve_token("my-token"), "my-token")

    def test_flag_overrides_env(self):
        with patch.dict(os.environ, {"BULK_TOKEN": "env-token"}):
            self.assertEqual(bulk_post.resolve_token("flag-token"), "flag-token")

    def test_env_var_used_when_no_flag(self):
        with patch.dict(os.environ, {"BULK_TOKEN": "env-token"}):
            self.assertEqual(bulk_post.resolve_token(None), "env-token")

    def test_env_var_stripped(self):
        with patch.dict(os.environ, {"BULK_TOKEN": "  spaced  "}):
            self.assertEqual(bulk_post.resolve_token(None), "spaced")

    def test_interactive_prompt_when_no_flag_no_env(self):
        with patch.dict(os.environ, {"BULK_TOKEN": ""}), \
             patch("builtins.input", return_value="typed-token"):
            token = bulk_post.resolve_token(None)
        self.assertEqual(token, "typed-token")

    def test_empty_interactive_input_exits(self):
        with patch.dict(os.environ, {"BULK_TOKEN": ""}), \
             patch("builtins.input", return_value=""), \
             patch("builtins.print"), \
             self.assertRaises(SystemExit) as ctx:
            bulk_post.resolve_token(None)
        self.assertEqual(ctx.exception.code, 1)


# ---------------------------------------------------------------------------
# resolve_basic_creds
# ---------------------------------------------------------------------------

class TestResolveBasicCreds(unittest.TestCase):
    def test_flag_value_returned_directly(self):
        self.assertEqual(bulk_post.resolve_basic_creds("user:pass"), "user:pass")

    def test_flag_overrides_env(self):
        with patch.dict(os.environ, {"BULK_USER": "env:creds"}):
            self.assertEqual(bulk_post.resolve_basic_creds("flag:creds"), "flag:creds")

    def test_env_var_used_when_no_flag(self):
        with patch.dict(os.environ, {"BULK_USER": "env:creds"}):
            self.assertEqual(bulk_post.resolve_basic_creds(None), "env:creds")

    def test_env_var_stripped(self):
        with patch.dict(os.environ, {"BULK_USER": "  u:p  "}):
            self.assertEqual(bulk_post.resolve_basic_creds(None), "u:p")

    def test_interactive_prompt_when_no_flag_no_env(self):
        with patch.dict(os.environ, {"BULK_USER": ""}), \
             patch("builtins.input", return_value="typed:creds"):
            creds = bulk_post.resolve_basic_creds(None)
        self.assertEqual(creds, "typed:creds")

    def test_empty_interactive_input_exits(self):
        with patch.dict(os.environ, {"BULK_USER": ""}), \
             patch("builtins.input", return_value=""), \
             patch("builtins.print"), \
             self.assertRaises(SystemExit) as ctx:
            bulk_post.resolve_basic_creds(None)
        self.assertEqual(ctx.exception.code, 1)


# ---------------------------------------------------------------------------
# _validate_body_template
# ---------------------------------------------------------------------------

class TestValidateBodyTemplate(unittest.TestCase):
    def test_valid_json_literal_returns_none(self):
        self.assertIsNone(bulk_post._validate_body_template('{"id":1}', "application/json"))

    def test_valid_json_with_placeholder_returns_none(self):
        self.assertIsNone(bulk_post._validate_body_template('{"id":"{{id}}"}', "application/json"))

    def test_valid_json_unquoted_placeholder_returns_none(self):
        # {{amount}} → null, giving {"amount": null} which is valid JSON
        self.assertIsNone(bulk_post._validate_body_template('{"amount":{{amount}}}', "application/json"))

    def test_invalid_json_template_returns_error(self):
        err = bulk_post._validate_body_template("{bad json {{id}}}", "application/json")
        self.assertIsNotNone(err)
        self.assertIn("Invalid JSON", err)

    def test_valid_xml_with_placeholder_returns_none(self):
        self.assertIsNone(bulk_post._validate_body_template("<item><id>{{id}}</id></item>", "application/xml"))

    def test_invalid_xml_template_returns_error(self):
        err = bulk_post._validate_body_template("<root><unclosed {{id}}>", "application/xml")
        self.assertIsNotNone(err)
        self.assertIn("Invalid XML", err)

    def test_text_xml_content_type(self):
        self.assertIsNone(bulk_post._validate_body_template("<a/>", "text/xml"))

    def test_unknown_content_type_skips_validation(self):
        self.assertIsNone(bulk_post._validate_body_template("not json or xml", "application/x-www-form-urlencoded"))

    def test_json_content_type_case_insensitive(self):
        self.assertIsNone(bulk_post._validate_body_template('{"x":"{{v}}"}', "Application/JSON"))

    def test_empty_body_invalid_json(self):
        self.assertIsNotNone(bulk_post._validate_body_template("", "application/json"))

    def test_empty_body_invalid_xml(self):
        self.assertIsNotNone(bulk_post._validate_body_template("", "application/xml"))


# ---------------------------------------------------------------------------
# http_request
# ---------------------------------------------------------------------------

class TestHttpRequest(unittest.TestCase):
    def _mock_resp(self, status, body=b""):
        m = MagicMock()
        m.status = status
        m.read.return_value = body
        m.headers = {}
        m.__enter__.return_value = m
        m.__exit__.return_value = False
        return m

    def test_200_returns_status_and_body(self):
        with patch("urllib.request.urlopen", return_value=self._mock_resp(200, b'{"ok":true}')):
            status, body, elapsed, *_ = bulk_post.http_request("http://x.com/", "tok", "GET", None)
        self.assertEqual(status, 200)
        self.assertEqual(body, '{"ok":true}')
        self.assertGreaterEqual(elapsed, 0)

    def test_201_success(self):
        with patch("urllib.request.urlopen", return_value=self._mock_resp(201, b"created")):
            status, *_ = bulk_post.http_request("http://x.com/", "tok", "POST", '{}')
        self.assertEqual(status, 201)

    def test_http_error_404(self):
        err = urllib.error.HTTPError("http://x.com/", 404, "Not Found", {}, BytesIO(b"not found"))
        with patch("urllib.request.urlopen", side_effect=err):
            status, body, *_ = bulk_post.http_request("http://x.com/", "tok", "GET", None)
        self.assertEqual(status, 404)
        self.assertEqual(body, "not found")

    def test_http_error_401(self):
        err = urllib.error.HTTPError("http://x.com/", 401, "Unauthorized", {}, BytesIO(b""))
        with patch("urllib.request.urlopen", side_effect=err):
            status, *_ = bulk_post.http_request("http://x.com/", "tok", "GET", None)
        self.assertEqual(status, 401)

    def test_url_error_returns_none_status(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            status, body, *_ = bulk_post.http_request("http://x.com/", "tok", "GET", None)
        self.assertIsNone(status)
        self.assertIn("Connection error", body)

    def test_timeout_error_returns_none_status(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError()):
            status, body, *_ = bulk_post.http_request("http://x.com/", "tok", "GET", None, timeout=5)
        self.assertIsNone(status)
        self.assertIn("timed out", body)
        self.assertIn("5s", body)

    def test_body_sets_content_type_header(self):
        captured = []
        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200)
        with patch("urllib.request.urlopen", side_effect=capture):
            bulk_post.http_request("http://x.com/", "tok", "POST", '{"x":1}')
        self.assertEqual(captured[0].get_header("Content-type"), "application/json")

    def test_no_body_no_content_type_header(self):
        captured = []
        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200)
        with patch("urllib.request.urlopen", side_effect=capture):
            bulk_post.http_request("http://x.com/", "tok", "GET", None)
        self.assertIsNone(captured[0].get_header("Content-type"))

    def test_custom_content_type_used_when_body_present(self):
        captured = []
        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200)
        with patch("urllib.request.urlopen", side_effect=capture):
            bulk_post.http_request("http://x.com/", "tok", "POST", "id=1&v=2",
                                   content_type="application/x-www-form-urlencoded")
        self.assertEqual(captured[0].get_header("Content-type"), "application/x-www-form-urlencoded")

    def test_no_body_ignores_custom_content_type(self):
        captured = []
        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200)
        with patch("urllib.request.urlopen", side_effect=capture):
            bulk_post.http_request("http://x.com/", "tok", "GET", None,
                                   content_type="application/x-www-form-urlencoded")
        self.assertIsNone(captured[0].get_header("Content-type"))

    def test_auth_header_passed_through(self):
        captured = []
        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200)
        with patch("urllib.request.urlopen", side_effect=capture):
            bulk_post.http_request("http://x.com/", "Bearer my-secret", "GET", None)
        self.assertEqual(captured[0].get_header("Authorization"), "Bearer my-secret")

    def test_basic_auth_header_passed_through(self):
        import base64
        creds = base64.b64encode(b"user:pass").decode()
        captured = []
        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200)
        with patch("urllib.request.urlopen", side_effect=capture):
            bulk_post.http_request("http://x.com/", f"Basic {creds}", "GET", None)
        self.assertEqual(captured[0].get_header("Authorization"), f"Basic {creds}")

    def test_none_auth_header_omits_authorization(self):
        captured = []
        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200)
        with patch("urllib.request.urlopen", side_effect=capture):
            bulk_post.http_request("http://x.com/", None, "GET", None)
        self.assertIsNone(captured[0].get_header("Authorization"))


# ---------------------------------------------------------------------------
# _run integration tests
# ---------------------------------------------------------------------------

class TestRun(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_csv(self, filename, rows):
        path = os.path.join(self.tmpdir, filename)
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def _mock_resp(self, status, body=b""):
        m = MagicMock()
        m.status = status
        m.read.return_value = body
        m.headers = {}
        m.__enter__.return_value = m
        m.__exit__.return_value = False
        return m

    def _argv(self, url, csv_path, *extra_flags):
        args = ["bp", "-u", url, "-c", csv_path, "-a", "bearer", "-t", "tok"]
        args += list(extra_flags)
        return args

    def test_all_rows_succeed_no_retry_file(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}, {"id": "2"}])
        retry_path = Path(csv_path).parent / "data_failed.csv"

        with patch("sys.argv", self._argv("http://t.com/{{id}}", csv_path)), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", return_value=self._mock_resp(200, b"ok")), \
             patch("builtins.print"):
            bulk_post._run()

        self.assertFalse(retry_path.exists())

    def test_failed_rows_written_to_retry_file(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}, {"id": "2"}])
        retry_path = Path(csv_path).parent / "data_failed.csv"
        err = urllib.error.HTTPError("http://t.com/1", 500, "Err", {}, BytesIO(b"boom"))

        with patch("sys.argv", self._argv("http://t.com/{{id}}", csv_path)), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", side_effect=err), \
             patch("builtins.print"), \
             self.assertRaises(SystemExit) as ctx:
            bulk_post._run()

        self.assertEqual(ctx.exception.code, 1)
        self.assertTrue(retry_path.exists())
        with open(retry_path) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)

    def test_offset_skips_leading_rows(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}, {"id": "2"}, {"id": "3"}])
        urls_called = []

        def capture(req, timeout=None):
            urls_called.append(req.full_url)
            return self._mock_resp(200, b"ok")

        with patch("sys.argv", self._argv("http://t.com/{{id}}", csv_path, "--offset", "2")), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", side_effect=capture), \
             patch("builtins.print"):
            bulk_post._run()

        self.assertEqual(urls_called, ["http://t.com/3"])

    def test_offset_beyond_rows_exits(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])

        with patch("sys.argv", self._argv("http://t.com/{{id}}", csv_path, "--offset", "5")), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("builtins.print"), \
             self.assertRaises(SystemExit) as ctx:
            bulk_post._run()

        self.assertEqual(ctx.exception.code, 1)

    def test_401_retries_with_new_token(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        err_401 = urllib.error.HTTPError("http://t.com/1", 401, "Unauthorized", {}, BytesIO(b""))
        auth_headers = []

        def capture(req, timeout=None):
            auth_headers.append(req.get_header("Authorization"))
            if len(auth_headers) == 1:
                raise err_401
            return self._mock_resp(200, b"ok")

        with patch("sys.argv", self._argv("http://t.com/{{id}}", csv_path)), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", side_effect=capture), \
             patch("bulk_post.prompt_new_token", return_value="new-tok"), \
             patch("builtins.print"):
            bulk_post._run()

        self.assertEqual(len(auth_headers), 2)
        self.assertEqual(auth_headers[0], "Bearer tok")
        self.assertEqual(auth_headers[1], "Bearer new-tok")

    def test_custom_retry_file_path(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        custom_path = os.path.join(self.tmpdir, "custom_failed.csv")
        err = urllib.error.HTTPError("http://t.com/1", 500, "Err", {}, BytesIO(b""))

        with patch("sys.argv", self._argv("http://t.com/{{id}}", csv_path, "--retry-file", custom_path)), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", side_effect=err), \
             patch("builtins.print"), \
             self.assertRaises(SystemExit):
            bulk_post._run()

        self.assertTrue(Path(custom_path).exists())

    def test_missing_csv_column_for_placeholder_exits(self):
        csv_path = self._write_csv("data.csv", [{"name": "alice"}])

        with patch("sys.argv", self._argv("http://t.com/{{id}}", csv_path)), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("builtins.print"), \
             self.assertRaises(SystemExit) as ctx:
            bulk_post._run()

        self.assertEqual(ctx.exception.code, 1)

    def test_body_placeholder_substituted_per_row(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}, {"id": "2"}])
        bodies_sent = []

        def capture(req, timeout=None):
            bodies_sent.append(req.data.decode())
            return self._mock_resp(200, b"ok")

        with patch("sys.argv", ["bp", "-u", "http://t.com/", "-c", csv_path, "-t", "tok",
                                 "-b", '{"id":"{{id}}"}']), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", side_effect=capture), \
             patch("builtins.print"):
            bulk_post._run()

        self.assertEqual(bodies_sent, ['{"id":"1"}', '{"id":"2"}'])

    def test_content_type_flag_sets_header(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        captured = []

        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200, b"ok")

        with patch("sys.argv", ["bp", "-u", "http://t.com/", "-c", csv_path, "-t", "tok",
                                 "-b", "id={{id}}", "-C", "application/x-www-form-urlencoded"]), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", side_effect=capture), \
             patch("builtins.print"):
            bulk_post._run()

        self.assertEqual(captured[0].get_header("Content-type"), "application/x-www-form-urlencoded")

    def test_invalid_json_body_exits_before_any_request(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        calls = []

        with patch("sys.argv", ["bp", "-u", "http://t.com/", "-c", csv_path, "-t", "tok",
                                 "-b", "{bad json {{id}}}", "-C", "application/json"]), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", side_effect=lambda *a, **kw: calls.append(1)), \
             patch("builtins.print"), \
             self.assertRaises(SystemExit) as ctx:
            bulk_post._run()

        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(calls, [])

    def test_invalid_xml_body_exits_before_any_request(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        calls = []

        with patch("sys.argv", ["bp", "-u", "http://t.com/", "-c", csv_path, "-t", "tok",
                                 "-b", "<root><unclosed {{id}}>", "-C", "application/xml"]), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", side_effect=lambda *a, **kw: calls.append(1)), \
             patch("builtins.print"), \
             self.assertRaises(SystemExit) as ctx:
            bulk_post._run()

        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(calls, [])

    def test_default_content_type_is_json(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        captured = []

        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200, b"ok")

        with patch("sys.argv", ["bp", "-u", "http://t.com/", "-c", csv_path, "-t", "tok",
                                 "-b", '{"id":"{{id}}"}']), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", side_effect=capture), \
             patch("builtins.print"):
            bulk_post._run()

        self.assertEqual(captured[0].get_header("Content-type"), "application/json")


    def test_basic_auth_header_sent(self):
        import base64
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        captured = []

        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200, b"ok")

        with patch("sys.argv", ["bp", "-u", "http://t.com/{{id}}", "-c", csv_path,
                                 "-a", "basic", "-U", "alice:s3cret"]), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", side_effect=capture), \
             patch("builtins.print"):
            bulk_post._run()

        expected = "Basic " + base64.b64encode(b"alice:s3cret").decode()
        self.assertEqual(captured[0].get_header("Authorization"), expected)

    def test_no_auth_sends_no_authorization_header(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        captured = []

        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200, b"ok")

        with patch("sys.argv", ["bp", "-u", "http://t.com/{{id}}", "-c", csv_path, "-a", "none"]), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", side_effect=capture), \
             patch("builtins.print"):
            bulk_post._run()

        self.assertIsNone(captured[0].get_header("Authorization"))

    def test_401_retries_with_new_basic_creds(self):
        import base64
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        err_401 = urllib.error.HTTPError("http://t.com/1", 401, "Unauthorized", {}, BytesIO(b""))
        auth_headers = []

        def capture(req, timeout=None):
            auth_headers.append(req.get_header("Authorization"))
            if len(auth_headers) == 1:
                raise err_401
            return self._mock_resp(200, b"ok")

        with patch("sys.argv", ["bp", "-u", "http://t.com/{{id}}", "-c", csv_path,
                                 "-a", "basic", "-U", "old:pass"]), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", side_effect=capture), \
             patch("bulk_post.prompt_new_basic_creds", return_value="new:pass"), \
             patch("builtins.print"):
            bulk_post._run()

        self.assertEqual(len(auth_headers), 2)
        self.assertEqual(auth_headers[0], "Basic " + base64.b64encode(b"old:pass").decode())
        self.assertEqual(auth_headers[1], "Basic " + base64.b64encode(b"new:pass").decode())


# ---------------------------------------------------------------------------
# --parallel integration tests
# ---------------------------------------------------------------------------

class TestParallelRun(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_csv(self, filename, rows):
        path = os.path.join(self.tmpdir, filename)
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return path

    def _mock_resp(self, status, body=b""):
        m = MagicMock()
        m.status = status
        m.read.return_value = body
        m.headers = {}
        m.__enter__.return_value = m
        m.__exit__.return_value = False
        return m

    def test_parallel_all_succeed_no_retry_file(self):
        rows = [{"id": str(i)} for i in range(1, 11)]
        csv_path = self._write_csv("data.csv", rows)
        retry_path = Path(csv_path).parent / "data_failed.csv"

        with patch("sys.argv", ["bp", "-u", "http://t.com/{{id}}", "-c", csv_path,
                                 "-a", "none", "--parallel", "-n", "3"]), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", return_value=self._mock_resp(200, b"ok")), \
             patch("builtins.print"):
            bulk_post._run()

        self.assertFalse(retry_path.exists())

    def test_parallel_failed_rows_written(self):
        rows = [{"id": str(i)} for i in range(1, 4)]
        csv_path = self._write_csv("data.csv", rows)
        retry_path = Path(csv_path).parent / "data_failed.csv"
        err = urllib.error.HTTPError("http://t.com/1", 500, "Err", {}, BytesIO(b"boom"))

        with patch("sys.argv", ["bp", "-u", "http://t.com/{{id}}", "-c", csv_path,
                                 "-a", "none", "--parallel", "-n", "3"]), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", side_effect=err), \
             patch("builtins.print"), \
             self.assertRaises(SystemExit) as ctx:
            bulk_post._run()

        self.assertEqual(ctx.exception.code, 1)
        self.assertTrue(retry_path.exists())
        with open(retry_path) as f:
            written_rows = list(csv.DictReader(f))
        self.assertEqual(len(written_rows), 3)

    def test_parallel_401_prompt_new_token_called_once(self):
        rows = [{"id": str(i)} for i in range(1, 6)]
        csv_path = self._write_csv("data.csv", rows)

        def mock_urlopen(req, timeout=None):
            if req.get_header("Authorization") == "Bearer old-tok":
                raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, BytesIO(b""))
            return self._mock_resp(200, b"ok")

        with patch("sys.argv", ["bp", "-u", "http://t.com/{{id}}", "-c", csv_path,
                                 "-a", "bearer", "-t", "old-tok", "--parallel", "-n", "5"]), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", side_effect=mock_urlopen), \
             patch("bulk_post.prompt_new_token", return_value="new-tok") as mock_prompt, \
             patch("builtins.print"):
            bulk_post._run()

        mock_prompt.assert_called_once()

    def test_parallel_respects_offset(self):
        rows = [{"id": str(i)} for i in range(1, 11)]
        csv_path = self._write_csv("data.csv", rows)
        urls_called = []

        def capture(req, timeout=None):
            urls_called.append(req.full_url)
            return self._mock_resp(200, b"ok")

        with patch("sys.argv", ["bp", "-u", "http://t.com/{{id}}", "-c", csv_path,
                                 "-a", "none", "--parallel", "-n", "3", "--offset", "5"]), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", side_effect=capture), \
             patch("builtins.print"):
            bulk_post._run()

        self.assertEqual(len(urls_called), 5)
        self.assertNotIn("http://t.com/1", urls_called)
        self.assertIn("http://t.com/10", urls_called)

    def test_debug_flag_prefixes_thread_name_in_output(self):
        rows = [{"id": str(i)} for i in range(1, 4)]
        csv_path = self._write_csv("data.csv", rows)
        printed = []

        with patch("sys.argv", ["bp", "-u", "http://t.com/{{id}}", "-c", csv_path,
                                 "-a", "none", "--parallel", "-n", "2", "--debug"]), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: self._mock_resp(200, b"ok")), \
             patch("builtins.print", side_effect=lambda *a, **kw: printed.append(str(a[0]) if a else "")):
            bulk_post._run()

        row_lines = [l for l in printed if "[OK]" in l]
        self.assertTrue(row_lines, "expected at least one [OK] line")
        self.assertTrue(
            any("[worker-" in l for l in row_lines),
            f"expected '[worker-N]' prefix in output; got: {row_lines}",
        )

    def test_parallel_exit_while_paused_does_not_hang(self):
        """Regression: /exit while paused must unblock workers and complete."""
        import threading as _threading
        rows = [{"id": str(i)} for i in range(1, 20)]
        csv_path = self._write_csv("data.csv", rows)

        cmd_queue = []

        def mock_poll(bar):
            import time
            time.sleep(0.05)
            if not cmd_queue:
                return None
            return cmd_queue.pop(0)

        # Slow requests so workers are still running when we send /pause then /exit
        def slow_resp(req, timeout=None):
            import time
            time.sleep(0.02)
            return self._mock_resp(200, b"ok")

        result = {}

        def run():
            with patch("sys.argv", ["bp", "-u", "http://t.com/{{id}}", "-c", csv_path,
                                     "-a", "none", "--parallel", "-n", "2"]), \
                 patch("sys.stdin.isatty", return_value=False), \
                 patch("urllib.request.urlopen", side_effect=slow_resp), \
                 patch("bulk_post._poll_cmd", side_effect=mock_poll):
                bulk_post._run()
            result["done"] = True

        t = _threading.Thread(target=run, daemon=True)
        t.start()
        import time
        time.sleep(0.15)           # let some rows start
        cmd_queue.append("/pause")
        time.sleep(0.1)            # let pause take effect
        cmd_queue.append("/exit")
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "script hung after /exit while paused")
        self.assertTrue(result.get("done"), "script did not complete cleanly")

    def test_debug_without_parallel_prints_info(self):
        import io
        rows = [{"id": "1"}]
        csv_path = self._write_csv("data.csv", rows)
        stderr_buf = io.StringIO()

        with patch("sys.argv", ["bp", "-u", "http://t.com/{{id}}", "-c", csv_path,
                                 "-a", "none", "--debug"]), \
             patch("sys.stdin.isatty", return_value=False), \
             patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: self._mock_resp(200, b"ok")), \
             patch("sys.stderr", stderr_buf):
            bulk_post._run()

        self.assertIn("--debug has no effect without --parallel", stderr_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
