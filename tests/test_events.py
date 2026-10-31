import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from mx_teams_feed import events, signature
from mx_teams_feed.errors import InvalidPayload
from mx_teams_feed.receiver import WebhookReceiver
from mx_teams_feed.testing import FixedClock, MemoryStore

NOW = datetime(2026, 10, 30, 19, 0, tzinfo=UTC)
URL = "https://portal.example.com/api/maintenance-feed/webhooks/maintainx"


def _sign(secret: str, message: str, when: datetime = NOW) -> str:
    t = int(when.timestamp())
    digest = hmac.new(secret.encode(), f"{t}.{message}".encode(), hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"


def _body(**fields) -> bytes:
    payload = {
        "eventType": "WORK_REQUEST_STATUS_CHANGE",
        "requestId": 800801,
        "orgId": 900001,
        "oldStatus": "PENDING",
        "newStatus": "APPROVED",
        "occurredAt": "2026-10-29T19:35:44.051Z",
    }
    payload.update(fields)
    return json.dumps(payload).encode()


class TestVerify:
    def test_valid_body_signature(self):
        body = _body()
        headers = {"x-maintainx-webhook-body-signature": _sign("s1", body.decode())}
        assert signature.verify(body, headers, URL, ["s1"], NOW)

    def test_header_names_are_case_insensitive(self):
        body = _body()
        headers = {"X-MaintainX-Webhook-Body-Signature": _sign("s1", body.decode())}
        assert signature.verify(body, headers, URL, ["s1"], NOW)

    def test_wrong_secret_is_rejected(self):
        body = _body()
        headers = {"x-maintainx-webhook-body-signature": _sign("other", body.decode())}
        assert not signature.verify(body, headers, URL, ["s1"], NOW)

    def test_any_configured_secret_may_match(self):
        body = _body()
        headers = {"x-maintainx-webhook-body-signature": _sign("s2", body.decode())}
        assert signature.verify(body, headers, URL, ["s1", "s2"], NOW)

    def test_tampered_body_is_rejected(self):
        headers = {"x-maintainx-webhook-body-signature": _sign("s1", _body().decode())}
        assert not signature.verify(_body(newStatus="REJECTED"), headers, URL, ["s1"], NOW)

    def test_stale_timestamp_is_rejected(self):
        body = _body()
        old = NOW - timedelta(minutes=6)
        headers = {"x-maintainx-webhook-body-signature": _sign("s1", body.decode(), old)}
        assert not signature.verify(body, headers, URL, ["s1"], NOW)

    def test_unparseable_header_is_rejected(self):
        headers = {"x-maintainx-webhook-body-signature": "t=soon,v1=abc"}
        assert not signature.verify(_body(), headers, URL, ["s1"], NOW)

    def test_no_secrets_configured_fails_closed(self):
        body = _body()
        headers = {"x-maintainx-webhook-body-signature": _sign("s1", body.decode())}
        assert not signature.verify(body, headers, URL, [], NOW)

    def test_missing_headers_are_rejected(self):
        assert not signature.verify(_body(), {}, URL, ["s1"], NOW)

    def test_uri_signature_alone_is_rejected(self):
        # The URI signature doesn't cover the body: one leaked header would let anyone
        # attach it to forged events for five minutes.
        headers = {"x-maintainx-webhook-uri-signature": _sign("s1", URL)}
        assert not signature.verify(_body(), headers, URL, ["s1"], NOW)

    def test_non_hex_signature_is_rejected_not_an_error(self):
        t = int(NOW.timestamp())
        headers = {"x-maintainx-webhook-body-signature": f"t={t},v1=é"}
        assert not signature.verify(_body(), headers, URL, ["s1"], NOW)


class TestParseEvent:
    def test_request_status_change(self):
        event = events.parse_event(_body())
        assert event.event_type == "WORK_REQUEST_STATUS_CHANGE"
        assert event.request_id == 800801
        assert event.new_status == "APPROVED"
        assert event.org_id == 900001

    def test_key_is_sha256_of_the_raw_body(self):
        body = _body()
        assert events.parse_event(body).key == hashlib.sha256(body).hexdigest()

    def test_new_work_request_uses_work_request_id(self):
        body = json.dumps(
            {"eventType": "NEW_WORK_REQUEST", "workRequestId": 9, "orgId": 1}
        ).encode()
        assert events.parse_event(body).request_id == 9

    def test_work_order_status_change_fields(self):
        body = json.dumps(
            {
                "eventType": "WORK_ORDER_STATUS_CHANGE",
                "workOrderId": "700701",
                "orgId": 900001,
                "oldStatus": "OPEN",
                "newStatus": "DONE",
                "newSubStatus": "CANCELED",
                "userId": 500501,
                "occurredAt": "2026-10-30T14:00:00Z",
            }
        ).encode()
        event = events.parse_event(body)
        assert event.work_order_id == 700701
        assert event.user_id == 500501
        assert event.new_sub_status == "CANCELED"

    def test_only_scalar_ids_are_kept(self):
        # Nested work-order copies carry the requester's email and free-text custom-field
        # labels (unsafe as RTDB keys); nothing reads them, so nothing stores them.
        body = json.dumps(
            {
                "eventType": "WORK_ORDER_CHANGE",
                "workOrderId": 5,
                "url": "https://s3/x",
                "newWorkOrder": {"description": "Requester contact information: j@x.com"},
            }
        ).encode()
        stored = events.parse_event(body).to_dict()
        assert "newWorkOrder" not in json.dumps(stored)
        assert "s3" not in json.dumps(stored)
        assert all(not isinstance(v, (dict, list)) for v in stored.values())

    def test_from_dict_tolerates_dropped_null_fields(self):
        # Firebase RTDB silently drops null values on write.
        event = events.MxEvent.from_dict({"key": "k", "event_type": "WORK_ORDER_CHANGE"})
        assert event.work_order_id is None and event.user_id is None

    def test_round_trips_through_dict(self):
        event = events.parse_event(_body())
        assert events.MxEvent.from_dict(event.to_dict()) == event

    def test_invalid_json_raises(self):
        with pytest.raises(InvalidPayload):
            events.parse_event(b"not json")

    def test_missing_event_type_raises(self):
        with pytest.raises(InvalidPayload):
            events.parse_event(b'{"orgId": 1}')


class TestReceiver:
    def _receiver(self, store, secrets=("s1",)):
        return WebhookReceiver(
            store=store, secrets=lambda: list(secrets), clock=FixedClock(NOW), org_id=900001
        )

    def _headers(self, body: bytes, secret="s1"):
        return {"x-maintainx-webhook-body-signature": _sign(secret, body.decode())}

    def test_valid_event_is_queued(self):
        store = MemoryStore()
        body = _body()
        result = self._receiver(store).handle(body, self._headers(body), URL)
        assert (result.status, result.outcome) == (200, "queued")
        assert len(store.events) == 1

    def test_duplicate_delivery_is_acknowledged_once(self):
        store = MemoryStore()
        body = _body()
        receiver = self._receiver(store)
        receiver.handle(body, self._headers(body), URL)
        result = receiver.handle(body, self._headers(body), URL)
        assert (result.status, result.outcome) == (200, "duplicate")
        assert len(store.events) == 1

    def test_bad_signature_is_401_and_not_queued(self):
        store = MemoryStore()
        body = _body()
        result = self._receiver(store).handle(body, self._headers(body, "wrong"), URL)
        assert result.status == 401
        assert store.events == {}

    def test_unconfigured_secrets_fail_closed(self):
        store = MemoryStore()
        body = _body()
        result = self._receiver(store, secrets=()).handle(body, self._headers(body), URL)
        assert result.status == 401

    def test_other_org_is_ignored(self):
        store = MemoryStore()
        body = _body(orgId=1)
        result = self._receiver(store).handle(body, self._headers(body), URL)
        assert (result.status, result.outcome) == (200, "ignored:org")
        assert store.events == {}

    def test_unsupported_event_type_is_ignored(self):
        store = MemoryStore()
        body = _body(eventType="NEW_WORK_REQUEST")
        result = self._receiver(store).handle(body, self._headers(body), URL)
        assert (result.status, result.outcome) == (200, "ignored:type")
        assert store.events == {}

    def test_signed_garbage_is_400(self):
        store = MemoryStore()
        body = b"not json"
        result = self._receiver(store).handle(body, self._headers(body), URL)
        assert result.status == 400


def test_absurd_timestamp_is_rejected_not_an_error():
    headers = {"x-maintainx-webhook-body-signature": "t=" + "9" * 400 + ",v1=" + "a" * 64}
    assert not signature.verify(_body(), headers, URL, ["s1"], NOW)
