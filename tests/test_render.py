from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from mx_teams_feed import render
from mx_teams_feed.render import RootView

CHICAGO = ZoneInfo("America/Chicago")


class TestStripTitlePrefix:
    def test_strips_machine_prefix_for_this_site(self):
        title = "Springfield - 2026-10-29 @ 2:35 PM CDT - Vacuum 3 hose split"
        assert render.strip_title_prefix(title, ["Springfield"]) == "Vacuum 3 hose split"

    def test_keeps_dashes_inside_the_tail(self):
        title = "Shelbyville - 2026-08-09 @ 8:15 AM CDT - Vacuum 3 - hose split"
        assert render.strip_title_prefix(title, ["Shelbyville"]) == "Vacuum 3 - hose split"

    def test_site_names_that_contain_a_dash_separator(self):
        title = "ST43 - Ogdenville - 2026-10-01 @ 9:05 AM CDT - Dryer down"
        assert render.strip_title_prefix(title, ["ST43 - Ogdenville"]) == "Dryer down"

    def test_site_match_is_case_insensitive(self):
        title = "SPRINGFIELD - 2026-10-29 @ 2:35 PM CDT - Leak"
        assert render.strip_title_prefix(title, ["Springfield"]) == "Leak"

    def test_any_candidate_site_name_may_match(self):
        title = "Ogdenville - 2026-10-29 @ 2:35 PM CDT - Leak"
        assert render.strip_title_prefix(title, ["Ogdenville - East", "Ogdenville"]) == "Leak"

    def test_other_sites_prefix_is_left_alone(self):
        title = "Shelbyville - 2026-08-09 @ 8:15 AM CDT - Leak"
        assert render.strip_title_prefix(title, ["Springfield"]) == title

    def test_hand_typed_title_without_timestamp_is_left_alone(self):
        title = "Springfield - Bay 2 - leak under the arch"
        assert render.strip_title_prefix(title, ["Springfield"]) == title

    def test_empty_tail_keeps_the_raw_title(self):
        title = "Springfield - 2026-10-29 @ 2:35 PM CDT - "
        assert render.strip_title_prefix(title, ["Springfield"]) == title.strip()

    def test_no_site_names_means_no_stripping(self):
        title = "Springfield - 2026-10-29 @ 2:35 PM CDT - Leak"
        assert render.strip_title_prefix(title, []) == title


class TestHeadline:
    def test_blank_title_falls_back_to_the_work_order_number(self):
        assert render.headline("   ", 5052, ["Springfield"]) == "Work order #5052"

    def test_uses_stripped_title(self):
        title = "Springfield - 2026-10-29 @ 2:35 PM CDT - Leak"
        assert render.headline(title, 5052, ["Springfield"]) == "Leak"


class TestScrubDescription:
    def test_removes_requester_contact_trailer(self):
        text = "Leak at bay 2\n\nRequester contact information:\njane.doe@example.com"
        assert render.scrub_description(text) == "Leak at bay 2"

    def test_trailer_match_ignores_case(self):
        text = "Leak\nREQUESTER CONTACT INFORMATION\njane.doe@example.com"
        assert render.scrub_description(text) == "Leak"

    def test_removes_a_trailing_bare_email_line(self):
        assert render.scrub_description("Leak at bay 2\njane.doe@example.com") == "Leak at bay 2"

    def test_keeps_an_at_sign_used_in_a_sentence(self):
        text = "Leak at bay 2\ncall the vendor @ 555-1200"
        assert render.scrub_description(text) == text

    def test_none_becomes_empty(self):
        assert render.scrub_description(None) == ""


class TestStatusLabel:
    def test_plain_statuses(self):
        assert render.status_label("OPEN") == "Open"
        assert render.status_label("IN_PROGRESS") == "In Progress"
        assert render.status_label("ON_HOLD") == "On Hold"
        assert render.status_label("DONE") == "Done"

    def test_done_with_canceled_sub_status_reads_canceled(self):
        assert render.status_label("DONE", "CANCELED") == "Canceled"

    def test_top_level_canceled_and_skipped(self):
        assert render.status_label("CANCELED") == "Canceled"
        assert render.status_label("SKIPPED") == "Skipped"

    def test_unknown_status_is_title_cased(self):
        assert render.status_label("WAITING_ON_PARTS") == "Waiting On Parts"


class TestFormatting:
    def test_local_datetime(self):
        when = datetime(2026, 10, 30, 19, 14, tzinfo=UTC)
        assert render.format_local(when, CHICAGO) == "Oct 30, 2026 2:14 PM CDT"

    def test_local_datetime_after_midnight_and_in_winter(self):
        when = datetime(2026, 12, 1, 6, 5, tzinfo=UTC)
        assert render.format_local(when, CHICAGO) == "Dec 1, 2026 12:05 AM CST"

    def test_due_date_in_local_time(self):
        # 03:00Z on Nov 3 is still Nov 2 in Chicago.
        assert render.format_date("2026-11-03T03:00:00.000Z", CHICAGO) == "Nov 2, 2026"


def _view(**overrides):
    base = dict(
        status_label="In Progress",
        as_of_text="Oct 30, 2026 2:14 PM CDT",
        sequential_id=5052,
        requester="Jane Doe",
        description="Hose split <again>\nsecond line",
        images_html="",
        link_url="https://portal.example.com/utrack/work-orders?wo=700701",
        extra_photos=0,
    )
    base.update(overrides)
    return RootView(**base)


class TestRootHtml:
    def test_contains_status_with_as_of(self):
        html = render.root_html(_view())
        assert "<b>Status:</b> In Progress" in html
        assert "as of Oct 30, 2026 2:14 PM CDT" in html

    def test_contains_work_order_number_and_requester(self):
        html = render.root_html(_view())
        assert "WO #5052" in html
        assert "Requested by Jane Doe" in html

    def test_escapes_description_and_keeps_line_breaks(self):
        html = render.root_html(_view())
        assert "Hose split &lt;again&gt;<br>second line" in html

    def test_links_to_the_portal(self):
        html = render.root_html(_view())
        assert '<a href="https://portal.example.com/utrack/work-orders?wo=700701">' in html

    def test_images_html_is_inserted_verbatim(self):
        img = '<img src="../hostedContents/1/$value" alt="Photo 1">'
        assert img in render.root_html(_view(images_html=img))

    def test_empty_description_has_no_paragraph(self):
        html = render.root_html(_view(description=""))
        assert "<p></p>" not in html

    def test_extra_photos_note(self):
        html = render.root_html(_view(extra_photos=2))
        assert "2 more photos in the portal" in html

    def test_unknown_requester_is_omitted(self):
        html = render.root_html(_view(requester=None))
        assert "Requested by" not in html
