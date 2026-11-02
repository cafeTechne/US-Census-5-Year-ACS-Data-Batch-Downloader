"""Census Bureau API client with retry and backoff handling."""

import os
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

load_dotenv()

BASE_URL = "https://api.census.gov/data"
DEFAULT_API_KEY = os.getenv("CENSUS_API_KEY", "")


class CensusAPIClient:
    """Client for interacting with the US Census Bureau ACS APIs."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = BASE_URL,
        timeout: int = 30,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
    ) -> None:
        self.api_key = api_key if api_key is not None else DEFAULT_API_KEY
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        self.session = requests.Session()
        retry_strategy = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def fetch(
        self,
        year: int,
        state_code: str,
        table_code: str,
        geo_level: str = "place",
        place_code: Optional[str] = None,
        county_code: Optional[str] = None,
    ) -> Optional[List[List[str]]]:
        """Fetch ACS 5-year data for a specific year, geography, and table.

        Supports geography levels: 'place', 'state', 'county'.
        """
        endpoint = f"{self.base_url}/{year}/acs/acs5"
        params: Dict[str, str] = {
            "get": f"NAME,group({table_code})",
            "key": self.api_key,
        }

        if geo_level == "place":
            if not place_code:
                raise ValueError("place_code is required for place-level query.")
            params["for"] = f"place:{place_code}"
            params["in"] = f"state:{state_code}"
        elif geo_level == "state":
            params["for"] = f"state:{state_code}"
        elif geo_level == "county":
            params["for"] = f"county:{county_code or '*'}"
            params["in"] = f"state:{state_code}"
        else:
            raise ValueError(f"Unsupported geography level: {geo_level}")

        try:
            response = self.session.get(endpoint, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            return None


# Module-level default client
_default_client = CensusAPIClient()


def fetch_census_data(
    year: int,
    state_code: str,
    table_code: str,
    geo_level: str = "place",
    place_code: Optional[str] = None,
    county_code: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: int = 30,
) -> Optional[List[List[str]]]:
    """Convenience function delegating to CensusAPIClient."""
    if api_key is not None or timeout != 30:
        client = CensusAPIClient(api_key=api_key, timeout=timeout)
        return client.fetch(
            year=year,
            state_code=state_code,
            table_code=table_code,
            geo_level=geo_level,
            place_code=place_code,
            county_code=county_code,
        )
    return _default_client.fetch(
        year=year,
        state_code=state_code,
        table_code=table_code,
        geo_level=geo_level,
        place_code=place_code,
        county_code=county_code,
    )

