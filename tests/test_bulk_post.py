"""Tests for bulk_post.py — run with: python -m unittest discover tests/"""

import argparse
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

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
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
        f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)  # noqa: SIM115  # NamedTemporaryFile, not open()
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
        with (
            patch.dict(os.environ, {"BULK_TOKEN": ""}),
            patch("builtins.input", return_value="typed-token"),
        ):
            token = bulk_post.resolve_token(None)
        self.assertEqual(token, "typed-token")

    def test_empty_interactive_input_exits(self):
        with (
            patch.dict(os.environ, {"BULK_TOKEN": ""}),
            patch("builtins.input", return_value=""),
            patch("builtins.print"),
            self.assertRaises(SystemExit) as ctx,
        ):
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
        with (
            patch.dict(os.environ, {"BULK_USER": ""}),
            patch("builtins.input", return_value="typed:creds"),
        ):
            creds = bulk_post.resolve_basic_creds(None)
        self.assertEqual(creds, "typed:creds")

    def test_empty_interactive_input_exits(self):
        with (
            patch.dict(os.environ, {"BULK_USER": ""}),
            patch("builtins.input", return_value=""),
            patch("builtins.print"),
            self.assertRaises(SystemExit) as ctx,
        ):
            bulk_post.resolve_basic_creds(None)
        self.assertEqual(ctx.exception.code, 1)


# ---------------------------------------------------------------------------
# _validate_body_template
# ---------------------------------------------------------------------------


class TestValidateBodyTemplate(unittest.TestCase):
    def test_valid_json_literal_returns_none(self):
        self.assertIsNone(
            bulk_post._validate_body_template('{"id":1}', "application/json")
        )

    def test_valid_json_with_placeholder_returns_none(self):
        self.assertIsNone(
            bulk_post._validate_body_template('{"id":"{{id}}"}', "application/json")
        )

    def test_valid_json_unquoted_placeholder_returns_none(self):
        # {{amount}} → null, giving {"amount": null} which is valid JSON
        self.assertIsNone(
            bulk_post._validate_body_template(
                '{"amount":{{amount}}}', "application/json"
            )
        )

    def test_invalid_json_template_returns_error(self):
        err = bulk_post._validate_body_template("{bad json {{id}}}", "application/json")
        self.assertIsNotNone(err)
        self.assertIn("Invalid JSON", err)

    def test_valid_xml_with_placeholder_returns_none(self):
        self.assertIsNone(
            bulk_post._validate_body_template(
                "<item><id>{{id}}</id></item>", "application/xml"
            )
        )

    def test_invalid_xml_template_returns_error(self):
        err = bulk_post._validate_body_template(
            "<root><unclosed {{id}}>", "application/xml"
        )
        self.assertIsNotNone(err)
        self.assertIn("Invalid XML", err)

    def test_text_xml_content_type(self):
        self.assertIsNone(bulk_post._validate_body_template("<a/>", "text/xml"))

    def test_unknown_content_type_skips_validation(self):
        self.assertIsNone(
            bulk_post._validate_body_template(
                "not json or xml", "application/x-www-form-urlencoded"
            )
        )

    def test_json_content_type_case_insensitive(self):
        self.assertIsNone(
            bulk_post._validate_body_template('{"x":"{{v}}"}', "Application/JSON")
        )

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
        with patch(
            "urllib.request.urlopen", return_value=self._mock_resp(200, b'{"ok":true}')
        ):
            status, body, elapsed, *_ = bulk_post.http_request(
                "http://x.com/", "tok", "GET", None
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, '{"ok":true}')
        self.assertGreaterEqual(elapsed, 0)

    def test_201_success(self):
        with patch(
            "urllib.request.urlopen", return_value=self._mock_resp(201, b"created")
        ):
            status, *_ = bulk_post.http_request("http://x.com/", "tok", "POST", "{}")
        self.assertEqual(status, 201)

    def test_http_error_404(self):
        err = urllib.error.HTTPError(
            "http://x.com/", 404, "Not Found", {}, BytesIO(b"not found")
        )
        with patch("urllib.request.urlopen", side_effect=err):
            status, body, *_ = bulk_post.http_request(
                "http://x.com/", "tok", "GET", None
            )
        self.assertEqual(status, 404)
        self.assertEqual(body, "not found")

    def test_http_error_401(self):
        err = urllib.error.HTTPError(
            "http://x.com/", 401, "Unauthorized", {}, BytesIO(b"")
        )
        with patch("urllib.request.urlopen", side_effect=err):
            status, *_ = bulk_post.http_request("http://x.com/", "tok", "GET", None)
        self.assertEqual(status, 401)

    def test_url_error_returns_none_status(self):
        with patch(
            "urllib.request.urlopen", side_effect=urllib.error.URLError("refused")
        ):
            status, body, *_ = bulk_post.http_request(
                "http://x.com/", "tok", "GET", None
            )
        self.assertIsNone(status)
        self.assertIn("Connection error", body)

    def test_timeout_error_returns_none_status(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError()):
            status, body, *_ = bulk_post.http_request(
                "http://x.com/", "tok", "GET", None, timeout=5
            )
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
            bulk_post.http_request(
                "http://x.com/",
                "tok",
                "POST",
                "id=1&v=2",
                content_type="application/x-www-form-urlencoded",
            )
        self.assertEqual(
            captured[0].get_header("Content-type"), "application/x-www-form-urlencoded"
        )

    def test_no_body_ignores_custom_content_type(self):
        captured = []

        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200)

        with patch("urllib.request.urlopen", side_effect=capture):
            bulk_post.http_request(
                "http://x.com/",
                "tok",
                "GET",
                None,
                content_type="application/x-www-form-urlencoded",
            )
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

    def test_extra_headers_sent(self):
        captured = []

        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200)

        with patch("urllib.request.urlopen", side_effect=capture):
            bulk_post.http_request(
                "http://x.com/",
                None,
                "GET",
                None,
                extra_headers={"X-Tenant": "acme", "X-Request-Id": "42"},
            )
        self.assertEqual(captured[0].get_header("X-tenant"), "acme")
        self.assertEqual(captured[0].get_header("X-request-id"), "42")

    def test_auth_overrides_extra_header_with_same_name(self):
        """auth_header should win if extra_headers contains Authorization."""
        captured = []

        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200)

        with patch("urllib.request.urlopen", side_effect=capture):
            bulk_post.http_request(
                "http://x.com/",
                "Bearer real-token",
                "GET",
                None,
                extra_headers={"Authorization": "Bearer wrong-token"},
            )
        self.assertEqual(captured[0].get_header("Authorization"), "Bearer real-token")


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

        with (
            patch("sys.argv", self._argv("http://t.com/{{id}}", csv_path)),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", return_value=self._mock_resp(200, b"ok")),
            patch("builtins.print"),
        ):
            bulk_post._run()

        self.assertFalse(retry_path.exists())

    def test_failed_rows_written_to_retry_file(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}, {"id": "2"}])
        retry_path = Path(csv_path).parent / "data_failed.csv"
        err = urllib.error.HTTPError("http://t.com/1", 500, "Err", {}, BytesIO(b"boom"))

        with (
            patch("sys.argv", self._argv("http://t.com/{{id}}", csv_path)),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=err),
            patch("builtins.print"),
        ):
            code = bulk_post.main()

        self.assertEqual(code, 1)
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

        with (
            patch(
                "sys.argv", self._argv("http://t.com/{{id}}", csv_path, "--offset", "2")
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=capture),
            patch("builtins.print"),
        ):
            bulk_post._run()

        self.assertEqual(urls_called, ["http://t.com/3"])

    def test_offset_beyond_rows_exits(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])

        with (
            patch(
                "sys.argv", self._argv("http://t.com/{{id}}", csv_path, "--offset", "5")
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("builtins.print"),
        ):
            code = bulk_post.main()

        self.assertEqual(code, 1)

    def test_401_retries_with_new_token(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        err_401 = urllib.error.HTTPError(
            "http://t.com/1", 401, "Unauthorized", {}, BytesIO(b"")
        )
        auth_headers = []

        def capture(req, timeout=None):
            auth_headers.append(req.get_header("Authorization"))
            if len(auth_headers) == 1:
                raise err_401
            return self._mock_resp(200, b"ok")

        with (
            patch("sys.argv", self._argv("http://t.com/{{id}}", csv_path)),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=capture),
            patch("bulk_post.runner.prompt_new_token", return_value="new-tok"),
            patch("builtins.print"),
        ):
            bulk_post._run()

        self.assertEqual(len(auth_headers), 2)
        self.assertEqual(auth_headers[0], "Bearer tok")
        self.assertEqual(auth_headers[1], "Bearer new-tok")

    def test_custom_retry_file_path(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        custom_path = os.path.join(self.tmpdir, "custom_failed.csv")
        err = urllib.error.HTTPError("http://t.com/1", 500, "Err", {}, BytesIO(b""))

        with (
            patch(
                "sys.argv",
                self._argv(
                    "http://t.com/{{id}}", csv_path, "--retry-file", custom_path
                ),
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=err),
            patch("builtins.print"),
        ):
            code = bulk_post.main()

        self.assertEqual(code, 1)
        self.assertTrue(Path(custom_path).exists())

    def test_missing_csv_column_for_placeholder_exits(self):
        csv_path = self._write_csv("data.csv", [{"name": "alice"}])

        with (
            patch("sys.argv", self._argv("http://t.com/{{id}}", csv_path)),
            patch("sys.stdin.isatty", return_value=False),
            patch("builtins.print"),
        ):
            code = bulk_post.main()

        self.assertEqual(code, 1)

    def test_body_placeholder_substituted_per_row(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}, {"id": "2"}])
        bodies_sent = []

        def capture(req, timeout=None):
            bodies_sent.append(req.data.decode())
            return self._mock_resp(200, b"ok")

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/",
                    "-c",
                    csv_path,
                    "-t",
                    "tok",
                    "-b",
                    '{"id":"{{id}}"}',
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=capture),
            patch("builtins.print"),
        ):
            bulk_post._run()

        self.assertEqual(bodies_sent, ['{"id":"1"}', '{"id":"2"}'])

    def test_content_type_flag_sets_header(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        captured = []

        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200, b"ok")

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/",
                    "-c",
                    csv_path,
                    "-t",
                    "tok",
                    "-b",
                    "id={{id}}",
                    "-C",
                    "application/x-www-form-urlencoded",
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=capture),
            patch("builtins.print"),
        ):
            bulk_post._run()

        self.assertEqual(
            captured[0].get_header("Content-type"), "application/x-www-form-urlencoded"
        )

    def test_invalid_json_body_exits_before_any_request(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        calls = []

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/",
                    "-c",
                    csv_path,
                    "-t",
                    "tok",
                    "-b",
                    "{bad json {{id}}}",
                    "-C",
                    "application/json",
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch(
                "urllib.request.urlopen", side_effect=lambda *a, **kw: calls.append(1)
            ),
            patch("builtins.print"),
        ):
            code = bulk_post.main()

        self.assertEqual(code, 1)
        self.assertEqual(calls, [])

    def test_invalid_xml_body_exits_before_any_request(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        calls = []

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/",
                    "-c",
                    csv_path,
                    "-t",
                    "tok",
                    "-b",
                    "<root><unclosed {{id}}>",
                    "-C",
                    "application/xml",
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch(
                "urllib.request.urlopen", side_effect=lambda *a, **kw: calls.append(1)
            ),
            patch("builtins.print"),
        ):
            code = bulk_post.main()

        self.assertEqual(code, 1)
        self.assertEqual(calls, [])

    def test_default_content_type_is_json(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        captured = []

        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200, b"ok")

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/",
                    "-c",
                    csv_path,
                    "-t",
                    "tok",
                    "-b",
                    '{"id":"{{id}}"}',
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=capture),
            patch("builtins.print"),
        ):
            bulk_post._run()

        self.assertEqual(captured[0].get_header("Content-type"), "application/json")

    def test_basic_auth_header_sent(self):
        import base64

        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        captured = []

        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200, b"ok")

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/{{id}}",
                    "-c",
                    csv_path,
                    "-a",
                    "basic",
                    "-U",
                    "alice:s3cret",
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=capture),
            patch("builtins.print"),
        ):
            bulk_post._run()

        expected = "Basic " + base64.b64encode(b"alice:s3cret").decode()
        self.assertEqual(captured[0].get_header("Authorization"), expected)

    def test_custom_header_sent_on_every_request(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}, {"id": "2"}])
        captured = []

        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200, b"ok")

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/{{id}}",
                    "-c",
                    csv_path,
                    "-a",
                    "none",
                    "-H",
                    "X-Tenant: acme",
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=capture),
            patch("builtins.print"),
        ):
            bulk_post._run()

        self.assertEqual(len(captured), 2)
        for req in captured:
            self.assertEqual(req.get_header("X-tenant"), "acme")

    def test_custom_header_value_supports_placeholder(self):
        csv_path = self._write_csv("data.csv", [{"id": "42", "tenant": "acme"}])
        captured = []

        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200, b"ok")

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/{{id}}",
                    "-c",
                    csv_path,
                    "-a",
                    "none",
                    "-H",
                    "X-Tenant: {{tenant}}",
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=capture),
            patch("builtins.print"),
        ):
            bulk_post._run()

        self.assertEqual(captured[0].get_header("X-tenant"), "acme")

    def test_multiple_custom_headers(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        captured = []

        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200, b"ok")

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/{{id}}",
                    "-c",
                    csv_path,
                    "-a",
                    "none",
                    "-H",
                    "X-Foo: bar",
                    "-H",
                    "X-Baz: qux",
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=capture),
            patch("builtins.print"),
        ):
            bulk_post._run()

        self.assertEqual(captured[0].get_header("X-foo"), "bar")
        self.assertEqual(captured[0].get_header("X-baz"), "qux")

    def test_bad_header_format_exits(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        calls = []

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/{{id}}",
                    "-c",
                    csv_path,
                    "-a",
                    "none",
                    "-H",
                    "BadHeaderNoColon",
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch(
                "urllib.request.urlopen", side_effect=lambda *a, **kw: calls.append(1)
            ),
            patch("builtins.print"),
        ):
            code = bulk_post.main()

        self.assertEqual(code, 1)
        self.assertEqual(calls, [])

    def test_header_placeholder_missing_from_csv_exits(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        calls = []

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/{{id}}",
                    "-c",
                    csv_path,
                    "-a",
                    "none",
                    "-H",
                    "X-Tenant: {{tenant}}",
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch(
                "urllib.request.urlopen", side_effect=lambda *a, **kw: calls.append(1)
            ),
            patch("builtins.print"),
        ):
            code = bulk_post.main()

        self.assertEqual(code, 1)
        self.assertEqual(calls, [])

    def test_no_auth_sends_no_authorization_header(self):
        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        captured = []

        def capture(req, timeout=None):
            captured.append(req)
            return self._mock_resp(200, b"ok")

        with (
            patch(
                "sys.argv",
                ["bp", "-u", "http://t.com/{{id}}", "-c", csv_path, "-a", "none"],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=capture),
            patch("builtins.print"),
        ):
            bulk_post._run()

        self.assertIsNone(captured[0].get_header("Authorization"))

    def test_401_retries_with_new_basic_creds(self):
        import base64

        csv_path = self._write_csv("data.csv", [{"id": "1"}])
        err_401 = urllib.error.HTTPError(
            "http://t.com/1", 401, "Unauthorized", {}, BytesIO(b"")
        )
        auth_headers = []

        def capture(req, timeout=None):
            auth_headers.append(req.get_header("Authorization"))
            if len(auth_headers) == 1:
                raise err_401
            return self._mock_resp(200, b"ok")

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/{{id}}",
                    "-c",
                    csv_path,
                    "-a",
                    "basic",
                    "-U",
                    "old:pass",
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=capture),
            patch("bulk_post.runner.prompt_new_basic_creds", return_value="new:pass"),
            patch("builtins.print"),
        ):
            bulk_post._run()

        self.assertEqual(len(auth_headers), 2)
        self.assertEqual(
            auth_headers[0], "Basic " + base64.b64encode(b"old:pass").decode()
        )
        self.assertEqual(
            auth_headers[1], "Basic " + base64.b64encode(b"new:pass").decode()
        )


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

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/{{id}}",
                    "-c",
                    csv_path,
                    "-a",
                    "none",
                    "--parallel",
                    "-n",
                    "3",
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", return_value=self._mock_resp(200, b"ok")),
            patch("builtins.print"),
        ):
            bulk_post._run()

        self.assertFalse(retry_path.exists())

    def test_parallel_failed_rows_written(self):
        rows = [{"id": str(i)} for i in range(1, 4)]
        csv_path = self._write_csv("data.csv", rows)
        retry_path = Path(csv_path).parent / "data_failed.csv"
        err = urllib.error.HTTPError("http://t.com/1", 500, "Err", {}, BytesIO(b"boom"))

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/{{id}}",
                    "-c",
                    csv_path,
                    "-a",
                    "none",
                    "--parallel",
                    "-n",
                    "3",
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=err),
            patch("builtins.print"),
        ):
            code = bulk_post.main()

        self.assertEqual(code, 1)
        self.assertTrue(retry_path.exists())
        with open(retry_path) as f:
            written_rows = list(csv.DictReader(f))
        self.assertEqual(len(written_rows), 3)

    def test_parallel_401_prompt_new_token_called_once(self):
        rows = [{"id": str(i)} for i in range(1, 6)]
        csv_path = self._write_csv("data.csv", rows)

        def mock_urlopen(req, timeout=None):
            if req.get_header("Authorization") == "Bearer old-tok":
                raise urllib.error.HTTPError(
                    req.full_url, 401, "Unauthorized", {}, BytesIO(b"")
                )
            return self._mock_resp(200, b"ok")

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/{{id}}",
                    "-c",
                    csv_path,
                    "-a",
                    "bearer",
                    "-t",
                    "old-tok",
                    "--parallel",
                    "-n",
                    "5",
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=mock_urlopen),
            patch(
                "bulk_post.auth.prompt_new_token", return_value="new-tok"
            ) as mock_prompt,
            patch("builtins.print"),
        ):
            bulk_post._run()

        mock_prompt.assert_called_once()

    def test_parallel_respects_offset(self):
        rows = [{"id": str(i)} for i in range(1, 11)]
        csv_path = self._write_csv("data.csv", rows)
        urls_called = []

        def capture(req, timeout=None):
            urls_called.append(req.full_url)
            return self._mock_resp(200, b"ok")

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/{{id}}",
                    "-c",
                    csv_path,
                    "-a",
                    "none",
                    "--parallel",
                    "-n",
                    "3",
                    "--offset",
                    "5",
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=capture),
            patch("builtins.print"),
        ):
            bulk_post._run()

        self.assertEqual(len(urls_called), 5)
        self.assertNotIn("http://t.com/1", urls_called)
        self.assertIn("http://t.com/10", urls_called)

    def test_parallel_progress_accounts_for_offset(self):
        rows = [{"id": str(i)} for i in range(1, 11)]
        csv_path = self._write_csv("data.csv", rows)
        progress_calls = []

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/{{id}}",
                    "-c",
                    csv_path,
                    "-a",
                    "none",
                    "--parallel",
                    "-n",
                    "3",
                    "--offset",
                    "5",
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", return_value=self._mock_resp(200, b"ok")),
            patch(
                "bulk_post.runner._progress",
                side_effect=lambda bar, current, total: progress_calls.append(
                    (current, total)
                ),
            ),
            patch("builtins.print"),
        ):
            bulk_post._run()

        # Progress must count absolute rows (offset + processed) against the
        # total, so it reaches 100% (10/10) rather than maxing at 5/10.
        self.assertTrue(progress_calls)
        self.assertEqual(progress_calls[0][1], 10)
        self.assertEqual(max(c for c, _ in progress_calls), 10)
        self.assertEqual(min(c for c, _ in progress_calls), 6)

    def test_debug_flag_prefixes_thread_name_in_output(self):
        rows = [{"id": str(i)} for i in range(1, 4)]
        csv_path = self._write_csv("data.csv", rows)
        printed = []

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/{{id}}",
                    "-c",
                    csv_path,
                    "-a",
                    "none",
                    "--parallel",
                    "-n",
                    "2",
                    "--debug",
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch(
                "urllib.request.urlopen",
                side_effect=lambda req, timeout=None: self._mock_resp(200, b"ok"),
            ),
            patch(
                "builtins.print",
                side_effect=lambda *a, **kw: printed.append(str(a[0]) if a else ""),
            ),
        ):
            bulk_post._run()

        row_lines = [line for line in printed if "[OK]" in line]
        self.assertTrue(row_lines, "expected at least one [OK] line")
        self.assertTrue(
            any("[worker-" in line for line in row_lines),
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
            with (
                patch(
                    "sys.argv",
                    [
                        "bp",
                        "-u",
                        "http://t.com/{{id}}",
                        "-c",
                        csv_path,
                        "-a",
                        "none",
                        "--parallel",
                        "-n",
                        "2",
                    ],
                ),
                patch("sys.stdin.isatty", return_value=False),
                patch("urllib.request.urlopen", side_effect=slow_resp),
                patch("bulk_post.runner._poll_cmd", side_effect=mock_poll),
            ):
                bulk_post._run()
            result["done"] = True

        t = _threading.Thread(target=run, daemon=True)
        t.start()
        import time

        time.sleep(0.15)  # let some rows start
        cmd_queue.append("/pause")
        time.sleep(0.1)  # let pause take effect
        cmd_queue.append("/exit")
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "script hung after /exit while paused")
        self.assertTrue(result.get("done"), "script did not complete cleanly")

    def test_debug_without_parallel_prints_info(self):
        import io

        rows = [{"id": "1"}]
        csv_path = self._write_csv("data.csv", rows)
        stderr_buf = io.StringIO()

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "http://t.com/{{id}}",
                    "-c",
                    csv_path,
                    "-a",
                    "none",
                    "--debug",
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch(
                "urllib.request.urlopen",
                side_effect=lambda req, timeout=None: self._mock_resp(200, b"ok"),
            ),
            patch("sys.stderr", stderr_buf),
        ):
            bulk_post._run()

        self.assertIn("--debug has no effect without --parallel", stderr_buf.getvalue())


# ---------------------------------------------------------------------------
# parse_workflow
# ---------------------------------------------------------------------------


class TestParseWorkflow(unittest.TestCase):
    def _write_yaml(self, name, content):
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115  # NamedTemporaryFile, not open()
            mode="w", suffix=".yaml", delete=False, prefix=name
        )
        tmp.write(content)
        tmp.close()
        return tmp.name

    def tearDown(self):
        pass  # tempfiles cleaned up per-test via addCleanup

    def _yaml(self, name, content):
        path = self._write_yaml(name, content)
        self.addCleanup(os.unlink, path)
        return path

    def test_nested_style_parses(self):
        path = self._yaml(
            "wf",
            """
workflow:
  groupA:
    auth:
      type: bearer
      token: tok123
    endpoints:
      - step-one:
          url: https://api.example.com/{{id}}
          method: POST
          body: '{"x": "{{val}}"}'
          headers:
            Content-Type: application/json
            X-Version: "1"
""",
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNone(err)
        self.assertEqual(len(steps), 1)
        s = steps[0]
        self.assertEqual(s.path, "groupA/step-one")
        self.assertEqual(s.url, "https://api.example.com/{{id}}")
        self.assertEqual(s.method, "POST")
        self.assertEqual(s.body, '{"x": "{{val}}"}')
        self.assertEqual(s.content_type, "application/json")
        self.assertNotIn("Content-Type", s.headers)
        self.assertNotIn("content-type", s.headers)
        self.assertEqual(s.headers.get("X-Version"), "1")
        self.assertEqual(s.auth_type, "bearer")
        self.assertEqual(s.auth_raw, "tok123")
        self.assertEqual(s.on_error, "stop")

    def test_group_auth_applied_to_steps(self):
        path = self._yaml(
            "wf",
            """
workflow:
  groupA:
    auth:
      type: basic
      user: alice
      password: secret
    endpoints:
      - step-one:
          url: https://api.example.com/{{id}}
          method: GET
""",
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNone(err)
        s = steps[0]
        self.assertEqual(s.auth_type, "basic")
        self.assertEqual(s.auth_raw, "alice:secret")

    def test_step_auth_overrides_group_auth(self):
        path = self._yaml(
            "wf",
            """
workflow:
  groupA:
    auth:
      type: bearer
      token: group_tok
    endpoints:
      - step-one:
          url: https://api.example.com/{{id}}
          method: GET
          auth:
            type: bearer
            token: step_tok
""",
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNone(err)
        self.assertEqual(steps[0].auth_raw, "step_tok")

    def test_no_auth_group_defaults_to_none(self):
        path = self._yaml(
            "wf",
            """
workflow:
  groupC:
    endpoints:
      - step-one:
          url: https://api.example.com/{{id}}
          method: DELETE
""",
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNone(err)
        self.assertEqual(steps[0].auth_type, "none")
        self.assertEqual(steps[0].auth_raw, "")

    def test_on_error_continue(self):
        path = self._yaml(
            "wf",
            """
workflow:
  groupA:
    endpoints:
      - step-one:
          url: https://api.example.com/{{id}}
          method: DELETE
          on_error: continue
""",
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNone(err)
        self.assertEqual(steps[0].on_error, "continue")

    def test_content_type_extracted_from_headers(self):
        path = self._yaml(
            "wf",
            """
workflow:
  groupA:
    endpoints:
      - step-one:
          url: https://api.example.com/{{id}}
          method: POST
          headers:
            Content-Type: text/plain
            X-Foo: bar
""",
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNone(err)
        s = steps[0]
        self.assertEqual(s.content_type, "text/plain")
        self.assertNotIn("Content-Type", s.headers)
        self.assertEqual(s.headers.get("X-Foo"), "bar")

    def test_document_order_preserved(self):
        path = self._yaml(
            "wf",
            """
workflow:
  groupA:
    endpoints:
      - step-one:
          url: https://a.example.com/{{id}}
          method: GET
      - step-two:
          url: https://b.example.com/{{id}}
          method: POST
  groupB:
    endpoints:
      - step-three:
          url: https://c.example.com/{{id}}
          method: DELETE
""",
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNone(err)
        self.assertEqual(
            [s.path for s in steps],
            ["groupA/step-one", "groupA/step-two", "groupB/step-three"],
        )

    def test_missing_url_returns_error(self):
        path = self._yaml(
            "wf",
            """
workflow:
  groupA:
    endpoints:
      - step-one:
          method: GET
""",
        )
        _, err = bulk_post.parse_workflow(path)
        self.assertIsNotNone(err)
        self.assertIn("url", err.lower())

    def test_missing_workflow_key_returns_error(self):
        path = self._yaml(
            "wf",
            """
steps:
  - url: https://api.example.com
""",
        )
        _, err = bulk_post.parse_workflow(path)
        self.assertIsNotNone(err)
        self.assertIn("workflow", err)

    def test_invalid_yaml_returns_error(self):
        path = self._yaml("wf", "workflow:\n  groupA:\n    bad: [\n")
        _, err = bulk_post.parse_workflow(path)
        self.assertIsNotNone(err)

    def test_no_endpoints_returns_error(self):
        path = self._yaml(
            "wf",
            """
workflow:
  description: Empty workflow
""",
        )
        _, err = bulk_post.parse_workflow(path)
        self.assertIsNotNone(err)

    def test_description_key_skipped(self):
        path = self._yaml(
            "wf",
            """
workflow:
  description: This is a test workflow
  groupA:
    endpoints:
      - step-one:
          url: https://api.example.com/{{id}}
          method: GET
""",
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNone(err)
        self.assertEqual(len(steps), 1)


# ---------------------------------------------------------------------------
# Workflow integration (_run with --workflow)
# ---------------------------------------------------------------------------


class TestWorkflowRun(unittest.TestCase):
    def _write_csv(self, name, rows):
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115  # NamedTemporaryFile, not open()
            mode="w", suffix=".csv", delete=False, prefix=name, newline=""
        )
        if rows:
            w = csv.DictWriter(tmp, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def _write_yaml(self, name, content):
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115  # NamedTemporaryFile, not open()
            mode="w", suffix=".yaml", delete=False, prefix=name
        )
        tmp.write(content)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def _mock_resp(self, status, body=b"ok"):
        resp = MagicMock()
        resp.status = status
        resp.read.return_value = body
        resp.headers = {}
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_basic_workflow_all_steps_succeed(self):
        csv_path = self._write_csv("rows", [{"id": "1"}, {"id": "2"}])
        wf_path = self._write_yaml(
            "wf",
            """
workflow:
  groupA:
    endpoints:
      - step-one:
          url: https://api.example.com/{{id}}
          method: GET
      - step-two:
          url: https://api.example.com/{{id}}/confirm
          method: POST
""",
        )
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            return self._mock_resp(200)

        with (
            patch(
                "sys.argv", ["bp", "--workflow", wf_path, "-c", csv_path, "-a", "none"]
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            bulk_post._run()

        self.assertEqual(len(calls), 4)
        self.assertIn("https://api.example.com/1", calls)
        self.assertIn("https://api.example.com/1/confirm", calls)
        self.assertIn("https://api.example.com/2", calls)
        self.assertIn("https://api.example.com/2/confirm", calls)

    def test_parallel_progress_accounts_for_offset(self):
        csv_path = self._write_csv("rows", [{"id": str(i)} for i in range(1, 11)])
        wf_path = self._write_yaml(
            "wf",
            """
workflow:
  groupA:
    endpoints:
      - step-one:
          url: https://api.example.com/{{id}}
          method: GET
""",
        )
        progress_calls = []

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "--workflow",
                    wf_path,
                    "-c",
                    csv_path,
                    "-a",
                    "none",
                    "--parallel",
                    "-n",
                    "3",
                    "--offset",
                    "5",
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch(
                "urllib.request.urlopen",
                side_effect=lambda req, timeout=None: self._mock_resp(200),
            ),
            patch(
                "bulk_post.workflow_runner._progress",
                side_effect=lambda bar, current, total: progress_calls.append(
                    (current, total)
                ),
            ),
            patch("builtins.print"),
        ):
            bulk_post._run()

        # Progress must count absolute rows (offset + processed), so it reaches
        # 100% (10/10) rather than maxing at 5/10.
        self.assertTrue(progress_calls)
        self.assertEqual(progress_calls[0][1], 10)
        self.assertEqual(max(c for c, _ in progress_calls), 10)
        self.assertEqual(min(c for c, _ in progress_calls), 6)

    def test_step_failure_stop_writes_retry_with_step_col(self):
        csv_path = self._write_csv("rows", [{"id": "1"}])
        wf_path = self._write_yaml(
            "wf",
            """
workflow:
  groupA:
    endpoints:
      - step-one:
          url: https://api.example.com/{{id}}
          method: GET
      - step-two:
          url: https://api.example.com/{{id}}/confirm
          method: POST
""",
        )
        retry_path = csv_path.replace(".csv", "_failed.csv")
        self.addCleanup(
            lambda: os.unlink(retry_path) if os.path.exists(retry_path) else None
        )

        call_count = [0]

        def fake_urlopen(req, timeout=None):
            call_count[0] += 1
            if ("step-one" in req.full_url or "/1" in req.full_url) and call_count[
                0
            ] == 1:
                return self._mock_resp(500)
            return self._mock_resp(200)

        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "--workflow",
                    wf_path,
                    "-c",
                    csv_path,
                    "-a",
                    "none",
                    "-r",
                    retry_path,
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            code = bulk_post.main()

        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(retry_path))
        with open(retry_path, newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        self.assertEqual(len(rows), 1)
        self.assertIn(bulk_post._WORKFLOW_STEP_COL, rows[0])
        self.assertEqual(rows[0][bulk_post._WORKFLOW_STEP_COL], "groupA/step-one")

    def test_step_failure_on_error_continue_continues_steps(self):
        csv_path = self._write_csv("rows", [{"id": "1"}])
        wf_path = self._write_yaml(
            "wf",
            """
workflow:
  groupA:
    endpoints:
      - step-one:
          url: https://api.example.com/{{id}}
          method: GET
          on_error: continue
      - step-two:
          url: https://api.example.com/{{id}}/confirm
          method: POST
          on_error: continue
""",
        )
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            if req.full_url.split("example.com")[1] == "/1":
                return self._mock_resp(500)
            return self._mock_resp(200)

        with (
            patch(
                "sys.argv", ["bp", "--workflow", wf_path, "-c", csv_path, "-a", "none"]
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            code = bulk_post.main()

        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(any("/1/confirm" in u for u in calls))

    def test_resume_skips_steps_before_failed_step(self):
        """If _bulk_post_step is present in CSV row, steps before that path are skipped."""
        csv_path = self._write_csv(
            "rows", [{"id": "1", bulk_post._WORKFLOW_STEP_COL: "groupA/step-two"}]
        )
        wf_path = self._write_yaml(
            "wf",
            """
workflow:
  groupA:
    endpoints:
      - step-one:
          url: https://api.example.com/{{id}}/one
          method: GET
      - step-two:
          url: https://api.example.com/{{id}}/two
          method: GET
      - step-three:
          url: https://api.example.com/{{id}}/three
          method: GET
""",
        )
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            return self._mock_resp(200)

        with (
            patch(
                "sys.argv", ["bp", "--workflow", wf_path, "-c", csv_path, "-a", "none"]
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            bulk_post._run()

        urls = list(calls)
        self.assertFalse(any("/one" in u for u in urls), "step-one should be skipped")
        self.assertTrue(any("/two" in u for u in urls), "step-two should be executed")
        self.assertTrue(
            any("/three" in u for u in urls), "step-three should be executed"
        )

    def test_url_and_workflow_mutually_exclusive(self):
        csv_path = self._write_csv("rows", [{"id": "1"}])
        wf_path = self._write_yaml(
            "wf",
            """
workflow:
  groupA:
    endpoints:
      - step-one:
          url: https://api.example.com/{{id}}
          method: GET
""",
        )
        import io

        stderr_buf = io.StringIO()
        with (
            patch(
                "sys.argv",
                [
                    "bp",
                    "-u",
                    "https://api.example.com/{{id}}",
                    "--workflow",
                    wf_path,
                    "-c",
                    csv_path,
                ],
            ),
            patch("sys.stdin.isatty", return_value=False),
            patch("sys.stderr", stderr_buf),
        ):
            code = bulk_post.main()
        self.assertEqual(code, 1)

    def test_neither_url_nor_workflow_exits(self):
        csv_path = self._write_csv("rows", [{"id": "1"}])
        import io

        stderr_buf = io.StringIO()
        with (
            patch("sys.argv", ["bp", "-c", csv_path]),
            patch("sys.stdin.isatty", return_value=False),
            patch("sys.stderr", stderr_buf),
        ):
            code = bulk_post.main()
        self.assertEqual(code, 1)


class TestResumeAfterDrain(unittest.TestCase):
    """Regression: /resume must unblock workers after in-flight requests drain to 0.

    Bug (commit 988bd5c): _pausing was reset to False once in_flight hit 0 while
    printing "[PAUSED]". Because /resume is gated on `cmd == _CMD_RESUME and
    _pausing`, resuming after a full drain became a no-op, leaving pause_event
    cleared and workers parked in pause_event.wait() forever.
    """

    def test_resume_after_full_drain_unblocks_workers(self):
        import contextlib
        import io as _io

        state = bulk_post._ParallelState(auth_header=None)
        state.in_flight = 2  # two requests still running at /pause time

        class _FakeThread:
            def __init__(self):
                self.alive = True

            def is_alive(self):
                return self.alive

            def join(self, *a, **k):
                pass

        thread = _FakeThread()
        producer = _FakeThread()

        # Scripted commands, one per poll-loop iteration:
        #   0: /pause                  -> pause_event cleared, _pausing=True
        #   1: (None) still draining   -> "[PAUSING] ..."
        #   2: (None) workers finished -> in_flight=0 -> "[PAUSED] ..."
        #   3: /resume                 -> must re-set pause_event
        #   4: (None) stop the loop
        calls = {"n": 0}

        def fake_poll(bar):
            i = calls["n"]
            calls["n"] += 1
            if i == 0:
                return bulk_post._CMD_PAUSE
            if i == 2:
                state.in_flight = 0  # all in-flight requests have completed
                return None
            if i == 3:
                return bulk_post._CMD_RESUME
            if i >= 4:
                thread.alive = False  # let the loop exit after resume
            return None

        with (
            patch.object(bulk_post.runner, "_poll_cmd", fake_poll),
            contextlib.redirect_stdout(_io.StringIO()),
        ):
            bulk_post._run_parallel_main_loop(
                threads=[thread],
                producer_thread=producer,
                state=state,
                bar=None,
                debug=False,
                work_queue=MagicMock(),
            )

        self.assertTrue(
            state.pause_event.is_set(),
            "/resume after a full in-flight drain failed to unblock workers",
        )


class TestVersionFlag(unittest.TestCase):
    def test_version_flag_prints_and_exits_zero(self):
        with (
            self.assertRaises(SystemExit) as ctx,
            patch("sys.argv", ["bulk-post", "--version"]),
        ):
            bulk_post._run()
        self.assertEqual(ctx.exception.code, 0)

    def test_get_version_returns_string(self):
        self.assertIsInstance(bulk_post._get_version(), str)


class TestMainExitCodes(unittest.TestCase):
    def test_missing_url_and_workflow_returns_1(self):
        with (
            patch("sys.argv", ["bulk-post", "-c", "nonexistent.csv"]),
            patch("sys.stdin.isatty", return_value=False),
        ):
            code = bulk_post.main()
        self.assertEqual(code, 1)


class TestVariableExtraction(unittest.TestCase):
    def _extract(self, body, expr):
        from bulk_post.variables import _compile_jsonpath, _extract

        return _extract(body, _compile_jsonpath(expr))

    def test_top_level_scalar(self):
        self.assertEqual(self._extract('{"id": 42}', "$.id"), 42)

    def test_nested_scalar(self):
        self.assertEqual(self._extract('{"a": {"b": "x"}}', "$.a.b"), "x")

    def test_no_match_is_null(self):
        from bulk_post.variables import _NULL

        self.assertIs(self._extract('{"id": 1}', "$.missing"), _NULL)

    def test_explicit_null_is_null(self):
        from bulk_post.variables import _NULL

        self.assertIs(self._extract('{"id": null}', "$.id"), _NULL)

    def test_object_match_is_nonscalar(self):
        from bulk_post.variables import _NONSCALAR

        self.assertIs(self._extract('{"a": {"b": 1}}', "$.a"), _NONSCALAR)

    def test_unparseable_body_is_null(self):
        from bulk_post.variables import _NULL

        self.assertIs(self._extract("not json", "$.id"), _NULL)

    def test_empty_body_is_null(self):
        from bulk_post.variables import _NULL

        self.assertIs(self._extract("", "$.id"), _NULL)


class TestRenderScalar(unittest.TestCase):
    def test_string_passthrough(self):
        from bulk_post.variables import _render_scalar

        self.assertEqual(_render_scalar("abc"), "abc")

    def test_int(self):
        from bulk_post.variables import _render_scalar

        self.assertEqual(_render_scalar(42), "42")

    def test_bool_lowercase(self):
        from bulk_post.variables import _render_scalar

        self.assertEqual(_render_scalar(True), "true")
        self.assertEqual(_render_scalar(False), "false")


class TestSubstituteVars(unittest.TestCase):
    def test_replaces_variable(self):
        from bulk_post import substitute_vars

        out, err = substitute_vars("/users/{{$id}}", {"$id": "42"})
        self.assertIsNone(err)
        self.assertEqual(out, "/users/42")

    def test_missing_variable_returns_error(self):
        from bulk_post import substitute_vars

        out, err = substitute_vars("/users/{{$id}}", {})
        self.assertIsNotNone(err)
        self.assertIn("$id", err)

    def test_no_variables_passthrough(self):
        from bulk_post import substitute_vars

        out, err = substitute_vars("/users/{{id}}", {})
        self.assertIsNone(err)
        self.assertEqual(out, "/users/{{id}}")  # {{id}} is a column, not a var

    def test_multiple_distinct_vars(self):
        from bulk_post import substitute_vars

        out, err = substitute_vars("{{$a}}/{{$b}}", {"$a": "1", "$b": "2"})
        self.assertIsNone(err)
        self.assertEqual(out, "1/2")

    def test_repeated_var(self):
        from bulk_post import substitute_vars

        out, err = substitute_vars("{{$id}}-{{$id}}", {"$id": "9"})
        self.assertIsNone(err)
        self.assertEqual(out, "9-9")

    def test_missing_error_lists_all_missing(self):
        from bulk_post import substitute_vars

        out, err = substitute_vars("{{$a}}{{$b}}", {})
        self.assertIsNotNone(err)
        self.assertIn("$a", err)
        self.assertIn("$b", err)


class TestRenderTemplate(unittest.TestCase):
    def test_columns_then_vars(self):
        from bulk_post import render_template

        out, err = render_template(
            "/{{region}}/users/{{$id}}", {"region": "eu"}, {"$id": "7"}
        )
        self.assertIsNone(err)
        self.assertEqual(out, "/eu/users/7")

    def test_var_value_with_braces_not_reexpanded(self):
        from bulk_post import render_template

        out, err = render_template("/{{$x}}", {"region": "eu"}, {"$x": "{{region}}"})
        self.assertIsNone(err)
        self.assertEqual(out, "/{{region}}")  # var value is NOT re-scanned

    def test_missing_column_error_propagates(self):
        from bulk_post import render_template

        out, err = render_template("/{{region}}", {}, {})
        self.assertIsNotNone(err)

    def test_var_adjacent_to_column(self):
        from bulk_post import render_template

        out, err = render_template("{{region}}{{$id}}", {"region": "eu"}, {"$id": "7"})
        self.assertIsNone(err)
        self.assertEqual(out, "eu7")


class TestResolveVariables(unittest.TestCase):
    def _step(self, *vardefs):
        from bulk_post import WorkflowStep

        s = WorkflowStep(
            path="g/b",
            url="",
            method="GET",
            body=None,
            content_type="application/json",
            headers={},
            auth_type="none",
            auth_raw="",
            on_error="stop",
            variables={v.name: v for v in vardefs},
        )
        return s

    def test_live_scalar(self):
        from bulk_post import VarDef, resolve_variables

        v = VarDef("$id", "g/a", "$.id", nullable=False)
        vals, err = resolve_variables(self._step(v), {"g/a": '{"id": 42}'}, {})
        self.assertIsNone(err)
        self.assertEqual(vals, {"$id": "42"})

    def test_null_nullable_true_empty(self):
        from bulk_post import VarDef, resolve_variables

        v = VarDef("$id", "g/a", "$.missing", nullable=True)
        vals, err = resolve_variables(self._step(v), {"g/a": "{}"}, {})
        self.assertIsNone(err)
        self.assertEqual(vals, {"$id": ""})

    def test_null_nullable_false_errors(self):
        from bulk_post import VarDef, resolve_variables

        v = VarDef("$id", "g/a", "$.missing", nullable=False)
        vals, err = resolve_variables(self._step(v), {"g/a": "{}"}, {})
        self.assertIsNotNone(err)
        self.assertIn("$id", err)

    def test_nonscalar_errors(self):
        from bulk_post import VarDef, resolve_variables

        v = VarDef("$id", "g/a", "$.obj", nullable=True)
        vals, err = resolve_variables(self._step(v), {"g/a": '{"obj": {"k": 1}}'}, {})
        self.assertIsNotNone(err)
        self.assertIn("non-scalar", err)

    def test_persisted_value_used_when_source_absent(self):
        from bulk_post import VarDef, resolve_variables

        v = VarDef("$id", "g/a", "$.id", nullable=False)
        row = {"_bulk_post_var/g/a/$id": "99"}
        vals, err = resolve_variables(self._step(v), {}, row)
        self.assertIsNone(err)
        self.assertEqual(vals, {"$id": "99"})

    def test_absent_source_no_persist_applies_nullable(self):
        from bulk_post import VarDef, resolve_variables

        v = VarDef("$id", "g/a", "$.id", nullable=True)
        vals, err = resolve_variables(self._step(v), {}, {})
        self.assertIsNone(err)
        self.assertEqual(vals, {"$id": ""})

    def test_empty_persisted_column_applies_nullable(self):
        from bulk_post import VarDef, resolve_variables

        # An empty persisted column is treated as absent (null), so a
        # non-nullable variable errors rather than resolving to "".
        v = VarDef("$id", "g/a", "$.id", nullable=False)
        row = {"_bulk_post_var/g/a/$id": ""}
        vals, err = resolve_variables(self._step(v), {}, row)
        self.assertIsNotNone(err)
        self.assertIn("$id", err)


class TestPersistVars(unittest.TestCase):
    def _step(self, *vardefs):
        from bulk_post import WorkflowStep

        return WorkflowStep(
            path="g/b",
            url="",
            method="GET",
            body=None,
            content_type="application/json",
            headers={},
            auth_type="none",
            auth_raw="",
            on_error="stop",
            variables={v.name: v for v in vardefs},
        )

    def test_persists_rendered_scalar(self):
        from bulk_post import VarDef, persist_vars

        v = VarDef("$id", "g/a", "$.id", nullable=True)
        out = persist_vars([self._step(v)], {"g/a": '{"id": 42}'})
        self.assertEqual(out, {"_bulk_post_var/g/a/$id": "42"})

    def test_skips_when_source_not_run(self):
        from bulk_post import VarDef, persist_vars

        v = VarDef("$id", "g/a", "$.id", nullable=True)
        out = persist_vars([self._step(v)], {})
        self.assertEqual(out, {})

    def test_skips_null_match(self):
        from bulk_post import VarDef, persist_vars

        v = VarDef("$id", "g/a", "$.missing", nullable=True)
        out = persist_vars([self._step(v)], {"g/a": "{}"})
        self.assertEqual(out, {})


class TestParseWorkflowVariables(unittest.TestCase):
    def _yaml(self, content):
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode="w", suffix=".yaml", delete=False, prefix="wfvar"
        )
        tmp.write(content)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    _GOOD = """
workflow:
  groupA:
    endpoints:
      - create:
          url: https://api/{{id}}
          method: POST
  groupB:
    variables:
      $newId:
        source: .workflow.groupA.create
        jsonPath: $.id
        nullable: false
    endpoints:
      - use:
          url: https://api/use/{{$newId}}
          method: POST
"""

    def test_valid_variable_attached_to_step(self):
        path = self._yaml(self._GOOD)
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNone(err)
        use = next(s for s in steps if s.path == "groupB/use")
        self.assertIn("$newId", use.variables)
        v = use.variables["$newId"]
        self.assertEqual(v.source_path, "groupA/create")
        self.assertEqual(v.json_path, "$.id")
        self.assertFalse(v.nullable)

    def test_nullable_defaults_true(self):
        path = self._yaml(self._GOOD.replace("        nullable: false\n", ""))
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNone(err)
        use = next(s for s in steps if s.path == "groupB/use")
        self.assertTrue(use.variables["$newId"].nullable)

    def test_endpoint_overrides_group_variable(self):
        path = self._yaml(
            """
workflow:
  groupA:
    endpoints:
      - create:
          url: https://api/x
          method: POST
  groupB:
    variables:
      $v:
        source: .workflow.groupA.create
        jsonPath: $.a
    endpoints:
      - use:
          url: https://api/{{$v}}
          method: POST
          variables:
            $v:
              source: .workflow.groupA.create
              jsonPath: $.b
"""
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNone(err)
        use = next(s for s in steps if s.path == "groupB/use")
        self.assertEqual(use.variables["$v"].json_path, "$.b")

    def test_name_without_dollar_errors(self):
        path = self._yaml(self._GOOD.replace("$newId", "newId"))
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNotNone(err)
        self.assertIn("$", err)

    def test_undefined_reference_errors(self):
        path = self._yaml(
            self._GOOD.replace(
                "https://api/use/{{$newId}}", "https://api/use/{{$ghost}}"
            )
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNotNone(err)
        self.assertIn("$ghost", err)

    def test_forward_reference_errors(self):
        # $newId in groupA references groupB/use which runs LATER
        path = self._yaml(
            """
workflow:
  groupA:
    variables:
      $x:
        source: .workflow.groupB.use
        jsonPath: $.id
    endpoints:
      - create:
          url: https://api/{{$x}}
          method: POST
  groupB:
    endpoints:
      - use:
          url: https://api/x
          method: POST
"""
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNotNone(err)

    def test_unknown_source_errors(self):
        path = self._yaml(
            self._GOOD.replace(".workflow.groupA.create", ".workflow.groupA.nope")
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNotNone(err)

    def test_invalid_jsonpath_errors(self):
        path = self._yaml(self._GOOD.replace("jsonPath: $.id", "jsonPath: '$.['"))
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNotNone(err)

    def test_missing_jsonpath_ng_reports_install_error(self):
        # Simulate jsonpath-ng not installed: a None entry in sys.modules makes
        # `import jsonpath_ng` raise ImportError, which validate_jsonpath converts
        # into a clean install message (mirrors the pyyaml-missing behavior).
        path = self._yaml(self._GOOD)
        with patch.dict(sys.modules, {"jsonpath_ng": None}):
            steps, err = bulk_post.parse_workflow(path)
        self.assertIsNotNone(err)
        self.assertIn("jsonpath-ng", err)

    def test_variable_referenced_in_body_and_header_validates(self):
        # A {{$var}} used in body and header (not just the URL) must be
        # recognized as an in-scope reference (no "undefined variable" error).
        path = self._yaml(
            """
workflow:
  groupA:
    endpoints:
      - create:
          url: https://api/x
          method: POST
  groupB:
    variables:
      $v:
        source: .workflow.groupA.create
        jsonPath: $.id
    endpoints:
      - use:
          url: https://api/use
          method: POST
          headers:
            X-Ref: "{{$v}}"
          body: '{"ref": "{{$v}}"}'
"""
        )
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNone(err)
        use = next(s for s in steps if s.path == "groupB/use")
        self.assertIn("$v", use.variables)

    def test_group_variable_inherited_by_endpoint(self):
        # A group-level variable is attached to an endpoint that does not
        # declare its own variables block.
        path = self._yaml(self._GOOD)
        steps, err = bulk_post.parse_workflow(path)
        self.assertIsNone(err)
        use = next(s for s in steps if s.path == "groupB/use")
        self.assertEqual(use.variables["$newId"].source_path, "groupA/create")


class TestWorkflowVarColumns(unittest.TestCase):
    def test_dedup_and_order(self):
        from bulk_post import VarDef, WorkflowStep, workflow_var_columns

        def step(path, *vs):
            return WorkflowStep(
                path=path,
                url="",
                method="GET",
                body=None,
                content_type="application/json",
                headers={},
                auth_type="none",
                auth_raw="",
                on_error="stop",
                variables={v.name: v for v in vs},
            )

        a = VarDef("$x", "g/a", "$.x", True)
        b = VarDef("$y", "g/a", "$.y", True)
        cols = workflow_var_columns([step("g/b", a), step("g/c", a, b)])
        self.assertEqual(cols, ["_bulk_post_var/g/a/$x", "_bulk_post_var/g/a/$y"])


class TestFireWorkflowStepVariables(unittest.TestCase):
    def _step(self, url, variables):
        from bulk_post import WorkflowStep

        return WorkflowStep(
            path="g/b",
            url=url,
            method="GET",
            body=None,
            content_type="application/json",
            headers={},
            auth_type="none",
            auth_raw="",
            on_error="stop",
            variables=variables,
        )

    def test_variable_substituted_into_url(self):
        from bulk_post import VarDef, _fire_workflow_step

        v = VarDef("$id", "g/a", "$.id", nullable=False)
        step = self._step("https://api/use/{{$id}}", {"$id": v})
        responses = {"g/a": '{"id": 42}'}

        with patch("bulk_post.workflow.http_request") as mock_http:
            mock_http.return_value = (200, "ok", 0.01, {}, {})
            result = _fire_workflow_step(step, {}, None, 30, responses=responses)
        # final_url is index 3 of the returned tuple
        self.assertEqual(result[3], "https://api/use/42")
        mock_http.assert_called_once()
        self.assertEqual(mock_http.call_args[0][0], "https://api/use/42")

    def test_non_nullable_null_is_skip_error(self):
        from bulk_post import VarDef, _fire_workflow_step

        v = VarDef("$id", "g/a", "$.missing", nullable=False)
        step = self._step("https://api/use/{{$id}}", {"$id": v})

        with patch("bulk_post.workflow.http_request") as mock_http:
            result = _fire_workflow_step(step, {}, None, 30, responses={"g/a": "{}"})
        status, body, _, url = result[0], result[1], result[2], result[3]
        self.assertIsNone(status)
        self.assertEqual(url, "")  # routed as a substitution SKIP
        self.assertIn("$id", body)
        mock_http.assert_not_called()

    def test_variable_substituted_into_body_and_header(self):
        from bulk_post import VarDef, WorkflowStep, _fire_workflow_step

        v = VarDef("$id", "g/a", "$.id", nullable=False)
        step = WorkflowStep(
            path="g/b",
            url="https://api/use",
            method="POST",
            body='{"ref": "{{$id}}"}',
            content_type="application/json",
            headers={"X-Ref": "{{$id}}"},
            auth_type="none",
            auth_raw="",
            on_error="stop",
            variables={"$id": v},
        )
        with patch("bulk_post.workflow.http_request") as mock_http:
            mock_http.return_value = (200, "ok", 0.01, {}, {})
            _fire_workflow_step(step, {}, None, 30, responses={"g/a": '{"id": 42}'})
        # http_request(url, auth, method, body, timeout, content_type, extra_headers)
        args = mock_http.call_args[0]
        self.assertEqual(args[3], '{"ref": "42"}')  # req_body
        self.assertEqual(args[6], {"X-Ref": "42"})  # extra_headers


class TestWorkflowRunnerVariables(unittest.TestCase):
    def _args(self):
        ns = argparse.Namespace()
        ns.timeout = 30
        ns.verbose = False
        ns.delay = 0
        ns.debug = False
        ns.parallel = False
        return ns

    def _steps(self):
        from bulk_post import VarDef, WorkflowStep

        a = WorkflowStep(
            path="groupA/create",
            url="https://api/create",
            method="POST",
            body=None,
            content_type="application/json",
            headers={},
            auth_type="none",
            auth_raw="",
            on_error="stop",
            variables={},
        )
        b = WorkflowStep(
            path="groupB/use",
            url="https://api/use/{{$id}}",
            method="POST",
            body=None,
            content_type="application/json",
            headers={},
            auth_type="none",
            auth_raw="",
            on_error="stop",
            variables={"$id": VarDef("$id", "groupA/create", "$.id", False)},
        )
        return [a, b]

    def test_second_step_uses_first_response(self):
        import io

        from bulk_post import _run_workflow_loop

        reader = [{"x": "1"}]  # one row; DictReader-like iterable of dicts
        calls = []

        def fake_http(url, auth, method, body, timeout, content_type, extra):
            calls.append(url)
            if url == "https://api/create":
                return (200, '{"id": 555}', 0.01, {}, {})
            return (200, "done", 0.01, {}, {})

        log = io.StringIO()
        retry = MagicMock()
        with patch("bulk_post.workflow.http_request", side_effect=fake_http):
            ok, failed, processed = _run_workflow_loop(
                iter(reader),
                self._steps(),
                self._args(),
                {},
                None,
                None,
                None,
                retry,
                log,
                0,
                1,
                ["x", "_bulk_post_step"],
            )
        self.assertEqual((ok, failed), (1, 0))
        self.assertIn("https://api/use/555", calls)


# ---------------------------------------------------------------------------
# CLI end-to-end: workflow variables
# ---------------------------------------------------------------------------


class TestCliWorkflowVariablesEndToEnd(unittest.TestCase):
    _WF = """
workflow:
  groupA:
    endpoints:
      - create:
          url: https://api/create
          method: POST
  groupB:
    variables:
      $id:
        source: .workflow.groupA.create
        jsonPath: $.id
        nullable: false
    endpoints:
      - use:
          url: https://api/use/{{$id}}
          method: POST
"""

    def _write(self, suffix, content, prefix="t"):
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode="w", suffix=suffix, delete=False, prefix=prefix
        )
        tmp.write(content)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def test_run_succeeds_with_chained_variable(self):
        csv_path = self._write(".csv", "x\n1\n", prefix="rows")
        wf_path = self._write(".yaml", self._WF, prefix="wf")
        calls = []

        def fake_http(url, auth, method, body, timeout, content_type, extra):
            calls.append(url)
            if url == "https://api/create":
                return (200, '{"id": 7}', 0.01, {}, {})
            return (200, "ok", 0.01, {}, {})

        with (
            patch("sys.stdin") as stdin,
            patch("bulk_post.workflow.http_request", side_effect=fake_http),
        ):
            stdin.isatty.return_value = False
            code = bulk_post.main(["-w", wf_path, "-c", csv_path])
        self.assertEqual(code, 0)
        self.assertIn("https://api/use/7", calls)

    def test_failure_persists_variable_column(self):
        csv_path = self._write(".csv", "x\n1\n", prefix="rows")
        wf_path = self._write(".yaml", self._WF, prefix="wf")
        retry_path = Path(csv_path).with_name(Path(csv_path).stem + "_failed.csv")
        self.addCleanup(lambda: retry_path.unlink(missing_ok=True))
        self.addCleanup(lambda: retry_path.with_suffix(".log").unlink(missing_ok=True))

        def fake_http(url, auth, method, body, timeout, content_type, extra):
            if url == "https://api/create":
                return (200, '{"id": 7}', 0.01, {}, {})
            return (500, "boom", 0.01, {}, {})  # step B fails -> row written to retry

        with (
            patch("sys.stdin") as stdin,
            patch("bulk_post.workflow.http_request", side_effect=fake_http),
        ):
            stdin.isatty.return_value = False
            code = bulk_post.main(["-w", wf_path, "-c", csv_path])

        self.assertEqual(code, 1)
        with open(retry_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["_bulk_post_var/groupA/create/$id"], "7")
        self.assertEqual(rows[0]["_bulk_post_step"], "groupB/use")


class TestWorkflowResumeVariables(unittest.TestCase):
    def _args(self):
        ns = argparse.Namespace()
        ns.timeout = 30
        ns.verbose = False
        ns.delay = 0
        ns.debug = False
        ns.parallel = False
        return ns

    def _write(self, suffix, content, prefix="t"):
        tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
            mode="w", suffix=suffix, delete=False, prefix=prefix
        )
        tmp.write(content)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def test_resumed_step_reads_persisted_variable(self):
        import io

        from bulk_post import VarDef, WorkflowStep, _run_workflow_loop

        a = WorkflowStep(
            path="groupA/create",
            url="https://api/create",
            method="POST",
            body=None,
            content_type="application/json",
            headers={},
            auth_type="none",
            auth_raw="",
            on_error="stop",
            variables={},
        )
        b = WorkflowStep(
            path="groupB/use",
            url="https://api/use/{{$id}}",
            method="POST",
            body=None,
            content_type="application/json",
            headers={},
            auth_type="none",
            auth_raw="",
            on_error="stop",
            variables={"$id": VarDef("$id", "groupA/create", "$.id", False)},
        )
        # Resume row: _bulk_post_step points at groupB/use; $id is persisted.
        row = {
            "x": "1",
            "_bulk_post_var/groupA/create/$id": "321",
            "_bulk_post_step": "groupB/use",
        }
        calls = []

        def fake_http(url, auth, method, body, timeout, content_type, extra):
            calls.append(url)
            return (200, "ok", 0.01, {}, {})

        log = io.StringIO()
        with patch("bulk_post.workflow.http_request", side_effect=fake_http):
            ok, failed, processed = _run_workflow_loop(
                iter([row]),
                [a, b],
                self._args(),
                {},
                None,
                None,
                None,
                MagicMock(),
                log,
                0,
                1,
                ["x", "_bulk_post_var/groupA/create/$id", "_bulk_post_step"],
            )
        self.assertEqual((ok, failed), (1, 0))
        # groupA/create is skipped on resume; only groupB/use fires, with $id=321
        self.assertEqual(calls, ["https://api/use/321"])

    def test_cli_resume_round_trip_reads_persisted_var(self):
        # A retry-style CSV whose header already carries the reserved columns,
        # fed back through main(): groupA/create is skipped (resume marker),
        # $id comes from the persisted column, run succeeds.
        wf = """
workflow:
  groupA:
    endpoints:
      - create:
          url: https://api/create
          method: POST
  groupB:
    variables:
      $id:
        source: .workflow.groupA.create
        jsonPath: $.id
        nullable: false
    endpoints:
      - use:
          url: https://api/use/{{$id}}
          method: POST
"""
        csv_content = (
            "x,_bulk_post_var/groupA/create/$id,_bulk_post_step\n1,321,groupB/use\n"
        )
        csv_path = self._write(".csv", csv_content, prefix="resume")
        wf_path = self._write(".yaml", wf, prefix="wf")
        calls = []

        def fake_http(url, auth, method, body, timeout, content_type, extra):
            calls.append(url)
            return (200, "ok", 0.01, {}, {})

        with (
            patch("sys.stdin") as stdin,
            patch("bulk_post.workflow.http_request", side_effect=fake_http),
        ):
            stdin.isatty.return_value = False
            code = bulk_post.main(["-w", wf_path, "-c", csv_path])

        self.assertEqual(code, 0)
        # groupA/create is skipped; only the resumed step fires, using $id=321
        self.assertEqual(calls, ["https://api/use/321"])


if __name__ == "__main__":
    unittest.main()
