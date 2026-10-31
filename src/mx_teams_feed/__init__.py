"""MaintainX work-request approvals -> per-site Microsoft Teams channel posts.

Typical host wiring (see README.md):

    receiver = WebhookReceiver(store=..., secrets=..., clock=..., org_id=...)   # web route
    engine = FeedEngine(store=..., maintainx=MaintainXClient(...),
                        graph=GraphClient(...), channels=..., clock=..., config=FeedConfig(...))
    engine.run_once()     # worker, every ~15 s
    engine.reconcile()    # worker, nightly
"""

from .engine import FeedConfig, FeedEngine, ReconcileReport, RunReport
from .errors import (
    ConflictError,
    FeedError,
    InvalidPayload,
    NotFound,
    PermanentError,
    ReconnectRequired,
    TransientError,
)
from .graph import GraphClient
from .maintainx import MaintainXClient
from .models import ChannelTarget, PostRecord, QueuedEvent, WorkOrderSnapshot
from .receiver import ReceiveResult, WebhookReceiver

__version__ = "0.1.0.dev0"

__all__ = [
    "ChannelTarget",
    "ConflictError",
    "FeedConfig",
    "FeedEngine",
    "FeedError",
    "GraphClient",
    "InvalidPayload",
    "MaintainXClient",
    "NotFound",
    "PermanentError",
    "PostRecord",
    "QueuedEvent",
    "ReceiveResult",
    "ReconcileReport",
    "ReconnectRequired",
    "RunReport",
    "TransientError",
    "WebhookReceiver",
    "WorkOrderSnapshot",
]
