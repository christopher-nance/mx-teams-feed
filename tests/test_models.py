from mx_teams_feed.models import PostRecord, WorkOrderSnapshot

API_WORK_ORDER = {
    "id": 5,
    "sequentialId": 52,
    "title": "Leak",
    "description": "",
    "status": "OPEN",
    "priority": "LOW",
    "dueDate": None,
    "assigneeIds": [],
    "locationId": 1,
    "location": {"id": 1, "name": "Springfield"},
}


def _drop_nulls(value):
    """What Firebase RTDB does on write: null values and empty containers vanish."""
    if isinstance(value, dict):
        cleaned = {k: _drop_nulls(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if v not in (None, [], {})}
    if isinstance(value, list):
        return [_drop_nulls(v) for v in value]
    return value


class TestSnapshotFromApi:
    def test_top_level_cancel_is_normalized_to_done_with_sub_status(self):
        # GET may say CANCELED/SKIPPED; webhooks say DONE + newSubStatus. Use one vocabulary.
        snapshot = WorkOrderSnapshot.from_api({**API_WORK_ORDER, "status": "CANCELED"})
        assert (snapshot.status, snapshot.sub_status) == ("DONE", "CANCELED")

    def test_plain_status_has_no_sub_status(self):
        snapshot = WorkOrderSnapshot.from_api(API_WORK_ORDER)
        assert (snapshot.status, snapshot.sub_status) == ("OPEN", None)


class TestTolerantLoading:
    def test_snapshot_survives_a_store_that_drops_nulls(self):
        snapshot = WorkOrderSnapshot.from_api(API_WORK_ORDER)
        assert WorkOrderSnapshot.from_dict(_drop_nulls(snapshot.to_dict())) == snapshot

    def test_post_record_survives_a_store_that_drops_nulls(self):
        record = PostRecord(
            work_order_id=5,
            request_id=None,
            team_id="t",
            channel_id="c",
            state="active",
            snapshot=WorkOrderSnapshot.from_api(API_WORK_ORDER).to_dict(),
        )
        loaded = PostRecord.from_dict(_drop_nulls(record.to_dict()))
        assert loaded.work_order == record.work_order
        assert loaded.applied_actions == [] and loaded.site_names == []
        assert loaded.request_id is None and loaded.message_id is None

    def test_unknown_keys_are_ignored(self):
        data = {**WorkOrderSnapshot.from_api(API_WORK_ORDER).to_dict(), "added_later": 1}
        assert WorkOrderSnapshot.from_dict(data).id == 5


def test_stored_description_never_keeps_the_requester_email():
    wo = {**API_WORK_ORDER, "description": "Leak\n\nRequester contact information:\nj@x.com"}
    assert WorkOrderSnapshot.from_api(wo).description == "Leak"
