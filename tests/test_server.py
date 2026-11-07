#!/usr/bin/env python3
"""Server tests, end to end, against a real instance started in this process.

The original suite could only run these against a server someone had already
started by hand, so they never ran in CI and the HTTP surface was the untested
part. Here the server is bound on an ephemeral port in setUpClass, with its state
directory in a temporary folder, so the whole thing runs offline.

Nothing here touches the network: 127.0.0.1 only.

    python3 -m unittest discover -s tests
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

from fixtures import SKELETON, many_xlf, sample_xlf, sample_xlz  # noqa: E402
from xliffedit import XliffDoc  # noqa: E402
from xliffedit import server as httpd  # noqa: E402


class Live(unittest.TestCase):
    """A running instance, one per test class run."""

    @classmethod
    def setUpClass(cls):
        # The access log is useful when running the editor and noise in a test
        # report, so silence it here rather than adding a flag to the server.
        httpd.Handler.log_message = lambda *args, **kwargs: None
        cls.tmp = tempfile.mkdtemp(prefix="xliff-test-")
        httpd.DATA = os.path.join(cls.tmp, "data")
        httpd.OPEN_DOCS.clear()
        cls.server = httpd.make_server("127.0.0.1", 0)
        cls.url = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.password = httpd.get_password()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=10)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ------------------------------------------------------------------ helper
    def call(self, path, data=None, method=None, ctype=None, raw=False,
             user="translator", password=None, csrf=True, extra=None):
        request = urllib.request.Request(
            self.url + path, data=data,
            method=method or ("POST" if data is not None else "GET"))
        credentials = "%s:%s" % (user, self.password if password is None else password)
        request.add_header("Authorization", "Basic " + base64.b64encode(
            credentials.encode("utf-8")).decode("ascii"))
        if csrf:
            request.add_header("X-XLIFF-Edit", "1")
        if ctype:
            request.add_header("Content-Type", ctype)
        for key, value in (extra or {}).items():
            request.add_header(key, value)
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read()
        return (response.status, body) if raw else (response.status, json.loads(body))

    def upload(self, blob=None, name="t.xlz"):
        status, meta = self.call("/api/upload?name=" + urllib.parse.quote(name),
                                 data=blob if blob is not None else sample_xlz(),
                                 ctype="application/octet-stream")
        self.assertEqual(status, 200)
        return meta


class Authentication(Live):
    def test_no_credentials_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.url + "/api/health", timeout=10)
        self.assertEqual(caught.exception.code, 401)

    def test_a_wrong_password_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.call("/api/health", password="wrong")
        self.assertEqual(caught.exception.code, 401)

    def test_a_wrong_user_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.call("/api/health", user="stranger")
        self.assertEqual(caught.exception.code, 401)

    def test_non_ascii_credentials_do_not_crash_the_server(self):
        """compare_digest raises TypeError on non ASCII strings, which turned a
        wrong password into a dropped connection instead of a 401."""
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.call("/api/health", user="żółw", password="ąć")
        self.assertEqual(caught.exception.code, 401)

    def test_health_answers_when_authenticated(self):
        status, body = self.call("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))


class CrossSiteProtection(Live):
    def test_a_post_without_the_header_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.call("/api/upload?name=t.xlz", data=sample_xlz(),
                      ctype="application/octet-stream", csrf=False)
        self.assertEqual(caught.exception.code, 403)

    def test_a_post_from_a_foreign_origin_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.call("/api/upload?name=t.xlz", data=sample_xlz(),
                      ctype="application/octet-stream",
                      extra={"Origin": "https://evil.example"})
        self.assertEqual(caught.exception.code, 403)


class Lifecycle(Live):
    def setUp(self):
        self.blob = sample_xlz()
        self.meta = self.upload(self.blob, name="Dostawa ŁÓDŹ ąę.xlz")
        self.fid = self.meta["id"]

    def test_upload_reports_the_segment_count(self):
        self.assertEqual(self.meta["units"], 11)
        self.assertTrue(self.meta["is_zip"])

    def test_a_polish_file_name_survives_upload(self):
        self.assertIn("ŁÓDŹ", self.meta["name"])

    def test_the_segment_list_and_its_statistics(self):
        status, body = self.call("/api/file/%s?limit=50&filter=all" % self.fid)
        self.assertEqual(status, 200)
        self.assertEqual(body["total"], 11)
        self.assertEqual(len(body["units"]), 11)
        self.assertEqual(body["stats"]["translatable"], 10)
        self.assertTrue(all("key" in u for u in body["units"]))

    def test_the_declared_target_language_reaches_the_client(self):
        """The editor sets its spell checker from this. It used to be hardcoded."""
        _, body = self.call("/api/file/%s?limit=1" % self.fid)
        self.assertEqual(body["target_language"], "pl-PL")
        self.assertEqual(body["source_language"], "en-US")

    def test_download_without_an_edit_returns_the_uploaded_bytes(self):
        status, blob = self.call("/api/file/%s/download" % self.fid, raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(blob, self.blob)

    def test_saving_a_segment_then_downloading_changes_only_that_segment(self):
        payload = json.dumps({"key": "0", "text": "Witaj, swiecie!"}).encode("utf-8")
        status, body = self.call("/api/file/%s/segment" % self.fid,
                                 data=payload, ctype="application/json")
        self.assertEqual(status, 200)
        self.assertEqual(body["target_text"], "Witaj, swiecie!")
        self.assertTrue(body["dirty"])

        _, blob = self.call("/api/file/%s/download" % self.fid, raw=True)
        with zipfile.ZipFile(BytesIO(blob)) as archive:
            xlf = archive.read("content.xlf").decode("utf-8")
            self.assertIn("Witaj, swiecie!</target>", xlf)
            self.assertEqual(archive.read("skeleton.skl"), SKELETON)
        original = sample_xlf().decode("utf-8")
        self.assertEqual(xlf.replace("Witaj, swiecie!", "Witaj swiecie."), original)

    def test_saving_a_segment_with_tags(self):
        _, body = self.call("/api/file/%s?limit=50&filter=all" % self.fid)
        unit = next(u for u in body["units"] if u["key"] == "2")
        tags = [t for t in unit["target"] if t["kind"] == "tag"]
        self.assertEqual(len(tags), 2)
        tokens = [{"kind": "tag", "ref": tags[0]["ref"]},
                  {"kind": "text", "text": "Wystawa zimowa"},
                  {"kind": "tag", "ref": tags[1]["ref"]},
                  {"kind": "text", "text": " trasa"}]
        payload = json.dumps({"key": "2", "tokens": tokens}).encode("utf-8")
        status, saved = self.call("/api/file/%s/segment" % self.fid,
                                  data=payload, ctype="application/json")
        self.assertEqual(status, 200)
        self.assertEqual(saved["target_text"], "Wystawa zimowa trasa")

    def test_a_bad_segment_request_is_refused(self):
        for payload in ({"key": "999", "text": "x"},
                        {"key": "1", "text": "x"},
                        {"text": "x"}):
            with self.subTest(payload=payload):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.call("/api/file/%s/segment" % self.fid,
                              data=json.dumps(payload).encode("utf-8"),
                              ctype="application/json")
                self.assertIn(caught.exception.code, (400, 404))

    def test_replace_preview_does_not_save(self):
        payload = json.dumps({"find": "Powtorz", "replace": "Zmien",
                              "dry": True}).encode("utf-8")
        status, body = self.call("/api/file/%s/replace" % self.fid,
                                 data=payload, ctype="application/json")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 2)
        self.assertTrue(body["dry"])
        _, after = self.call("/api/file/%s?limit=50&filter=edited" % self.fid)
        self.assertEqual(after["total"], 0)

    def test_replace_treats_the_replacement_literally(self):
        r"""A backslash sequence in the replacement must not act as a template:
        \1 and \n in a translator's text are text."""
        payload = json.dumps({"find": "Powtorz", "replace": r"A\1B\nC",
                              "dry": False}).encode("utf-8")
        status, body = self.call("/api/file/%s/replace" % self.fid,
                                 data=payload, ctype="application/json")
        self.assertEqual(status, 200)
        _, blob = self.call("/api/file/%s/download" % self.fid, raw=True)
        with zipfile.ZipFile(BytesIO(blob)) as archive:
            xlf = archive.read("content.xlf").decode("utf-8")
        self.assertIn(r"A\1B\nC", xlf)

    def test_quality_checks_run_over_http(self):
        status, body = self.call("/api/file/%s/qa" % self.fid)
        self.assertEqual(status, 200)
        self.assertGreater(body["count"], 0)
        self.assertIn("truncated", body)

    def test_exports(self):
        status, body = self.call("/api/file/%s/export.csv" % self.fid, raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(body.decode("utf-8").count("\n"), 11)
        status, body = self.call("/api/file/%s/export.html" % self.fid, raw=True)
        self.assertEqual(status, 200)
        self.assertIn(b"<table>", body)

    def test_reset_restores_the_original_bytes(self):
        self.call("/api/file/%s/segment" % self.fid,
                  data=json.dumps({"key": "0", "text": "Zmiana"}).encode("utf-8"),
                  ctype="application/json")
        self.call("/api/file/%s/reset" % self.fid, data=b"{}",
                  ctype="application/json")
        _, blob = self.call("/api/file/%s/download" % self.fid, raw=True)
        self.assertEqual(blob, self.blob)


class PathHandling(Live):
    def test_a_traversing_file_id_is_refused(self):
        for bad in ("../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "0" * 15,
                    "zzzzzzzzzzzzzzzz", "%2e%2e%2f"):
            with self.subTest(bad=bad):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.call("/api/file/%s" % bad)
                self.assertIn(caught.exception.code, (400, 404))

    def test_an_unknown_path_is_a_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.call("/api/nothing-here")
        self.assertEqual(caught.exception.code, 404)

    def test_the_interface_is_served(self):
        for path, needle in (("/", b"XLIFF editor"), ("/ui.js", b"TARGET_LANG"),
                             ("/ui.css", b"body")):
            with self.subTest(path=path):
                status, body = self.call(path, raw=True)
                self.assertEqual(status, 200)
                self.assertIn(needle, body)


class Concurrency(Live):
    """Regression. Two concurrent saves destroyed state.json, and the symptom
    was a delivery that silently lost edits."""

    def test_twenty_writers_six_rounds_each(self):
        meta = self.upload(sample_xlz(many_xlf(40)), name="race.xlz")
        fid = meta["id"]
        errors = []

        def writer(index):
            try:
                for round_number in range(6):
                    payload = json.dumps({
                        "key": str(index - 1),
                        "text": "writer %d round %d" % (index, round_number),
                    }).encode("utf-8")
                    self.call("/api/file/%s/segment" % fid, data=payload,
                              ctype="application/json")
            except Exception as error:  # noqa: BLE001
                errors.append("%s: %s" % (index, error))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(1, 21)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        self.assertEqual(errors, [], "; ".join(errors[:3]))
        _, body = self.call("/api/file/%s?limit=1&filter=edited" % fid)
        self.assertEqual(body["total"], 20)

        status, body = self.call("/api/files")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(body["files"]), 1)

        _, blob = self.call("/api/file/%s/download" % fid, raw=True)
        with zipfile.ZipFile(BytesIO(blob)) as archive:
            doc = XliffDoc(archive.read("content.xlf"))
        self.assertEqual(len(doc.units), 40)


if __name__ == "__main__":
    unittest.main(verbosity=2)
