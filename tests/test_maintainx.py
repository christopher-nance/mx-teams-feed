from datetime import UTC, datetime

import pytest

from mx_teams_feed.errors import CredentialsRejected, NotFound, PermanentError, TransientError
from mx_teams_feed.maintainx import MaintainXClient
from mx_teams_feed.testing import FakeTransport, FixedClock

NOW = datetime(2026, 10, 30, 19, 0, tzinfo=UTC)
BASE = "https://api.getmaintainx.com/v1"


def _client(transport, api_key="Bearer tok", **kwargs):
    return MaintainXClient(transport, api_key, FixedClock(NOW), **kwargs)


class TestAuth:
    def test_key_already_carrying_bearer_is_sent_verbatim(self):
        transport = FakeTransport().add("GET", "/users/1", json={"user": {"id": 1}})
        _client(transport, "Bearer tok").get_user(1)
        assert transport.requests[0].headers["Authorization"] == "Bearer tok"

    def test_bare_key_gets_bearer_prefix(self):
        transport = FakeTransport().add("GET", "/users/1", json={"user": {"id": 1}})
        _client(transport, "tok").get_user(1)
        assert transport.requests[0].headers["Authorization"] == "Bearer tok"


class TestReads:
    def test_get_work_order_expands_location_and_unwraps(self):
        transport = FakeTransport().add("GET", "/workorders/5", json={"workOrder": {"id": 5}})
        assert _client(transport).get_work_order(5) == {"id": 5}
        assert transport.requests[0].url == f"{BASE}/workorders/5?expand=location"

    def test_get_work_request_unwraps(self):
        transport = FakeTransport().add("GET", "/workrequests/7", json={"workRequest": {"id": 7}})
        assert _client(transport).get_work_request(7) == {"id": 7}
        assert transport.requests[0].url == f"{BASE}/workrequests/7"

    def test_list_work_requests_filters_by_status(self):
        transport = FakeTransport().add(
            "GET", "/workrequests?", json={"workRequests": [{"id": 1}], "nextCursor": None}
        )
        assert _client(transport).list_work_requests(["APPROVED"], limit=50) == [{"id": 1}]
        assert transport.requests[0].url == f"{BASE}/workrequests?statuses=APPROVED&limit=50"

    def test_list_work_requests_follows_the_cursor(self):
        transport = (
            FakeTransport()
            .add("GET", "cursor=c2", json={"workRequests": [{"id": 2}], "nextCursor": None})
            .add("GET", "/workrequests?", json={"workRequests": [{"id": 1}], "nextCursor": "c2"})
        )
        assert _client(transport).list_work_requests(["APPROVED"]) == [{"id": 1}, {"id": 2}]

    def test_list_work_requests_stops_at_the_page_cap(self):
        transport = FakeTransport().add(
            "GET", "/workrequests?", json={"workRequests": [{"id": 1}], "nextCursor": "again"}
        )
        assert len(_client(transport).list_work_requests(["APPROVED"], max_pages=3)) == 3

    def test_get_subscription_missing_is_none(self):
        transport = FakeTransport().add("GET", "/subscriptions/9", status=404)
        assert _client(transport).get_subscription(9) is None


class TestErrors:
    def test_401_pauses_the_feed(self):
        transport = FakeTransport().add("GET", "/workorders/5", status=401)
        with pytest.raises(CredentialsRejected):
            _client(transport).get_work_order(5)

    def test_absurd_retry_after_is_capped_at_an_hour(self):
        transport = FakeTransport().add(
            "GET", "/workorders/5", status=429, headers={"X-Rate-Limit-Reset": "1793000000"}
        )
        with pytest.raises(TransientError) as caught:
            _client(transport).get_work_order(5)
        assert caught.value.retry_after == 3600

    def test_404_is_not_found(self):
        transport = FakeTransport().add("GET", "/workorders/5", status=404)
        with pytest.raises(NotFound):
            _client(transport).get_work_order(5)

    def test_429_is_transient_with_retry_after(self):
        transport = FakeTransport().add(
            "GET", "/workorders/5", status=429, headers={"Retry-After": "12"}
        )
        with pytest.raises(TransientError) as caught:
            _client(transport).get_work_order(5)
        assert caught.value.retry_after == 12

    def test_5xx_is_transient(self):
        transport = FakeTransport().add("GET", "/workorders/5", status=503)
        with pytest.raises(TransientError):
            _client(transport).get_work_order(5)

    def test_other_4xx_is_permanent(self):
        transport = FakeTransport().add("GET", "/workorders/5", status=400)
        with pytest.raises(PermanentError):
            _client(transport).get_work_order(5)


class TestRateLimit:
    def test_waits_once_the_per_minute_budget_is_spent(self):
        transport = FakeTransport().add("GET", "/users/", json={"user": {}}, times=3)
        clock = FixedClock(NOW)
        client = MaintainXClient(transport, "tok", clock, requests_per_minute=2)
        for user_id in (1, 2, 3):
            client.get_user(user_id)
        assert clock.slept == [60.0]


class TestDownload:
    def test_presigned_download_sends_no_authorization(self):
        url = "https://maintainx-uploads-production.s3.us-west-2.amazonaws.com/x.jpg?X-Amz-Expires=3600"
        transport = FakeTransport().add(
            "GET", "amazonaws.com", body=b"jpeg", headers={"Content-Type": "image/jpeg"}
        )
        data, mime = _client(transport).download(url)
        assert (data, mime) == (b"jpeg", "image/jpeg")
        assert "Authorization" not in transport.requests[0].headers

    def test_expired_presigned_url_is_transient(self):
        # S3 answers 403 once the 60-minute signature lapses; re-fetching the record fixes it.
        transport = FakeTransport().add("GET", "amazonaws.com", status=403)
        with pytest.raises(TransientError):
            _client(transport).download("https://x.s3.amazonaws.com/y.jpg")
