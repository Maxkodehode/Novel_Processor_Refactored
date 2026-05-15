from .royalroad import RoyalRoadAdapter
from .scribblehub import ScribbleHubAdapter
from .fanfiction import FanFictionAdapter

ADAPTERS = [
    RoyalRoadAdapter,
    ScribbleHubAdapter,
    FanFictionAdapter,
]


def get_adapter(url: str):
    for cls in ADAPTERS:
        if cls.matches(url):
            return cls()
    raise ValueError(f"No adapter found for URL: {url}")
