"""Census Bureau API client with retry, backoff, and optional local caching."""

import os
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from census.cache import QueryCache

load_dotenv()

BASE_URL = "https://api.census.gov/data"
DEFAULT_API_KEY = os.getenv("CENSUS_API_KEY", "")

# ACS 5-year dataset paths
DATASETS: Dict[str, str] = {
    "acs5": "acs/acs5",  # Detailed tables (B/C)
    "profile": "acs/acs5/profile",  # Data Profiles (DP02-DP05)
    "subject": "acs/acs5/subject",  # Subject tables (S....)
    "cprofile": "acs/acs5/cprofile",  # Comparison Profiles (CP02-CP05)
}

# Geography levels -> (Census API "for" predicate name, summary level)
GEO_LEVELS: Dict[str, Tuple[str, str]] = {
    "us": ("us", "010"),
    "state": ("state", "040"),
    "county": ("county", "050"),
    "tract": ("tract", "140"),
    "place": ("place", "160"),
    "msa": ("metropolitan statistical area/micropolitan statistical area", "310"),
    "zcta": ("zip code tabulation area", "860"),
}

# ZCTAs were nested within states (summary level 871) through the 2019 ACS
# 5-year release; from 2020 they are only available nationally (level 860).
ZCTA_STATE_NESTING_LAST_YEAR = 2019


def infer_dataset(table_code: str) -> str:
    """Pick the ACS 5-year endpoint from a table code prefix."""
    code = table_code.upper()
    if code.startswith("DP"):
        return "profile"
    if code.startswith("CP"):
        return "cprofile"
    if code.startswith("S"):
        return "subject"
    return "acs5"


def build_geo_params(
    geo_level: str,
    state_code: Optional[str] = None,
    place_code: Optional[str] = None,
    county_code: Optional[str] = None,
    tract_code: Optional[str] = None,
    zcta_code: Optional[str] = None,
    msa_code: Optional[str] = None,
    year: Optional[int] = None,
) -> Dict[str, str]:
    """Build the Census API ``for`` / ``in`` predicates for a geography level."""
    params: Dict[str, str] = {}
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
    elif geo_level == "tract":
        if not state_code:
            raise ValueError("state_code is required for tract-level query.")
        params["for"] = f"tract:{tract_code or '*'}"
        params["in"] = f"state:{state_code} county:{county_code or '*'}"
    elif geo_level == "zcta":
        params["for"] = f"zip code tabulation area:{zcta_code or '*'}"
        if (
            state_code
            and year is not None
            and year <= ZCTA_STATE_NESTING_LAST_YEAR
        ):
            params["in"] = f"state:{state_code}"
    elif geo_level == "msa":
        params["for"] = f"{GEO_LEVELS['msa'][0]}:{msa_code or '*'}"
    elif geo_level == "us":
        params["for"] = "us:1"
    else:
        raise ValueError(f"Unsupported geography level: {geo_level}")
    return params


class CensusAPIClient:
    """Client for interacting with the US Census Bureau ACS APIs."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = BASE_URL,
        timeout: int = 30,
        max_retries: int = 3,
        backoff_factor: float = 1.0,
        cache: Optional[QueryCache] = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else DEFAULT_API_KEY
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cache = cache
        self.last_error: Optional[str] = None

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

    def endpoint(self, year: int, dataset: str = "acs5") -> str:
        if dataset not in DATASETS:
            raise ValueError(
                f"Unsupported dataset: {dataset}. Choose from {sorted(DATASETS)}."
            )
        return f"{self.base_url}/{year}/{DATASETS[dataset]}"

    def build_request(
        self,
        year: int,
        state_code: Optional[str],
        table_code: str,
        geo_level: str = "place",
        place_code: Optional[str] = None,
        county_code: Optional[str] = None,
        dataset: Optional[str] = None,
        tract_code: Optional[str] = None,
        zcta_code: Optional[str] = None,
        msa_code: Optional[str] = None,
        get: Optional[str] = None,
    ) -> Tuple[str, Dict[str, str]]:
        """Return the (endpoint URL, query params) for a data request."""
        dataset = dataset or infer_dataset(table_code)
        params: Dict[str, str] = {"get": get or f"NAME,group({table_code})"}
        params.update(
            build_geo_params(
                geo_level,
                state_code=state_code,
                place_code=place_code,
                county_code=county_code,
                tract_code=tract_code,
                zcta_code=zcta_code,
                msa_code=msa_code,
                year=year,
            )
        )
        if self.api_key:
            params["key"] = self.api_key
        return self.endpoint(year, dataset), params

    def _get_json(self, endpoint: str, params: Dict[str, str]) -> Optional[Any]:
        if self.cache is not None:
            cached = self.cache.get(endpoint, params)
            if cached is not None:
                return cached
        try:
            response = self.session.get(endpoint, params=params, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            self.last_error = str(exc)
            return None
        self.last_error = None
        if self.cache is not None and payload:
            self.cache.set(endpoint, params, payload)
        return payload

    def fetch(
        self,
        year: int,
        state_code: Optional[str],
        table_code: str,
        geo_level: str = "place",
        place_code: Optional[str] = None,
        county_code: Optional[str] = None,
        dataset: Optional[str] = None,
        tract_code: Optional[str] = None,
        zcta_code: Optional[str] = None,
        msa_code: Optional[str] = None,
    ) -> Optional[List[List[str]]]:
        """Fetch ACS 5-year data for a specific year, geography, and table.

        Geography levels: 'us', 'state', 'county', 'tract', 'place', 'zcta', 'msa'.
        Dataset ('acs5', 'profile', 'subject', 'cprofile') is inferred from the
        table code when not given.
        """
        endpoint, params = self.build_request(
            year=year,
            state_code=state_code,
            table_code=table_code,
            geo_level=geo_level,
            place_code=place_code,
            county_code=county_code,
            dataset=dataset,
            tract_code=tract_code,
            zcta_code=zcta_code,
            msa_code=msa_code,
        )
        return self._get_json(endpoint, params)

    def list_geographies(
        self,
        year: int,
        geo_level: str,
        state_code: Optional[str] = None,
        county_code: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """List entities (NAME + FIPS components) at a geography level.

        Used to enumerate counties or tracts within a county.
        """
        endpoint = self.endpoint(year, "acs5")
        params = build_geo_params(
            geo_level, state_code=state_code, county_code=county_code, year=year
        )
        params["get"] = "NAME"
        if self.api_key:
            params["key"] = self.api_key
        payload = self._get_json(endpoint, params)
        if not payload or len(payload) < 2:
            return []
        header = payload[0]
        return [dict(zip(header, row)) for row in payload[1:]]

    def fetch_variable_metadata(
        self, year: int, table_code: str, dataset: Optional[str] = None
    ) -> Dict[str, Dict[str, Any]]:
        """Fetch variable labels/concepts for a table group from the API."""
        dataset = dataset or infer_dataset(table_code)
        endpoint = f"{self.endpoint(year, dataset)}/groups/{table_code}.json"
        payload = self._get_json(endpoint, {})
        if not isinstance(payload, dict):
            return {}
        return payload.get("variables", {}) or {}


# Module-level default client (no cache; callers opt in explicitly)
_default_client = CensusAPIClient()


def fetch_census_data(
    year: int,
    state_code: Optional[str],
    table_code: str,
    geo_level: str = "place",
    place_code: Optional[str] = None,
    county_code: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: int = 30,
    dataset: Optional[str] = None,
    tract_code: Optional[str] = None,
    zcta_code: Optional[str] = None,
    msa_code: Optional[str] = None,
    cache: Optional[QueryCache] = None,
) -> Optional[List[List[str]]]:
    """Convenience function delegating to CensusAPIClient."""
    if api_key is not None or timeout != 30 or cache is not None:
        client = CensusAPIClient(api_key=api_key, timeout=timeout, cache=cache)
    else:
        client = _default_client
    return client.fetch(
        year=year,
        state_code=state_code,
        table_code=table_code,
        geo_level=geo_level,
        place_code=place_code,
        county_code=county_code,
        dataset=dataset,
        tract_code=tract_code,
        zcta_code=zcta_code,
        msa_code=msa_code,
    )
