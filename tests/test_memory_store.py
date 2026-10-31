from mx_teams_feed.store_contract import FeedStoreContract
from mx_teams_feed.testing import MemoryStore


class TestMemoryStore(FeedStoreContract):
    def make_store(self):
        return MemoryStore()
