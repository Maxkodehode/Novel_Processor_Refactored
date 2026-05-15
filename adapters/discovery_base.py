from abc import ABC, abstractmethod
from typing import List, Dict


class BaseDiscoveryAdapter(ABC):
    SITE_NAME: str = ""

    @abstractmethod
    def get_list_url(self, page: int) -> str:
        """Build the paginated list URL."""

    @abstractmethod
    def parse_list_page(self, soup) -> List[Dict[str, str]]:
        """
        Parse the list page HTML.
        Returns a list of dicts: {'title': str, 'url': str}
        """
