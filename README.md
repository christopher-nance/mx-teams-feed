# mx-teams-feed

When a MaintainX **work request is approved**, post one message in that store's Microsoft Teams
**Maintenance** channel, then keep it current: the root post is **edited** to show the current
status ("as of" a date and time) and the poster **replies** in the thread for status, assignment,
priority, due-date, overdue and deletion changes. Store staff reply under the post, so each
work order's conversation stays together.

A framework-free, standard-library-only Python 3.12+ package, built to be imported by a host web
app (for example Flask with Firebase RTDB or Firestore). The host supplies storage, sign-in and
channel lookup through small ports, so different apps can each wire it their own way.

## What a post looks like

```
Subject:  Vacuum 3 hose split          (title, minus the portal's "<Site> - <timestamp> - " prefix)
Status: In Progress (as of Oct 30, 2026 2:14 PM CDT)        ← rewritten by every status edit
WO #5052 · Requested by Jane Doe
Hose split at the coupler…
[photo] [photo]                         (the request's photos, downscaled, inline)
Open work order in the Team Portal ↗    (portal_work_order_url)
  └ reply: Status: Open → In Progress — by P. Quinn
  └ reply: Assigned to Alex Kim
  └ reply: Done — completed by P. Quinn
```

## How it fits together

```
MaintainX ──webhook──▶ host route ──▶ WebhookReceiver.handle()      verify HMAC (fail closed),
                                          │                         queue, answer 200 fast
                                          ▼
                                     FeedStore (inbox + one PostRecord per work order)
                                          ▲
worker process ──every ~15 s──▶ FeedEngine.run_once()  ──▶ MaintainXClient (GET only)
               ──nightly───────▶ FeedEngine.reconcile()──▶ GraphClient (post / edit / reply,
                                                            as the poster account, ≤ 0.8 writes/s)
```

The host supplies these (all in `mx_teams_feed.ports`):

| Port | What the host provides |
|---|---|
| `FeedStore` | Durable inbox + post records with atomic create and compare-and-set saves. **Prove it with `store_contract.FeedStoreContract`.** |
| `HttpTransport` | `adapters.urllib_transport.UrllibTransport` works anywhere; V4 should wrap `app.core.http`. |
| `GraphTokenProvider` | A delegated token for the poster account (MSAL lives in the host, not here). Raise `ReconnectRequired` when the refresh token is gone. |
| `ChannelResolver` | MaintainX location id → `ChannelTarget(team_id, channel_id or None, channel_name, site_names)`, or `None` when the site isn't enabled. |
| `PeopleDirectory` *(optional)* | Requester email → display name. Without it the email is shown. |
| `ImageProcessor` *(optional)* | `adapters.pillow_images.PillowImageProcessor` (`pip install mx-teams-feed[images]`). Without it, photos over 3.5 MB are skipped. |
| `Clock` | `now()` (aware) and `sleep()`. |

## Wiring (sketch)

```python
from mx_teams_feed import FeedConfig, FeedEngine, GraphClient, MaintainXClient, WebhookReceiver
from mx_teams_feed.adapters.pillow_images import PillowImageProcessor
from mx_teams_feed.adapters.urllib_transport import UrllibTransport

transport = UrllibTransport()
store = RtdbFeedStore(...)                     # host adapter, passes FeedStoreContract

# Web: POST /api/maintenance-feed/webhooks/maintainx (no login, no CSRF)
receiver = WebhookReceiver(store=store, secrets=load_subscription_secrets,
                           clock=SystemClock(), org_id=900001)
result = receiver.handle(request.get_data(), request.headers, request.url)
return "", result.status

# Worker (separate process, production only). It runs outside the web framework, so the
# host's adapters must not depend on a request or application context.
engine = FeedEngine(
    store=store,
    maintainx=MaintainXClient(transport, os.environ["MAINTAINX_KEY"], clock),
    graph=GraphClient(transport, PosterTokenProvider(...), clock),
    channels=PortalChannelResolver(...),       # location id -> store Team + channel
    clock=clock,
    config=FeedConfig(portal_work_order_url="https://<portal>/utrack/work-orders?wo={id}"),
    people=EmployeeDirectory(...),
    images=PillowImageProcessor(),
)
while True:
    report = engine.run_once()
    if report.paused: alert_admins("Reconnect the Teams poster account")
    clock.sleep(15)
```

**Run exactly one worker.** Start it only where an env flag says so (production slot only).
A second copy during a slot swap is tolerated (a root post in flight is waited out rather than
duplicated) but not designed for.

## Microsoft Graph permissions (delegated, for the poster account)

`offline_access`, `ChannelMessage.Send` (post, reply), `ChannelMessage.ReadWrite` (edit the root;
admin consent), `ChannelMessage.Read.All` (find a root post whose id was lost; admin consent),
`Channel.ReadBasic.All` (find the channel by name). The poster must be an **owner** of each store
Team so it can post in a moderated channel.

## MaintainX subscriptions

Create one subscription per type, all pointing at the host route:

| Type | Filter |
|---|---|
| `WORK_REQUEST_STATUS_CHANGE` | `oneOfStatuses: ["APPROVED"]` |
| `WORK_ORDER_STATUS_CHANGE` | all statuses |
| `WORK_ORDER_CHANGE` | `oneOfProperties: ["ASSIGNEES","PRIORITY","DUE_DATE","TITLE","DESCRIPTION","LOCATION"]` |
| `WORK_ORDER_OVERDUE` | — |
| `WORK_ORDER_DELETE` | — |

Store each returned `secret` (the receiver tries every configured secret) and each `id` (pass
them to `reconcile(subscription_ids=…)` so a silently deleted subscription raises an issue).

## Guarantees and limits

- **One root post per work order.** The post record is claimed atomically before posting. When
  a post's outcome is unknown (timeout, 5xx, crash), the retry looks for it by this work order's
  own portal link before posting again; a post Graph refused outright is simply retried.
- **Replies are announced once per change.** Replies come from diffing the stored snapshot
  against a fresh GET, and each sent reply is recorded by the change it announces
  (`status:OPEN/-->DONE/-`), so duplicate, late or overlapping deliveries are silent. The one
  exception is a reply whose response was lost after Graph accepted it: delivery is at least
  once, so it can appear twice.
- **Replies come before the root edit.** If Graph refuses an edit, the replies still go out and
  the stale root is corrected by the nightly refresh.
- **One bad photo never blocks a post.** Missing, corrupt or oversized photos are counted as
  "more photos in the portal".
- **Failures retry with backoff** (1 min → 5 min → 30 min → 1 h; the server's `Retry-After` wins
  when longer, capped at an hour), then dead-letter. Exceptions are never swallowed, and an
  event that keeps crashing the worker is dead-lettered too.
- **A lost poster sign-in or a rejected MaintainX key pauses the feed** (`RunReport.paused`) and
  nothing is lost: events wait, and the worker re-checks every 5 minutes until it's fixed.
- **Deletes are double-checked** against MaintainX before a post is marked deleted, and a work
  order that vanished without its delete webhook is marked deleted when the feed next sees it.
- **Completion is announced by the status event.** MaintainX's GET can't tell "Done" from
  "Canceled", so a completion noticed by another event waits for the status event (seconds
  behind) instead of risking "Done" for a cancellation; the nightly refresh catches up if the
  status event never comes.
- **A post that can't happen is retired, not retried forever.** If a work order closes, is
  deleted or loses its channel before its first post goes out, the claim is abandoned.
- **Nightly `reconcile()`** keeps the refresh token warm, checks the subscriptions, reports
  work orders stuck unposted, backfills open approved requests from the last 7 days that have no
  post (lost webhooks, newly enabled sites — never older history) and re-checks open posts for
  missed changes.
- **No personal data stored or logged beyond what the post shows.** Inbox rows keep only ids and
  statuses — no payload bodies, emails or file URLs. Logs carry ids only.
- **Webhooks must carry the body signature.** The URI-only signature is refused (it doesn't
  cover the body).
- Ordering isn't guaranteed by MaintainX. Each reply describes the change between the last
  state the feed saw and the current one, so two transitions seconds apart can collapse into one.

## Development

```
pip install -e .[dev]
pytest
ruff check . && ruff format --check .
```
