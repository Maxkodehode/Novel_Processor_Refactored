from typing import List, Dict
from bs4 import BeautifulSoup
from .discovery_base import BaseDiscoveryAdapter


class RoyalRoadDiscoveryAdapter(BaseDiscoveryAdapter):
    SITE_NAME = "royalroad"
    BASE_URL = "https://www.royalroad.com"

    def get_list_url(self, page: int) -> str:
        # Best Rated: https://www.royalroad.com/fictions/best-rated?page=N
        return f"{self.BASE_URL}/fictions/best-rated?page={page}"

    def parse_list_page(self, soup: BeautifulSoup) -> List[Dict[str, str]]:
        results = []
        # Target: .fiction-list-item -> title link href
        items = soup.select(".fiction-list-item")
        for item in items:
            title_link = item.select_one(".fiction-title a")
            if title_link and title_link.get("href"):
                url = title_link["href"]
                if not url.startswith("http"):
                    url = f"{self.BASE_URL}{url}"
                results.append({"title": title_link.get_text(strip=True), "url": url})
        return results


class ScribbleHubDiscoveryAdapter(BaseDiscoveryAdapter):
    SITE_NAME = "scribblehub"
    BASE_URL = "https://www.scribblehub.com"

    def get_list_url(self, page: int) -> str:
        # Series Ranking: https://www.scribblehub.com/series-ranking/?order=weekly&pg=N
        return f"{self.BASE_URL}/series-ranking/?order=weekly&pg={page}"

    def parse_list_page(self, soup: BeautifulSoup) -> List[Dict[str, str]]:
        results = []
        # Target: div.search_main_box -> title anchor
        items = soup.select(".search_main_box")
        for item in items:
            title_link = item.select_one(".search_title a")
            if title_link and title_link.get("href"):
                url = title_link["href"]
                results.append({"title": title_link.get_text(strip=True), "url": url})
        return results
