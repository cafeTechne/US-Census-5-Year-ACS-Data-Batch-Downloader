# SPDX-License-Identifier: AGPL-3.0-or-later
"""Census Bureau API client with retry, backoff, and optional local caching."""

import os
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from census.cache import QueryCache
from census.decennial import (
    DECENNIAL_DATASETS,
    DECENNIAL_DHC,
    PRODUCT_DECENNIAL,
    SAMPLING_REFUSAL,
    is_decennial,
)

load_dotenv()

BASE_URL = "https://api.census.gov/data"
DEFAULT_API_KEY = os.getenv("CENSUS_API_KEY", "")

# ACS dataset paths: 5-year (default) and 1-year releases
DATASETS: Dict[str, str] = {
    "acs5": "acs/acs5",  # Detailed tables (B/C)
    "profile": "acs/acs5/profile",  # Data Profiles (DP02-DP05)
    "subject": "acs/acs5/subject",  # Subject tables (S....)
    "cprofile": "acs/acs5/cprofile",  # Comparison Profiles (CP02-CP05)
    # ACS 1-year (api.census.gov/data.json catalog: acs1 and acs1/profile from
    # 2005, acs1/subject from 2010, acs1/cprofile from 2010; no 2020 release)
    "acs1": "acs/acs1",
    "acs1/profile": "acs/acs1/profile",
    "acs1/subject": "acs/acs1/subject",
    "acs1/cprofile": "acs/acs1/cprofile",
    # 2020 Decennial Census (census.decennial): P.L. 94-171 redistricting data
    # and the Demographic and Housing Characteristics File. Not sample-based.
    "dec/pl": "dec/pl",
    "dec/dhc": "dec/dhc",
}

# ACS products. Every ACS dataset key belongs to exactly one product. The
# decennial datasets form the separate product PRODUCT_DECENNIAL, which is not
# in PRODUCTS: ACS-only tools (overlap, replicates, PUMS) never accept it.
PRODUCT_5YR = "5-year"
PRODUCT_1YR = "1-year"
PRODUCTS: Tuple[str, ...] = (PRODUCT_5YR, PRODUCT_1YR)
PRODUCT_PERIOD_YEARS: Dict[str, int] = {PRODUCT_5YR: 5, PRODUCT_1YR: 1}
_TABLE_TYPES = ("detailed", "profile", "subject", "cprofile")
PRODUCT_DATASETS: Dict[str, Dict[str, str]] = {
    PRODUCT_5YR: dict(zip(_TABLE_TYPES, ("acs5", "profile", "subject", "cprofile"))),
    PRODUCT_1YR: dict(zip(_TABLE_TYPES, ("acs1", "acs1/profile", "acs1/subject", "acs1/cprofile"))),
}
_DATASET_TABLE_TYPE: Dict[str, Tuple[str, str]] = {
    ds: (product, kind) for product, m in PRODUCT_DATASETS.items() for kind, ds in m.items()
}

# 1-year estimates are published only for areas with 65,000+ population
# (https://www.census.gov/programs-surveys/acs/geography-acs/areas-published.html),
# and not for tracts, block groups or ZCTAs (acs1 geography.json, 2005-2024).
ACS1_MIN_POPULATION = 65000
ACS1_GEO_LEVELS = ("us", "state", "county", "place", "msa")
ACS1_AREAS_URL = "https://www.census.gov/programs-surveys/acs/geography-acs/areas-published.html"
# 2020: the standard 1-year release was not published (COVID-19 data-collection
# disruption); Census released only experimental estimates with
# experimental weights, as downloadable tables outside the API and data.census.gov.
ACS1_EXPERIMENTAL_YEAR = 2020
ACS1_EXPERIMENTAL_URL = "https://www.census.gov/programs-surveys/acs/data/experimental-data.html"
# "Can I compare ACS 1-year estimates with ACS 5-year estimates? No. Only
# compare 1-year estimates with 1-year estimates, and 5-year estimates with
# 5-year estimates."
COMPARING_ACS_URL = "https://www.census.gov/programs-surveys/acs/guidance/comparing-acs-data.html"
MIXED_PRODUCTS_MESSAGE = (
    "Comparing ACS 1-year with ACS 5-year estimates is not supported: Census advises "
    "\"Only compare 1-year estimates with 1-year estimates, and 5-year estimates with "
    f"5-year estimates\" ({COMPARING_ACS_URL})."
)

# Geography levels -> (Census API "for" predicate name, summary level)
GEO_LEVELS: Dict[str, Tuple[str, str]] = {
    "us": ("us", "010"),
    "state": ("state", "040"),
    "county": ("county", "050"),
    "tract": ("tract", "140"),
    "block group": ("block group", "150"),
    "place": ("place", "160"),
    "msa": ("metropolitan statistical area/micropolitan statistical area", "310"),
    "zcta": ("zip code tabulation area", "860"),
    # Decennial datasets only (census.decennial): GEOIDs are 15 digits.
    "block": ("block", "100"),
}

# ZCTAs were nested within states (summary level 871) through the 2019 ACS
# 5-year release; from 2020 they are only available nationally (level 860).
ZCTA_STATE_NESTING_LAST_YEAR = 2019

# Block groups (summary level 150) are in the ACS 5-year API from the 2013
# release (2009-2012 return "unknown/unsupported geography hierarchy"), and
# only in the detailed tables (acs5); subject and profile tables are not
# published for block groups. GEOIDs are 12 digits: SS CCC TTTTTT G.
BLOCK_GROUP = "block group"
BLOCK_GROUP_ALIASES = ("block group", "block_group", "blockgroup", "bg", "150")
BLOCK_GROUP_FIRST_YEAR = 2013


def normalize_geo_level(geo_level: str) -> str:
    """Canonical geography level name (``'block_group'``/``'bg'`` -> ``'block group'``)."""
    key = str(geo_level).strip().lower()
    return BLOCK_GROUP if key in BLOCK_GROUP_ALIASES else key


def _table_type(table_code: str) -> str:
    code = table_code.upper()
    if code.startswith("DP"):
        return "profile"
    if code.startswith("CP"):
        return "cprofile"
    if code.startswith("S"):
        return "subject"
    return "detailed"


def normalize_product(product: Optional[str]) -> str:
    """Canonical product name: '5-year' (default) or '1-year' ('acs1', '1yr', '1' accepted)."""
    if product is None:
        return PRODUCT_5YR
    key = str(product).strip().lower().replace("_", "-").replace(" ", "-")
    if key in ("5-year", "5yr", "5-yr", "5", "acs5", "five-year"):
        return PRODUCT_5YR
    if key in ("1-year", "1yr", "1-yr", "1", "acs1", "one-year"):
        return PRODUCT_1YR
    raise ValueError(f"Unsupported ACS product '{product}'. Choose from {list(PRODUCTS)}.")


def infer_dataset(table_code: str, product: str = PRODUCT_5YR) -> str:
    """Pick the endpoint for a table code prefix within a product (5-year by default).

    ``product='decennial'`` selects the 2020 DHC ('dec/dhc'); pass
    ``dataset='dec/pl'`` explicitly for P.L. 94-171 tables (table IDs are
    product-specific: P.L. 94-171 P1 is Race, DHC P1 is Total Population).
    """
    if is_decennial(product):
        return product if product in DECENNIAL_DATASETS else DECENNIAL_DHC
    return PRODUCT_DATASETS[normalize_product(product)][_table_type(table_code)]


def dataset_product(dataset: str) -> str:
    """'5-year', '1-year' or 'decennial' for a dataset key (ValueError when unknown)."""
    if dataset in DECENNIAL_DATASETS:
        return PRODUCT_DECENNIAL
    try:
        return _DATASET_TABLE_TYPE[dataset][0]
    except KeyError:
        raise ValueError(f"Unsupported dataset: {dataset}. Choose from {sorted(DATASETS)}.") from None


def dataset_for_product(dataset: str, product: str) -> str:
    """The same table type in another product ('profile', '1-year' -> 'acs1/profile')."""
    kind = _DATASET_TABLE_TYPE[dataset][1] if dataset in _DATASET_TABLE_TYPE else dataset
    return PRODUCT_DATASETS[normalize_product(product)][kind]


def require_same_product(*products_or_datasets: str) -> str:
    """Return the shared product; raise ValueError (Census guidance) when 1-year and 5-year mix.

    Accepts product names or dataset keys. A decennial product or dataset
    raises ValueError (census.decennial.SAMPLING_REFUSAL): callers are ACS
    sampling tools, which do not apply to census counts.
    """
    if any(is_decennial(p) for p in products_or_datasets):
        raise ValueError(SAMPLING_REFUSAL)
    found = set()
    for p in products_or_datasets:
        if p is None:
            continue
        found.add(dataset_product(p) if p in DATASETS else normalize_product(p))
    if len(found) > 1:
        raise ValueError(MIXED_PRODUCTS_MESSAGE)
    return found.pop() if found else PRODUCT_5YR


def build_geo_params(
    geo_level: str,
    state_code: Optional[str] = None,
    place_code: Optional[str] = None,
    county_code: Optional[str] = None,
    tract_code: Optional[str] = None,
    zcta_code: Optional[str] = None,
    msa_code: Optional[str] = None,
    year: Optional[int] = None,
    block_group_code: Optional[str] = None,
) -> Dict[str, str]:
    """Build the Census API ``for`` / ``in`` predicates for a geography level.

    ``state_code`` may be one FIPS code or a comma-separated list. For state,
    county and place it may also be ``*`` (every state, including Puerto Rico);
    the API rejects a state wildcard for tracts ("wildcard not allowed for
    'state' in geography hierarchy"), so tract queries need explicit states.
    """
    params: Dict[str, str] = {}
    geo_level = normalize_geo_level(geo_level)
    if geo_level == "block":
        if not state_code or "*" in state_code or "," in state_code:
            raise ValueError("Block queries need exactly one state; request one state at a time.")
        params["for"] = "block:*"
        inn = f"state:{state_code} county:{county_code or '*'}"
        if tract_code:
            inn += f" tract:{tract_code}"
        params["in"] = inn
        return params
    if geo_level == BLOCK_GROUP:
        if not state_code or "*" in state_code or "," in state_code:
            raise ValueError("Block-group queries need exactly one state; request one state at a time.")
        if year is not None and year < BLOCK_GROUP_FIRST_YEAR:
            raise ValueError(
                f"Block groups are in the ACS 5-year API from the {BLOCK_GROUP_FIRST_YEAR} release; got {year}."
            )
        params["for"] = f"block group:{block_group_code or '*'}"
        inn = f"state:{state_code} county:{county_code or '*'}"
        if tract_code:
            inn += f" tract:{tract_code}"
        params["in"] = inn
        return params
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
        if "*" in state_code:
            raise ValueError(
                "Tract queries cannot use a state wildcard; request one state at a time."
            )
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
        synthetic: Optional[bool] = None,
    ) -> None:
        # synthetic: True when this client serves generated (non-Census) data;
        # None defers to the cache's synthetic marker (QueryCache.mark_synthetic)
        self._synthetic = synthetic
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

    @property
    def synthetic(self) -> bool:
        """True when the data this client serves are synthetic (explicit flag or marked cache)."""
        if getattr(self, "_synthetic", None) is not None:
            return bool(self._synthetic)
        return self.synthetic_marker() is not None

    @synthetic.setter
    def synthetic(self, value: Optional[bool]) -> None:
        self._synthetic = value

    def synthetic_marker(self) -> Optional[Dict[str, Any]]:
        cache = getattr(self, "cache", None)
        if cache is None or not hasattr(cache, "synthetic_marker"):
            return None
        try:
            return cache.synthetic_marker()
        except Exception:  # noqa: BLE001 - an unreadable marker is no marker
            return None

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
        block_group_code: Optional[str] = None,
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
                block_group_code=block_group_code,
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
        """Fetch ACS data for a specific year, geography, and table.

        Geography levels: 'us', 'state', 'county', 'tract', 'block group'
        (one county per request; 2013+ releases, acs5 only), 'place', 'zcta', 'msa'.
        Dataset ('acs5', 'profile', 'subject', 'cprofile'; 1-year: 'acs1',
        'acs1/profile', 'acs1/subject', 'acs1/cprofile'; 2020 Census: 'dec/pl',
        'dec/dhc', which also accept 'block') is inferred from the table code
        (5-year) when not given.
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
        dataset: str = "acs5",
    ) -> List[Dict[str, str]]:
        """List entities (NAME + FIPS components) at a geography level.

        Used to enumerate counties or tracts within a county, and (with a
        1-year ``dataset``) the areas a 1-year release publishes.
        """
        endpoint = self.endpoint(year, dataset)
        params = build_geo_params(
            geo_level, state_code=state_code, county_code=county_code, year=year,
            place_code="*" if normalize_geo_level(geo_level) == "place" else None,
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
