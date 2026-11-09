# SPDX-License-Identifier: AGPL-3.0-or-later
"""census.http: polite census.gov downloads (retries, pacing, firewall page, manual files).

All offline: fake sessions, a recorded sleep, and a mock clock; nothing waits.
"""

import hashlib
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

import requests

from census import boundaries as bd
from census import http as H
from census import replicate as rp

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "vre")
F2023 = os.path.join(FIXTURES, "B19001_040_2023_subset.csv.zip")

# The page census.gov's firewall serves (HTTP 200, text/html, 247 bytes) for the 2019 Michigan tract
# B01003 VRE file, with the support ID replaced.
REJECTED_PAGE = (
    b"<html><head><title>Request Rejected</title></head><body>The requested URL was rejected. "
    b"Please consult with your administrator.<br><br>Your support ID is: 1234567890123456789<br><br>"
    b"<a href='javascript:history.back();'>[Go Back]</a></body></html>"
)


class FakeResponse:
    def __init__(self, content=b"ok", status=200, headers=None):
        self.content = content
        self.status_code = status
        self.headers = {"Content-Type": "application/zip", **(headers or {})}


class FakeSession:
    """Answers each GET with the next item; an exception item is raised."""

    def __init__(self, items):
        self.items = list(items)
        self.calls = []

    def get(self, url, headers=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "headers": dict(headers or {}), "timeout": timeout, **kwargs})
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class HttpTestCase(unittest.TestCase):
    """Records sleeps instead of sleeping; jitter fixed at 1; default env values unless a test sets them."""

    env = {"CENSUS_WWW2_MIN_INTERVAL": "0", "CENSUS_WWW2_BACKOFF_BASE": "2",
           "CENSUS_WWW2_ATTEMPTS": "4", "CENSUS_WWW2_REJECTED_RETRY_DELAY": "0"}

    def setUp(self):
        self.sleeps = []
        self.now = [1000.0]
        patches = [
            mock.patch.dict(os.environ, self.env),
            mock.patch.object(H, "_sleep", self._sleep),
            mock.patch.object(H, "_monotonic", lambda: self.now[0]),
            mock.patch.object(H, "_jitter", lambda: 1.0),
            mock.patch.dict(H._last_request, clear=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _sleep(self, secs):
        self.sleeps.append(secs)
        self.now[0] += secs


class TestBackoff(HttpTestCase):
    def test_exponential_sequence_then_success(self):
        s = FakeSession([FakeResponse(status=503), FakeResponse(status=502), FakeResponse(status=500),
                         FakeResponse(b"data")])
        resp = H.polite_get("https://www2.census.gov/x", user_agent="ua/1", session=s)
        self.assertEqual(resp.content, b"data")
        self.assertEqual(self.sleeps, [2, 4, 8])
        self.assertEqual(len(s.calls), 4)

    def test_cap_and_jitter(self):
        with mock.patch.dict(os.environ, {"CENSUS_WWW2_ATTEMPTS": "8"}):
            s = FakeSession([FakeResponse(status=503)] * 7 + [FakeResponse()])
            H.polite_get("https://www2.census.gov/x", user_agent="ua/1", session=s)
        self.assertEqual(self.sleeps, [2, 4, 8, 16, 32, 60, 60])
        with mock.patch.object(H, "_jitter", lambda: 0.5):
            self.assertEqual(H.backoff_delay(0), 1.0)

    def test_exhausted_status_returned_to_caller(self):
        s = FakeSession([FakeResponse(status=503)] * 4)
        self.assertEqual(H.polite_get("https://www2.census.gov/x", user_agent="ua/1", session=s).status_code, 503)
        self.assertEqual(len(s.calls), 4)
        self.assertEqual(len(self.sleeps), 3)
        # The module's own error text is kept.
        s = FakeSession([FakeResponse(status=503)] * 4)
        with self.assertRaisesRegex(rp.VREDownloadError, "HTTP 503"):
            rp._download("https://www2.census.gov/x", session=s)

    def test_not_retried(self):
        for status in (404, 403, 400):
            s = FakeSession([FakeResponse(status=status)])
            self.assertEqual(H.polite_get("https://www2.census.gov/x", user_agent="ua", session=s).status_code,
                             status)
        self.assertEqual(self.sleeps, [])

    def test_connection_errors_and_timeouts(self):
        s = FakeSession([requests.ConnectionError("reset"), requests.Timeout("slow"), FakeResponse(b"d")])
        self.assertEqual(H.polite_get("https://www2.census.gov/x", user_agent="ua", session=s).content, b"d")
        self.assertEqual(self.sleeps, [2, 4])
        s = FakeSession([requests.ConnectionError("down")] * 4)
        with self.assertRaisesRegex(rp.VREDownloadError, "after 4 attempt"):
            rp._download("https://www2.census.gov/x", session=s)
        self.assertEqual(len(s.calls), 4)

    def test_honest_user_agent(self):
        s = FakeSession([FakeResponse()])
        rp._download("https://www2.census.gov/x", session=s)
        ua = s.calls[0]["headers"]["User-Agent"]
        self.assertEqual(ua, rp.USER_AGENT)
        self.assertNotIn("Mozilla", ua)


class TestRetryAfter(HttpTestCase):
    def test_seconds_header_is_honoured(self):
        s = FakeSession([FakeResponse(status=429, headers={"Retry-After": "7"}), FakeResponse()])
        H.polite_get("https://www2.census.gov/x", user_agent="ua", session=s)
        self.assertEqual(self.sleeps, [7.0])

    def test_parse(self):
        self.assertEqual(H.retry_after_seconds("12"), 12.0)
        self.assertEqual(H.retry_after_seconds("100000"), H.RETRY_AFTER_CAP)
        self.assertIsNone(H.retry_after_seconds(None))
        self.assertIsNone(H.retry_after_seconds("soon"))
        self.assertEqual(H.retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT"), 0.0)  # in the past


class TestRejectedPage(HttpTestCase):
    env = {**HttpTestCase.env, "CENSUS_WWW2_REJECTED_RETRY_DELAY": "30"}

    def test_support_id(self):
        self.assertEqual(H.rejection_support_id(REJECTED_PAGE), "1234567890123456789")
        self.assertEqual(H.rejection_support_id(b"<title>Request Rejected</title>"), "")
        self.assertIsNone(H.rejection_support_id(b"PK\x03\x04 data"))

    def test_one_delayed_retry_then_clear_error(self):
        url = rp.vre_url("B01003", 2019, "tract", "26")
        page = FakeResponse(REJECTED_PAGE, headers={"Content-Type": "text/html"})
        s = FakeSession([page, page, FakeResponse(b"never reached")])
        with self.assertRaises(rp.VREDownloadError) as ctx:
            rp.fetch_vre("B01003", 2019, "tract", "26", session=s, cache_dir=self.tmp, check_moe=False)
        exc = ctx.exception
        path = os.path.join(self.tmp, "140", "B01003_26.csv.zip")
        self.assertEqual(len(s.calls), 2)  # never loops
        self.assertEqual(self.sleeps, [30.0])
        self.assertEqual(exc.support_id, "1234567890123456789")
        self.assertEqual(exc.cache_path, path)
        self.assertTrue(exc.rejected)
        msg = str(exc)
        for part in ("1234567890123456789", url, path, "browser", "persistent", "manual download"):
            self.assertIn(part, msg)
        self.assertFalse(os.path.exists(path))

    def test_retry_can_succeed(self):
        with open(F2023, "rb") as f:
            raw = f.read()
        page = FakeResponse(REJECTED_PAGE, headers={"Content-Type": "text/html"})
        s = FakeSession([page, FakeResponse(raw)])
        frame = rp.fetch_vre("B19001", 2023, "state", session=s, cache_dir=self.tmp)
        self.assertEqual(len(frame), 85)
        self.assertEqual(frame.attrs["acquisition"], "download")

    def test_other_modules_raise_their_own_error(self):
        page = FakeResponse(REJECTED_PAGE, headers={"Content-Type": "text/html"})
        with self.assertRaises(bd.BoundaryError) as ctx:
            bd._download("https://www2.census.gov/geo/tiger/x.zip", session=FakeSession([page, page]),
                         cache_path="/c/x.zip")
        self.assertIn("/c/x.zip", str(ctx.exception))


class TestManualFile(HttpTestCase):
    def test_vre_manual_file_is_adopted(self):
        with open(F2023, "rb") as f:
            raw = f.read()
        url = rp.vre_url("B19001", 2023, "state")
        path = os.path.join(self.tmp, "040", "B19001.csv.zip")
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as f:
            f.write(raw)
        s = FakeSession([])  # any request would fail
        frame = rp.fetch_vre("B19001", 2023, "state", session=s, cache_dir=self.tmp)
        self.assertEqual(s.calls, [])
        self.assertEqual(len(frame), 85)
        self.assertEqual(frame.attrs["acquisition"], H.MANUAL_SOURCE)
        self.assertEqual(frame.attrs["sha256"], hashlib.sha256(raw).hexdigest())
        with open(path + ".json", encoding="utf-8") as f:
            meta = json.load(f)
        self.assertEqual(meta["source"], "manual download")
        self.assertEqual(meta["source_url"], url)
        self.assertEqual(meta["sha256"], hashlib.sha256(raw).hexdigest())
        # Later reads use the sidecar like any download.
        frame2 = rp.fetch_vre("B19001", 2023, "state", session=s, cache_dir=self.tmp)
        self.assertEqual(frame2.attrs["acquisition"], H.MANUAL_SOURCE)

    def test_saved_html_page_is_refused(self):
        path = os.path.join(self.tmp, "040", "B19001.csv.zip")
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as f:
            f.write(REJECTED_PAGE)
        with self.assertRaisesRegex(rp.VREDownloadError, "Manually placed file"):
            rp.fetch_vre("B19001", 2023, "state", session=FakeSession([]), cache_dir=self.tmp)
        self.assertFalse(os.path.exists(path + ".json"))

    def test_boundary_manual_zip(self):
        path = os.path.join(self.tmp, "cb.zip")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("a.txt", "x")
        meta = bd._read_cached(path, "https://www2.census.gov/geo/tiger/cb.zip")
        self.assertEqual(meta["source"], H.MANUAL_SOURCE)
        bad = os.path.join(self.tmp, "bad.zip")
        with open(bad, "wb") as f:
            f.write(b"<html>")
        with self.assertRaises(bd.BoundaryError):
            bd._read_cached(bad, "https://www2.census.gov/geo/tiger/bad.zip")

    def test_no_adoption_without_url_or_with_sidecar(self):
        path = os.path.join(self.tmp, "f.bin")
        with open(path, "wb") as f:
            f.write(b"x")
        self.assertIsNone(rp._read_cached(path))
        self.assertFalse(os.path.exists(path + ".json"))
        with open(path + ".json", "w") as f:
            json.dump({"sha256": "other"}, f)
        self.assertIsNone(H.adopt_manual_file(path, "https://www2.census.gov/f.bin"))


class TestPacing(HttpTestCase):
    env = {**HttpTestCase.env, "CENSUS_WWW2_MIN_INTERVAL": "1"}

    def test_min_interval_per_host(self):
        s = FakeSession([FakeResponse()] * 4)
        H.polite_get("https://www2.census.gov/a", user_agent="ua", session=s)
        self.assertEqual(self.sleeps, [])
        self.now[0] += 0.25
        H.polite_get("https://www2.census.gov/b", user_agent="ua", session=s)
        self.assertEqual(self.sleeps, [0.75])
        H.polite_get("https://www.census.gov/c", user_agent="ua", session=s)  # other host: no wait
        self.assertEqual(self.sleeps, [0.75])
        self.now[0] += 5
        H.polite_get("https://www2.census.gov/d", user_agent="ua", session=s)
        self.assertEqual(self.sleeps, [0.75])

    def test_configurable(self):
        with mock.patch.dict(os.environ, {"CENSUS_WWW2_MIN_INTERVAL": "3"}):
            s = FakeSession([FakeResponse()] * 2)
            H.polite_get("https://www2.census.gov/a", user_agent="ua", session=s)
            H.polite_get("https://www2.census.gov/a", user_agent="ua", session=s)
        self.assertEqual(self.sleeps, [3.0])


if __name__ == "__main__":
    unittest.main()
