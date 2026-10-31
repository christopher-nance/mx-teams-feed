import base64
from datetime import UTC, datetime

import pytest

from mx_teams_feed.errors import (
    CredentialsRejected,
    PermanentError,
    ReconnectRequired,
    TransientError,
)
from mx_teams_feed.graph import GraphClient, HostedImage, image_tags
from mx_teams_feed.ports import HttpResponse
from mx_teams_feed.testing import FakeTokenProvider, FakeTransport, FixedClock

NOW = datetime(2026, 10, 30, 19, 0, tzinfo=UTC)
MSGS = "/teams/T/channels/C/messages"
POSTED = {
    "id": "1730315000000",
    "webUrl": "https://teams.microsoft.com/l/message/C/1730315000000",
    "body": {
        "contentType": "html",
        "content": '<p>x</p><img src="https://graph.microsoft.com/v1.0/teams/T/channels/C/'
        'messages/1730315000000/hostedContents/aWQ9/$value" alt="Photo 1">',
    },
}


def _client(transport, tokens=None, clock=None):
    return GraphClient(transport, tokens or FakeTokenProvider(), clock or FixedClock(NOW))


class TestPostMessage:
    def test_posts_subject_html_and_hosted_images(self):
        transport = FakeTransport().add("POST", MSGS, status=201, json=POSTED)
        image = HostedImage("1", b"jpegbytes", "image/jpeg")
        posted = _client(transport).post_message("T", "C", "Vacuum 3", "<p>x</p>", [image])
        sent = transport.requests[0]
        assert sent.url == f"https://graph.microsoft.com/v1.0{MSGS}"
        assert sent.json["subject"] == "Vacuum 3"
        assert sent.json["body"] == {"contentType": "html", "content": "<p>x</p>"}
        assert sent.json["hostedContents"] == [
            {
                "@microsoft.graph.temporaryId": "1",
                "contentBytes": base64.b64encode(b"jpegbytes").decode(),
                "contentType": "image/jpeg",
            }
        ]
        assert sent.headers["Authorization"] == "Bearer token-1"
        assert posted.id == "1730315000000"
        assert posted.web_url == POSTED["webUrl"]
        assert posted.body_html == POSTED["body"]["content"]

    def test_no_images_sends_no_hosted_contents(self):
        transport = FakeTransport().add("POST", MSGS, status=201, json=POSTED)
        _client(transport).post_message("T", "C", "s", "<p>x</p>", [])
        assert "hostedContents" not in transport.requests[0].json


class TestUpdateAndReply:
    def test_update_patches_subject_and_body(self):
        transport = FakeTransport().add("PATCH", f"{MSGS}/m1", status=204)
        _client(transport).update_message("T", "C", "m1", "New subject", "<p>y</p>")
        sent = transport.requests[0]
        assert sent.json == {
            "subject": "New subject",
            "body": {"contentType": "html", "content": "<p>y</p>"},
        }

    def test_reply_posts_to_the_thread(self):
        transport = FakeTransport().add("POST", f"{MSGS}/m1/replies", status=201, json={"id": "r1"})
        assert _client(transport).post_reply("T", "C", "m1", "<p>Done</p>") == "r1"
        assert transport.requests[0].json == {
            "body": {"contentType": "html", "content": "<p>Done</p>"}
        }


class TestErrors:
    def test_401_refreshes_the_token_once_and_retries(self):
        transport = (
            FakeTransport()
            .add("POST", "/replies", status=401, times=1)
            .add("POST", "/replies", status=201, json={"id": "r1"})
        )
        tokens = FakeTokenProvider()
        assert _client(transport, tokens).post_reply("T", "C", "m1", "<p>x</p>") == "r1"
        assert tokens.calls == [False, True]
        assert transport.requests[1].headers["Authorization"] == "Bearer token-2"

    def test_401_after_refresh_pauses_the_feed(self):
        transport = FakeTransport().add("POST", "/replies", status=401)
        with pytest.raises(CredentialsRejected):
            _client(transport).post_reply("T", "C", "m1", "<p>x</p>")

    def test_reconnect_required_propagates(self):
        transport = FakeTransport()
        tokens = FakeTokenProvider(reconnect=True)
        with pytest.raises(ReconnectRequired):
            _client(transport, tokens).post_reply("T", "C", "m1", "<p>x</p>")
        assert transport.requests == []

    def test_429_is_transient_with_retry_after(self):
        transport = FakeTransport().add(
            "POST", "/replies", status=429, headers={"Retry-After": "7"}
        )
        with pytest.raises(TransientError) as caught:
            _client(transport).post_reply("T", "C", "m1", "<p>x</p>")
        assert caught.value.retry_after == 7

    def test_403_is_permanent(self):
        transport = FakeTransport().add("POST", MSGS, status=403)
        with pytest.raises(PermanentError):
            _client(transport).post_message("T", "C", "s", "<p>x</p>", [])


class TestPacing:
    def test_writes_are_spaced_to_stay_under_the_per_user_limit(self):
        transport = FakeTransport().add("POST", "/replies", status=201, json={"id": "r"}, times=2)
        clock = FixedClock(NOW)
        client = _client(transport, clock=clock)
        client.post_reply("T", "C", "m1", "<p>a</p>")
        client.post_reply("T", "C", "m1", "<p>b</p>")
        assert clock.slept == [1.25]

    def test_reads_are_not_paced(self):
        transport = FakeTransport().add("GET", "/channels", json={"value": []}, times=2)
        clock = FixedClock(NOW)
        client = _client(transport, clock=clock)
        client.find_channel_id("T", "Maintenance")
        client.find_channel_id("T", "Maintenance")
        assert clock.slept == []


class TestLookups:
    def test_find_channel_id_matches_name_case_insensitively(self):
        channels = {
            "value": [
                {"id": "19:a", "displayName": "General"},
                {"id": "19:b", "displayName": "maintenance"},
            ]
        }
        transport = FakeTransport().add("GET", "/teams/T/channels", json=channels)
        assert _client(transport).find_channel_id("T", "Maintenance") == "19:b"

    def test_find_channel_id_missing_is_none(self):
        transport = FakeTransport().add("GET", "/teams/T/channels", json={"value": []})
        assert _client(transport).find_channel_id("T", "Maintenance") is None

    def test_find_root_message_by_marker(self):
        page = {
            "value": [
                {"id": "a", "body": {"content": "<p>WO #5051</p>"}},
                {"id": "x", "body": {"content": "<p>WO #50521</p>"}},
                {
                    "id": "b",
                    "deletedDateTime": "2026-10-30T00:00:00Z",
                    "body": {"content": "<p>WO #5052</p>"},
                },
                {"id": "c", "webUrl": "u", "body": {"content": "<p>WO #5052</p>"}},
            ]
        }
        transport = FakeTransport().add("GET", f"{MSGS}?", json=page)
        found = _client(transport).find_root_message("T", "C", "WO #5052")
        assert found.id == "c"

    def test_find_root_message_ignores_messages_older_than_the_attempt(self):
        page = {
            "value": [
                {
                    "id": "old",
                    "createdDateTime": "2026-10-30T18:00:00Z",
                    "body": {"content": "<a href='x?wo=5'>"},
                },
                {
                    "id": "new",
                    "createdDateTime": "2026-10-30T19:01:00Z",
                    "body": {"content": "<a href='x?wo=5'>"},
                },
            ]
        }
        transport = FakeTransport().add("GET", f"{MSGS}?", json=page)
        after = datetime(2026, 10, 30, 19, 0, tzinfo=UTC)
        found = _client(transport).find_root_message("T", "C", "x?wo=5", created_after=after)
        assert found.id == "new"

    def test_find_root_message_absent_is_none(self):
        transport = FakeTransport().add("GET", f"{MSGS}?", json={"value": []})
        assert _client(transport).find_root_message("T", "C", "WO #1 ") is None


class TestRefreshToken:
    def test_forces_a_refresh(self):
        tokens = FakeTokenProvider()
        _client(FakeTransport(), tokens).refresh_token()
        assert tokens.calls == [True]

    def test_reconnect_required_propagates(self):
        with pytest.raises(ReconnectRequired):
            _client(FakeTransport(), FakeTokenProvider(reconnect=True)).refresh_token()


class TestImageTags:
    def test_extracts_img_tags_from_a_posted_body(self):
        assert image_tags(POSTED["body"]["content"]) == [
            '<img src="https://graph.microsoft.com/v1.0/teams/T/channels/C/messages/'
            '1730315000000/hostedContents/aWQ9/$value" alt="Photo 1">'
        ]

    def test_no_images(self):
        assert image_tags("<p>none</p>") == []


def test_http_response_header_lookup_is_case_insensitive():
    assert HttpResponse(200, {"retry-after": "3"}).header("Retry-After") == "3"
