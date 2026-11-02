"""US Census 5-Year ACS Data Batch Downloader.

A desktop application to fetch, format, and export American Community Survey (ACS)
5-year dataset tables across multiple years, geography levels (place, state, county),
and output formats (CSV, Excel).
"""

from datetime import datetime
import json
import os
import queue
import re
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
import openpyxl  # Ensure dependency presence for Excel export
import pandas as pd
import requests
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

load_dotenv()


def get_resource_path(relative_path: str) -> str:
    """Get the path to the resource in either development or packaged application."""
    if getattr(sys, "_MEIPASS", False):
        base_path = sys._MEIPASS  # type: ignore[attr-defined]
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


# Dracula Theme Color Palette
BACKGROUND_COLOR = "#282A36"
FOREGROUND_COLOR = "#F8F8F2"
SELECTION_COLOR = "#44475A"
COMMENT = "#6272A4"
RED = "#FF5555"
ORANGE = "#FFB86C"
YELLOW = "#F1FA8C"
GREEN = "#50FA7B"
PURPLE = "#BD93F9"
CYAN = "#8BE9FD"
PINK = "#FF79C6"

BASE_URL = "https://api.census.gov/data"
API_KEY = os.getenv("CENSUS_API_KEY", "")

VARIABLE_DEFINITIONS: Dict[str, str] = {}

STATE_FILES: List[str] = [
    "st01_al_place2020.txt",
    "st02_ak_place2020.txt",
    "st04_az_place2020.txt",
    "st05_ar_place2020.txt",
    "st06_ca_place2020.txt",
    "st08_co_place2020.txt",
    "st09_ct_place2020.txt",
    "st10_de_place2020.txt",
    "st11_dc_place2020.txt",
    "st12_fl_place2020.txt",
    "st13_ga_place2020.txt",
    "st15_hi_place2020.txt",
    "st16_id_place2020.txt",
    "st17_il_place2020.txt",
    "st18_in_place2020.txt",
    "st19_ia_place2020.txt",
    "st20_ks_place2020.txt",
    "st21_ky_place2020.txt",
    "st22_la_place2020.txt",
    "st23_me_place2020.txt",
    "st24_md_place2020.txt",
    "st25_ma_place2020.txt",
    "st26_mi_place2020.txt",
    "st27_mn_place2020.txt",
    "st28_ms_place2020.txt",
    "st29_mo_place2020.txt",
    "st30_mt_place2020.txt",
    "st31_ne_place2020.txt",
    "st32_nv_place2020.txt",
    "st33_nh_place2020.txt",
    "st34_nj_place2020.txt",
    "st35_nm_place2020.txt",
    "st36_ny_place2020.txt",
    "st37_nc_place2020.txt",
    "st38_nd_place2020.txt",
    "st39_oh_place2020.txt",
    "st40_ok_place2020.txt",
    "st41_or_place2020.txt",
    "st42_pa_place2020.txt",
    "st44_ri_place2020.txt",
    "st45_sc_place2020.txt",
    "st46_sd_place2020.txt",
    "st47_tn_place2020.txt",
    "st48_tx_place2020.txt",
    "st49_ut_place2020.txt",
    "st50_vt_place2020.txt",
    "st51_va_place2020.txt",
    "st53_wa_place2020.txt",
    "st54_wv_place2020.txt",
    "st55_wi_place2020.txt",
    "st56_wy_place2020.txt",
    "st60_as_place2020.txt",
    "st66_gu_place2020.txt",
    "st69_mp_place2020.txt",
    "st72_pr_place2020.txt",
    "st78_vi_place2020.txt",
]

STATE_MAPPING: Dict[str, str] = {
    "01": "Alabama",
    "02": "Alaska",
    "04": "Arizona",
    "05": "Arkansas",
    "06": "California",
    "08": "Colorado",
    "09": "Connecticut",
    "10": "Delaware",
    "11": "District of Columbia",
    "12": "Florida",
    "13": "Georgia",
    "15": "Hawaii",
    "16": "Idaho",
    "17": "Illinois",
    "18": "Indiana",
    "19": "Iowa",
    "20": "Kansas",
    "21": "Kentucky",
    "22": "Louisiana",
    "23": "Maine",
    "24": "Maryland",
    "25": "Massachusetts",
    "26": "Michigan",
    "27": "Minnesota",
    "28": "Mississippi",
    "29": "Missouri",
    "30": "Montana",
    "31": "Nebraska",
    "32": "Nevada",
    "33": "New Hampshire",
    "34": "New Jersey",
    "35": "New Mexico",
    "36": "New York",
    "37": "North Carolina",
    "38": "North Dakota",
    "39": "Ohio",
    "40": "Oklahoma",
    "41": "Oregon",
    "42": "Pennsylvania",
    "44": "Rhode Island",
    "45": "South Carolina",
    "46": "South Dakota",
    "47": "Tennessee",
    "48": "Texas",
    "49": "Utah",
    "50": "Vermont",
    "51": "Virginia",
    "53": "Washington",
    "54": "West Virginia",
    "55": "Wisconsin",
    "56": "Wyoming",
    "60": "American Samoa",
    "66": "Guam",
    "69": "Northern Mariana Islands",
    "72": "Puerto Rico",
    "74": "U.S. Minor Outlying Islands",
    "78": "U.S. Virgin Islands",
}

FIPS_MAPPING: Dict[str, str] = {v: k for k, v in STATE_MAPPING.items()}


# --------- Data Loading & Processing Functions -------------


def extract_state_abbr_from_filename(filename: str) -> str:
    """Extract numeric two-digit state FIPS code from place filename."""
    return filename[2:4]


def get_local_places_file(state_abbr: str) -> Optional[str]:
    """Return the absolute path to the local places file for a state code."""
    matching_files = [file for file in STATE_FILES if f"st{state_abbr}" in file]
    if matching_files:
        return get_resource_path(os.path.join("state_places", matching_files[0]))
    return None


def load_variable_definitions(filepath: Optional[str] = None) -> Dict[str, str]:
    """Load and cache variable definitions from variables.json."""
    global VARIABLE_DEFINITIONS
    if VARIABLE_DEFINITIONS:
        return VARIABLE_DEFINITIONS

    target_path = filepath or get_resource_path("variables.json")
    if os.path.exists(target_path):
        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            VARIABLE_DEFINITIONS = {
                var: details["label"]
                for var, details in data.get("variables", {}).items()
            }
    return VARIABLE_DEFINITIONS


def get_variable_descriptions(table_code: str) -> Dict[str, str]:
    """Retrieve variable descriptions starting with the given table code."""
    if not isinstance(table_code, str):
        raise ValueError(
            f"Expected table_code to be a string but got {type(table_code)}"
        )

    if not VARIABLE_DEFINITIONS:
        load_variable_definitions()

    return {
        var: label
        for var, label in VARIABLE_DEFINITIONS.items()
        if var.startswith(table_code)
    }


def load_places_from_local(path: str) -> List[Tuple[str, str, str, str, str]]:
    """Load place entries from a pipe-delimited Census place definition file."""
    with open(path, "r", encoding="utf-8", errors="replace") as file:
        lines = file.readlines()
    lines = lines[1:]  # Skip header

    places: List[Tuple[str, str, str, str, str]] = []
    for line in lines:
        parts = line.strip().split("|")
        if len(parts) >= 9:
            _, state_code, place_code, _, place_name, place_type, _, _, counties = (
                parts[:9]
            )
            places.append((state_code, place_code, place_name, place_type, counties))
    return places


def load_tables_from_local(path: str) -> Dict[str, str]:
    """Load table groups mapping (table code -> description) from groups.json."""
    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)
    tables = {item["name"]: item["description"] for item in data.get("groups", [])}
    for key, value in tables.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(
                f"Invalid structure for all_tables. Key: {key}, Value: {value}"
            )
    return tables


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
    """Fetch Census ACS 5-year data for given parameters.

    Supports geography levels: 'place', 'state', 'county'.
    """
    key = api_key if api_key is not None else API_KEY
    endpoint = f"{BASE_URL}/{year}/acs/acs5"
    params: Dict[str, str] = {
        "get": f"NAME,group({table_code})",
        "key": key,
    }

    if geo_level == "place":
        if not place_code:
            raise ValueError("place_code is required for place-level fetch.")
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
        response = requests.get(endpoint, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None


def format_dataframe(df: pd.DataFrame, table_code: str) -> pd.DataFrame:
    """Rename raw Census variable codes to their human-readable labels."""
    variable_descriptions = get_variable_descriptions(table_code)
    df.rename(columns=variable_descriptions, inplace=True)
    return df


def handle_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Deduplicate identical column headers by appending an index suffix."""
    cols = pd.Series(df.columns)
    for dup in cols[cols.duplicated()].unique():
        cols[cols[cols == dup].index.values.tolist()] = [
            dup + "_" + str(i) if i != 0 else dup for i in range(sum(cols == dup))
        ]
    df.columns = cols
    return df


def enhanced_melt(
    df: pd.DataFrame,
    id_vars: List[str],
    var_name: str = "Variable",
    value_name: str = "Value",
) -> pd.DataFrame:
    """Melt a DataFrame from wide to long format with validation."""
    if df.empty:
        return pd.DataFrame()

    missing_columns = [col for col in id_vars if col not in df.columns]
    if missing_columns:
        return pd.DataFrame()

    return df.melt(id_vars=id_vars, var_name=var_name, value_name=value_name)


def sanitize_filename(name: str) -> str:
    """Replace non-alphanumeric filesystem characters with underscores."""
    return re.sub(r'[\\/*?:"<>| ]', "_", name).strip("_")


def process_data_and_save(
    formatted_df: pd.DataFrame,
    folder_name: str,
    entity_name: str,
    table_code: str,
    year: int,
    output_format: str = "both",
    excel_accumulator: Optional[Dict[str, List[pd.DataFrame]]] = None,
) -> None:
    """Save wide/melted CSVs or accumulate DataFrames for Excel export.

    Inserts 'Year' column at column 0 for multi-year disambiguation.
    output_format options: 'both', 'wide_only', 'melted_only', 'excel'.
    """
    df = formatted_df.copy()

    # Prepend Year column if not already present
    if "Year" not in df.columns:
        df.insert(0, "Year", year)

    df = handle_duplicate_columns(df)

    if "NAME" not in df.columns or "state" not in df.columns:
        return

    clean_entity = sanitize_filename(entity_name)

    # Wide format CSV export
    if output_format in ("both", "wide_only"):
        compiled_file_name = os.path.join(folder_name, "compiled_unmelted_data.csv")
        write_headers = not os.path.exists(compiled_file_name)
        with open(compiled_file_name, "a", newline="", encoding="utf-8") as f:
            df.to_csv(f, header=write_headers, index=False)

    # Melted format CSV export
    id_vars = [
        col for col in ["Year", "NAME", "state", "place", "county"] if col in df.columns
    ]
    melted_df = enhanced_melt(df, id_vars, "Variable", "Value")

    if output_format in ("both", "melted_only"):
        if not melted_df.empty:
            melted_file_name = os.path.join(
                folder_name, f"{clean_entity}_{table_code}_{year}.csv"
            )
            melted_df.to_csv(melted_file_name, index=False, encoding="utf-8")

    # Excel accumulator
    if output_format == "excel" and excel_accumulator is not None:
        excel_accumulator.setdefault("wide", []).append(df)
        if not melted_df.empty:
            excel_accumulator.setdefault("melted", []).append(melted_df)


def save_excel_workbook(
    folder_name: str,
    entity_name: str,
    table_code: str,
    excel_accumulator: Dict[str, List[pd.DataFrame]],
) -> Optional[str]:
    """Compile accumulated DataFrames into an Excel workbook with multiple sheets."""
    clean_entity = sanitize_filename(entity_name)
    excel_file_path = os.path.join(
        folder_name, f"{clean_entity}_{table_code}_data.xlsx"
    )

    wide_list = excel_accumulator.get("wide", [])
    melted_list = excel_accumulator.get("melted", [])

    if not wide_list and not melted_list:
        return None

    combined_wide = (
        pd.concat(wide_list, ignore_index=True) if wide_list else pd.DataFrame()
    )
    combined_melted = (
        pd.concat(melted_list, ignore_index=True) if melted_list else pd.DataFrame()
    )

    with pd.ExcelWriter(excel_file_path, engine="openpyxl") as writer:
        if not combined_wide.empty:
            combined_wide.to_excel(writer, sheet_name="Wide_Data", index=False)
        if not combined_melted.empty:
            combined_melted.to_excel(writer, sheet_name="Melted_Data", index=False)

    return excel_file_path


# --------- GUI Application Class -------------


class ACSDownloaderApp:
    """Tkinter-based GUI for Census ACS batch data downloading."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("ACS 5-Year Data Batch Downloader")
        self.root.geometry("1000x760")
        self.root.minsize(920, 680)
        self.root.config(bg=BACKGROUND_COLOR)

        self.msg_queue: queue.Queue[Tuple[str, Any]] = queue.Queue()
        self.is_downloading = False

        # Data caches
        self.all_tables: Dict[str, str] = {}
        self.current_places: List[Tuple[str, str, str, str, str]] = []
        self.displayed_places: List[Tuple[str, str, str, str, str]] = []

        self._load_initial_data()
        self._configure_styles()
        self._build_ui()

        # Start periodic queue polling
        self.root.after(50, self._poll_queue)

    def _load_initial_data(self) -> None:
        """Load table definitions from groups.json."""
        try:
            groups_file = get_resource_path("groups.json")
            if os.path.exists(groups_file):
                self.all_tables = load_tables_from_local(groups_file)
        except Exception as e:
            print(f"Error loading initial groups.json: {e}")

    def _configure_styles(self) -> None:
        """Configure ttk styles with the Dracula color palette."""
        style = ttk.Style()
        style.theme_use("default")

        style.configure("TFrame", background=BACKGROUND_COLOR)
        style.configure(
            "TLabel",
            background=BACKGROUND_COLOR,
            foreground=FOREGROUND_COLOR,
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "TButton",
            background=SELECTION_COLOR,
            foreground=FOREGROUND_COLOR,
            font=("Segoe UI", 10, "bold"),
            padding=6,
        )
        style.map(
            "TButton",
            background=[("active", COMMENT), ("disabled", BACKGROUND_COLOR)],
            foreground=[("disabled", COMMENT)],
        )
        style.configure(
            "TCombobox",
            background=FOREGROUND_COLOR,
            selectbackground=SELECTION_COLOR,
            fieldbackground=FOREGROUND_COLOR,
            font=("Segoe UI", 10),
        )
        style.configure(
            "TRadiobutton",
            background=BACKGROUND_COLOR,
            foreground=FOREGROUND_COLOR,
            font=("Segoe UI", 10),
        )

    def _build_ui(self) -> None:
        """Assemble all frames and widgets in the application window."""
        # Top banner
        header_frame = tk.Frame(self.root, bg=BACKGROUND_COLOR)
        header_frame.pack(fill=tk.X, padx=16, pady=(12, 6))

        title_label = tk.Label(
            header_frame,
            text="US Census 5-Year ACS Data Downloader",
            font=("Segoe UI", 16, "bold"),
            bg=BACKGROUND_COLOR,
            fg=PURPLE,
        )
        title_label.pack(side=tk.LEFT)

        subtitle_label = tk.Label(
            header_frame,
            text="Phase 2 Research UX",
            font=("Segoe UI", 10, "italic"),
            bg=BACKGROUND_COLOR,
            fg=COMMENT,
        )
        subtitle_label.pack(side=tk.LEFT, padx=(12, 0), pady=(4, 0))

        # Main horizontal split frame
        split_frame = tk.Frame(self.root, bg=BACKGROUND_COLOR)
        split_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=6)
        split_frame.columnconfigure(0, weight=1)
        split_frame.columnconfigure(1, weight=1)
        split_frame.rowconfigure(0, weight=1)

        # Left Column: Geography, Years, Options
        left_frame = tk.Frame(split_frame, bg=BACKGROUND_COLOR)
        left_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 8))

        # Right Column: Table Browser & Variable Details
        right_frame = tk.Frame(split_frame, bg=BACKGROUND_COLOR)
        right_frame.grid(row=0, column=1, sticky="nsew", padx=(8, 0))

        self._build_left_panel(left_frame)
        self._build_right_panel(right_frame)
        self._build_bottom_panel()

    def _build_left_panel(self, parent: tk.Frame) -> None:
        """Build geographic, temporal, and format controls."""
        # 1. Geography Level
        geo_label = tk.Label(
            parent,
            text="1. Select Geography Level:",
            font=("Segoe UI", 10, "bold"),
            bg=BACKGROUND_COLOR,
            fg=RED,
        )
        geo_label.pack(anchor="w", pady=(0, 4))

        geo_button_frame = tk.Frame(parent, bg=BACKGROUND_COLOR)
        geo_button_frame.pack(fill=tk.X, pady=(0, 8))

        self.geo_level_var = tk.StringVar(value="place")
        for level, text in [
            ("place", "Place (Cities/CDPs)"),
            ("state", "Entire State"),
            ("county", "All Counties in State"),
        ]:
            rb = ttk.Radiobutton(
                geo_button_frame,
                text=text,
                value=level,
                variable=self.geo_level_var,
                command=self._on_geo_level_changed,
            )
            rb.pack(side=tk.LEFT, padx=(0, 10))

        # 2. State Selector
        state_label = tk.Label(
            parent,
            text="2. Select State or Territory:",
            font=("Segoe UI", 10, "bold"),
            bg=BACKGROUND_COLOR,
            fg=YELLOW,
        )
        state_label.pack(anchor="w", pady=(4, 4))

        state_abbrs = [extract_state_abbr_from_filename(f) for f in STATE_FILES]
        state_values = [f"{STATE_MAPPING.get(a, a)} ({a})" for a in state_abbrs]
        self.state_combo = ttk.Combobox(
            parent, values=state_values, state="readonly", width=38
        )
        self.state_combo.pack(fill=tk.X, pady=(0, 8))
        self.state_combo.bind("<<ComboboxSelected>>", self._on_state_selected)

        # 3. Place Multi-Select Box (visible for place mode)
        self.place_section_frame = tk.Frame(parent, bg=BACKGROUND_COLOR)
        self.place_section_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 8))

        place_header = tk.Frame(self.place_section_frame, bg=BACKGROUND_COLOR)
        place_header.pack(fill=tk.X, pady=(0, 4))

        tk.Label(
            place_header,
            text="3. Select Places (Multi-select):",
            font=("Segoe UI", 10, "bold"),
            bg=BACKGROUND_COLOR,
            fg=GREEN,
        ).pack(side=tk.LEFT)

        self.place_count_label = tk.Label(
            place_header,
            text="0 selected",
            font=("Segoe UI", 9, "italic"),
            bg=BACKGROUND_COLOR,
            fg=CYAN,
        )
        self.place_count_label.pack(side=tk.RIGHT)

        # Place Filter
        filter_frame = tk.Frame(self.place_section_frame, bg=BACKGROUND_COLOR)
        filter_frame.pack(fill=tk.X, pady=(0, 4))
        tk.Label(
            filter_frame,
            text="Filter:",
            font=("Segoe UI", 9),
            bg=BACKGROUND_COLOR,
            fg=FOREGROUND_COLOR,
        ).pack(side=tk.LEFT, padx=(0, 4))

        self.place_filter_entry = ttk.Entry(filter_frame)
        self.place_filter_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.place_filter_entry.bind("<KeyRelease>", self._on_place_filter_changed)

        # Place Listbox with Scrollbar
        listbox_container = tk.Frame(self.place_section_frame, bg=BACKGROUND_COLOR)
        listbox_container.pack(fill=tk.BOTH, expand=True)

        self.place_listbox = tk.Listbox(
            listbox_container,
            selectmode=tk.EXTENDED,
            bg=SELECTION_COLOR,
            fg=FOREGROUND_COLOR,
            selectbackground=COMMENT,
            selectforeground=FOREGROUND_COLOR,
            highlightthickness=0,
            relief="flat",
            height=6,
            exportselection=False,
        )
        self.place_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.place_listbox.bind("<<ListboxSelect>>", self._on_place_selection_changed)

        place_scrollbar = ttk.Scrollbar(
            listbox_container, orient=tk.VERTICAL, command=self.place_listbox.yview
        )
        place_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.place_listbox.config(yscrollcommand=place_scrollbar.set)

        # Place Action Buttons
        btn_row = tk.Frame(self.place_section_frame, bg=BACKGROUND_COLOR)
        btn_row.pack(fill=tk.X, pady=(4, 0))

        ttk.Button(btn_row, text="Select All", command=self._select_all_places).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_row, text="Clear", command=self._clear_place_selection).pack(
            side=tk.LEFT
        )

        # 4. Year Range
        year_label = tk.Label(
            parent,
            text="4. Year Range:",
            font=("Segoe UI", 10, "bold"),
            bg=BACKGROUND_COLOR,
            fg=ORANGE,
        )
        year_label.pack(anchor="w", pady=(6, 4))

        year_frame = tk.Frame(parent, bg=BACKGROUND_COLOR)
        year_frame.pack(anchor="w", pady=(0, 8))

        tk.Label(
            year_frame, text="From:", bg=BACKGROUND_COLOR, fg=FOREGROUND_COLOR
        ).pack(side=tk.LEFT)
        self.start_year_spin = tk.Spinbox(
            year_frame,
            from_=2005,
            to=2023,
            width=6,
            bg=SELECTION_COLOR,
            fg=FOREGROUND_COLOR,
            buttonbackground=SELECTION_COLOR,
            highlightthickness=0,
            command=self._update_call_estimate,
        )
        self.start_year_spin.delete(0, tk.END)
        self.start_year_spin.insert(0, "2010")
        self.start_year_spin.pack(side=tk.LEFT, padx=(4, 12))
        self.start_year_spin.bind(
            "<KeyRelease>", lambda e: self._update_call_estimate()
        )

        tk.Label(year_frame, text="To:", bg=BACKGROUND_COLOR, fg=FOREGROUND_COLOR).pack(
            side=tk.LEFT
        )
        self.end_year_spin = tk.Spinbox(
            year_frame,
            from_=2005,
            to=2023,
            width=6,
            bg=SELECTION_COLOR,
            fg=FOREGROUND_COLOR,
            buttonbackground=SELECTION_COLOR,
            highlightthickness=0,
            command=self._update_call_estimate,
        )
        self.end_year_spin.delete(0, tk.END)
        self.end_year_spin.insert(0, "2022")
        self.end_year_spin.pack(side=tk.LEFT, padx=(4, 0))
        self.end_year_spin.bind("<KeyRelease>", lambda e: self._update_call_estimate())

        # 5. Output Format
        format_label = tk.Label(
            parent,
            text="5. Output Format:",
            font=("Segoe UI", 10, "bold"),
            bg=BACKGROUND_COLOR,
            fg=PINK,
        )
        format_label.pack(anchor="w", pady=(4, 4))

        self.format_combo = ttk.Combobox(
            parent,
            values=[
                "Both Wide & Melted CSVs",
                "Melted CSV only",
                "Wide CSV only",
                "Excel Workbook (.xlsx)",
            ],
            state="readonly",
            width=38,
        )
        self.format_combo.current(0)
        self.format_combo.pack(fill=tk.X, pady=(0, 8))

        # 6. Call Estimation Banner
        self.estimate_frame = tk.Frame(
            parent, bg=SELECTION_COLOR, padx=8, pady=6, relief="flat"
        )
        self.estimate_frame.pack(fill=tk.X, pady=(4, 0))

        self.estimate_label = tk.Label(
            self.estimate_frame,
            text="Estimated API calls: 0",
            font=("Segoe UI", 9, "bold"),
            bg=SELECTION_COLOR,
            fg=GREEN,
        )
        self.estimate_label.pack(anchor="w")

    def _build_right_panel(self, parent: tk.Frame) -> None:
        """Build table browser and variable preview controls."""
        # 1. Search Table
        search_label = tk.Label(
            parent,
            text="Search Table Code or Title:",
            font=("Segoe UI", 10, "bold"),
            bg=BACKGROUND_COLOR,
            fg=GREEN,
        )
        search_label.pack(anchor="w", pady=(0, 4))

        self.table_search_entry = ttk.Entry(parent)
        self.table_search_entry.pack(fill=tk.X, pady=(0, 6))
        self.table_search_entry.bind("<KeyRelease>", self._on_table_search_changed)

        # 2. Table Listbox
        table_list_label = tk.Label(
            parent,
            text="Select Table:",
            font=("Segoe UI", 10, "bold"),
            bg=BACKGROUND_COLOR,
            fg=PINK,
        )
        table_list_label.pack(anchor="w", pady=(2, 4))

        table_container = tk.Frame(parent, bg=BACKGROUND_COLOR)
        table_container.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        self.table_listbox = tk.Listbox(
            table_container,
            bg=SELECTION_COLOR,
            fg=FOREGROUND_COLOR,
            selectbackground=COMMENT,
            selectforeground=FOREGROUND_COLOR,
            highlightthickness=0,
            relief="flat",
            height=7,
            exportselection=False,
        )
        self.table_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.table_listbox.bind("<<ListboxSelect>>", self._on_table_selected)

        table_scroll = ttk.Scrollbar(
            table_container, orient=tk.VERTICAL, command=self.table_listbox.yview
        )
        table_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.table_listbox.config(yscrollcommand=table_scroll.set)

        self._populate_table_listbox(sorted(self.all_tables.keys()))

        # 3. Table & Variable Details Text
        details_label = tk.Label(
            parent,
            text="Table & Variable Preview:",
            font=("Segoe UI", 10, "bold"),
            bg=BACKGROUND_COLOR,
            fg=CYAN,
        )
        details_label.pack(anchor="w", pady=(4, 4))

        details_container = tk.Frame(parent, bg=BACKGROUND_COLOR)
        details_container.pack(fill=tk.BOTH, expand=True)

        self.description_text = tk.Text(
            details_container,
            height=8,
            font=("Segoe UI", 9),
            bg=SELECTION_COLOR,
            fg=FOREGROUND_COLOR,
            wrap=tk.WORD,
            highlightthickness=0,
            relief="flat",
        )
        self.description_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        details_scroll = ttk.Scrollbar(
            details_container, orient=tk.VERTICAL, command=self.description_text.yview
        )
        details_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.description_text.config(yscrollcommand=details_scroll.set)

    def _build_bottom_panel(self) -> None:
        """Build execution controls and scrollable status log."""
        bottom_frame = tk.Frame(self.root, bg=BACKGROUND_COLOR)
        bottom_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(4, 12))

        # Controls Row
        control_row = tk.Frame(bottom_frame, bg=BACKGROUND_COLOR)
        control_row.pack(fill=tk.X, pady=(0, 6))

        self.submit_button = ttk.Button(
            control_row, text="Download Data", command=self.on_submit
        )
        self.submit_button.pack(side=tk.LEFT, padx=(0, 16))

        self.status_label = tk.Label(
            control_row,
            text="Status: Ready",
            font=("Segoe UI", 10, "bold"),
            bg=BACKGROUND_COLOR,
            fg=FOREGROUND_COLOR,
        )
        self.status_label.pack(side=tk.LEFT)

        # Scrollable Status Log
        log_container = tk.Frame(bottom_frame, bg=BACKGROUND_COLOR)
        log_container.pack(fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(
            log_container,
            height=6,
            font=("Consolas", 9),
            bg=SELECTION_COLOR,
            fg=FOREGROUND_COLOR,
            wrap=tk.WORD,
            highlightthickness=0,
            relief="flat",
            state=tk.DISABLED,
        )
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        log_scroll = ttk.Scrollbar(
            log_container, orient=tk.VERTICAL, command=self.log_text.yview
        )
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.config(yscrollcommand=log_scroll.set)

    # --------- Event Handlers & Dynamic UI Logic -------------

    def _on_geo_level_changed(self) -> None:
        """Handle geography level radiobutton selection."""
        level = self.geo_level_var.get()
        if level == "place":
            self.place_section_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 8))
        else:
            self.place_section_frame.pack_forget()
        self._update_call_estimate()

    def _on_state_selected(self, event: Optional[tk.Event] = None) -> None:
        """Load places when a new state is chosen."""
        selection = self.state_combo.get().strip()
        matched_abbr = None
        for abbr, name in STATE_MAPPING.items():
            if f"({abbr})" in selection:
                matched_abbr = abbr
                break

        if not matched_abbr:
            return

        places_file = get_local_places_file(matched_abbr)
        if not places_file or not os.path.exists(places_file):
            self.current_places = []
            self.displayed_places = []
            self.place_listbox.delete(0, tk.END)
            self._update_place_count()
            self._update_call_estimate()
            return

        self.current_places = load_places_from_local(places_file)
        self._on_place_filter_changed()

    def _on_place_filter_changed(self, event: Optional[tk.Event] = None) -> None:
        """Filter places list based on search entry text."""
        query = self.place_filter_entry.get().strip().lower()
        self.place_listbox.delete(0, tk.END)

        if not query:
            self.displayed_places = list(self.current_places)
        else:
            self.displayed_places = [
                p for p in self.current_places if query in p[2].lower()
            ]

        for p in self.displayed_places:
            self.place_listbox.insert(tk.END, p[2])

        self._update_place_count()
        self._update_call_estimate()

    def _select_all_places(self) -> None:
        """Select all visible items in the place listbox."""
        self.place_listbox.select_set(0, tk.END)
        self._update_place_count()
        self._update_call_estimate()

    def _clear_place_selection(self) -> None:
        """Deselect all items in the place listbox."""
        self.place_listbox.selection_clear(0, tk.END)
        self._update_place_count()
        self._update_call_estimate()

    def _on_place_selection_changed(self, event: Optional[tk.Event] = None) -> None:
        """Update count label and estimate when selection changes."""
        self._update_place_count()
        self._update_call_estimate()

    def _update_place_count(self) -> None:
        """Update the selected places counter."""
        count = len(self.place_listbox.curselection())
        self.place_count_label.config(text=f"{count} selected")

    def _get_year_range(self) -> Tuple[int, int]:
        """Parse and return validated start and end years."""
        try:
            start_yr = int(self.start_year_spin.get())
            end_yr = int(self.end_year_spin.get())
            return start_yr, end_yr
        except ValueError:
            return 2010, 2022

    def _update_call_estimate(self) -> None:
        """Recalculate and display the estimated number of API calls."""
        level = self.geo_level_var.get()
        start_yr, end_yr = self._get_year_range()
        year_count = max(0, end_yr - start_yr + 1)

        if level == "place":
            selected_indices = self.place_listbox.curselection()
            entity_count = len(selected_indices)
        elif level == "state":
            entity_count = 1 if self.state_combo.get().strip() else 0
        elif level == "county":
            entity_count = 1 if self.state_combo.get().strip() else 0
        else:
            entity_count = 0

        total_calls = entity_count * year_count
        color = GREEN if total_calls <= 50 else (ORANGE if total_calls <= 150 else RED)

        self.estimate_label.config(
            text=f"Estimated API calls: {total_calls} ({entity_count} entities x {year_count} years)",
            fg=color,
        )

    def _populate_table_listbox(self, tables: List[str]) -> None:
        """Populate the table listbox with table codes."""
        self.table_listbox.delete(0, tk.END)
        for code in tables:
            self.table_listbox.insert(tk.END, code)

    def _on_table_search_changed(self, event: Optional[tk.Event] = None) -> None:
        """Filter table codes by code or description."""
        query = self.table_search_entry.get().strip().lower()
        if not query:
            filtered = sorted(self.all_tables.keys())
        else:
            filtered = [
                code
                for code, desc in sorted(self.all_tables.items())
                if query in code.lower() or query in desc.lower()
            ]
        self._populate_table_listbox(filtered)

    def _on_table_selected(self, event: Optional[tk.Event] = None) -> None:
        """Display table description and lazy-loaded variable details."""
        selection = self.table_listbox.curselection()
        if not selection:
            return

        table_code = self.table_listbox.get(selection[0])
        description = self.all_tables.get(table_code, "No description available.")

        # Lazy load variable definitions
        load_variable_definitions()
        variables = get_variable_descriptions(table_code)

        preview_lines = [
            f"Table: {table_code}",
            f"Title: {description}",
            f"Total Variables: {len(variables)}",
            "-" * 50,
        ]

        if variables:
            preview_lines.append("Variables Preview (First 25):")
            for i, (var_code, var_label) in enumerate(sorted(variables.items())):
                if i >= 25:
                    preview_lines.append(
                        f"... and {len(variables) - 25} more variables."
                    )
                    break
                preview_lines.append(f"  {var_code}: {var_label}")
        else:
            preview_lines.append("No variable definitions found for this table.")

        self.description_text.delete(1.0, tk.END)
        self.description_text.insert(tk.END, "\n".join(preview_lines))

    # --------- Thread-Safe Logging & Queue Processing -------------

    def _append_log(self, text: str) -> None:
        """Append timestamped text to the status log."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{timestamp}] {text}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _clear_log(self) -> None:
        """Clear all entries in the status log."""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state=tk.DISABLED)

    def _poll_queue(self) -> None:
        """Poll thread-safe queue for worker events."""
        try:
            while True:
                msg_type, payload = self.msg_queue.get_nowait()
                if msg_type == "LOG":
                    self._append_log(payload)
                elif msg_type == "STATUS":
                    self.status_label.config(text=f"Status: {payload}")
                elif msg_type == "DONE":
                    self._on_download_complete(payload)
                elif msg_type == "ERROR":
                    self._on_download_error(payload)
        except queue.Empty:
            pass
        finally:
            self.root.after(50, self._poll_queue)

    def _on_download_complete(self, summary: str) -> None:
        """Clean up state and notify user on completion."""
        self.is_downloading = False
        self.submit_button.config(state=tk.NORMAL, text="Download Data")
        self.status_label.config(text="Status: Completed")
        self._append_log("Batch download successfully completed.")
        messagebox.showinfo("Done", summary)

    def _on_download_error(self, error_message: str) -> None:
        """Clean up state and notify user on failure."""
        self.is_downloading = False
        self.submit_button.config(state=tk.NORMAL, text="Download Data")
        self.status_label.config(text="Status: Error")
        self._append_log(f"ERROR: {error_message}")
        messagebox.showerror("Download Error", error_message)

    # --------- Submission & Background Thread Worker -------------

    def on_submit(self) -> None:
        """Validate input and trigger background worker thread."""
        if self.is_downloading:
            return

        table_selection = self.table_listbox.curselection()
        if not table_selection:
            messagebox.showerror("Error", "Please select a table.")
            return
        table_code = self.table_listbox.get(table_selection[0])

        state_text = self.state_combo.get().strip()
        matched_state_code = None
        for code, name in STATE_MAPPING.items():
            if f"({code})" in state_text:
                matched_state_code = code
                break
        if not matched_state_code:
            messagebox.showerror("Error", "Please select a valid state.")
            return

        try:
            year_start = int(self.start_year_spin.get())
            year_end = int(self.end_year_spin.get())
        except ValueError:
            messagebox.showerror("Error", "Year values must be valid integers.")
            return

        if year_start > year_end:
            messagebox.showerror(
                "Error", "Start year must be less than or equal to end year."
            )
            return

        geo_level = self.geo_level_var.get()
        entities: List[Tuple[str, Optional[str]]] = []

        if geo_level == "place":
            selected_indices = self.place_listbox.curselection()
            if not selected_indices:
                messagebox.showerror("Error", "Please select at least one place.")
                return
            for idx in selected_indices:
                place_tuple = self.displayed_places[idx]
                # place_tuple is (state_code, place_code, place_name, place_type, counties)
                entities.append((place_tuple[2], place_tuple[1]))
        elif geo_level == "state":
            entities.append((STATE_MAPPING[matched_state_code], None))
        elif geo_level == "county":
            entities.append((f"{STATE_MAPPING[matched_state_code]}_All_Counties", "*"))

        total_calls = len(entities) * (year_end - year_start + 1)
        if total_calls > 50:
            confirm = messagebox.askyesno(
                "Large Batch Warning",
                f"This operation will make {total_calls} API calls to the Census Bureau.\n"
                f"Entities: {len(entities)}, Years: {year_end - year_start + 1}.\n\n"
                f"Do you want to proceed?",
            )
            if not confirm:
                return

        folder_path = filedialog.askdirectory(title="Select Destination Folder")
        if not folder_path:
            return

        format_mapping = {
            "Both Wide & Melted CSVs": "both",
            "Melted CSV only": "melted_only",
            "Wide CSV only": "wide_only",
            "Excel Workbook (.xlsx)": "excel",
        }
        output_format = format_mapping.get(self.format_combo.get(), "both")

        # Disable submit button and initialize state
        self.is_downloading = True
        self.submit_button.config(state=tk.DISABLED, text="Downloading...")
        self.status_label.config(text="Status: Starting download...")
        self._clear_log()

        # Start background daemon thread
        threading.Thread(
            target=self._download_worker,
            args=(
                matched_state_code,
                table_code,
                geo_level,
                entities,
                year_start,
                year_end,
                folder_path,
                output_format,
            ),
            daemon=True,
        ).start()

    def _download_worker(
        self,
        state_code: str,
        table_code: str,
        geo_level: str,
        entities: List[Tuple[str, Optional[str]]],
        year_start: int,
        year_end: int,
        base_folder: str,
        output_format: str,
    ) -> None:
        """Worker thread to execute batch downloads without freezing UI."""
        total_entities = len(entities)
        years = list(range(year_start, year_end + 1))
        total_calls = total_entities * len(years)
        call_index = 0

        self.msg_queue.put(
            (
                "LOG",
                f"Starting download: {total_entities} entities x {len(years)} years "
                f"({total_calls} total calls). Format: {output_format}",
            )
        )

        load_variable_definitions()

        saved_folders: List[str] = []

        for entity_idx, (entity_name, place_or_county_code) in enumerate(entities, 1):
            clean_entity = sanitize_filename(entity_name)
            entity_folder = os.path.join(base_folder, f"{clean_entity}_{table_code}")
            os.makedirs(entity_folder, exist_ok=True)
            saved_folders.append(entity_folder)

            excel_accumulator: Dict[str, List[pd.DataFrame]] = {
                "wide": [],
                "melted": [],
            }

            self.msg_queue.put(
                (
                    "LOG",
                    f"[{entity_idx}/{total_entities}] Processing {entity_name} ({geo_level})...",
                )
            )

            for year in years:
                call_index += 1
                self.msg_queue.put(
                    (
                        "STATUS",
                        f"Fetching [{call_index}/{total_calls}]: {entity_name} ({year})...",
                    )
                )

                data = fetch_census_data(
                    year=year,
                    state_code=state_code,
                    table_code=table_code,
                    geo_level=geo_level,
                    place_code=place_or_county_code if geo_level == "place" else None,
                    county_code=place_or_county_code if geo_level == "county" else None,
                )

                if data and len(data) > 1:
                    df = pd.DataFrame(data[1:], columns=data[0])
                    formatted_df = format_dataframe(df, table_code)
                    process_data_and_save(
                        formatted_df=formatted_df,
                        folder_name=entity_folder,
                        entity_name=entity_name,
                        table_code=table_code,
                        year=year,
                        output_format=output_format,
                        excel_accumulator=excel_accumulator,
                    )
                    self.msg_queue.put(
                        (
                            "LOG",
                            f"  {year}: {len(df)} row(s), {len(formatted_df.columns)} columns saved.",
                        )
                    )
                else:
                    self.msg_queue.put(
                        (
                            "LOG",
                            f"  {year}: No data returned (skipped or not available).",
                        )
                    )

            if output_format == "excel":
                saved_excel = save_excel_workbook(
                    folder_name=entity_folder,
                    entity_name=entity_name,
                    table_code=table_code,
                    excel_accumulator=excel_accumulator,
                )
                if saved_excel:
                    self.msg_queue.put(
                        (
                            "LOG",
                            f"  Excel workbook saved: {os.path.basename(saved_excel)}",
                        )
                    )

        summary_msg = (
            f"Download completed successfully.\n"
            f"Total API Calls: {total_calls}\n"
            f"Saved to: {base_folder}"
        )
        self.msg_queue.put(("DONE", summary_msg))


def main() -> None:
    """Application main entry point."""
    root = tk.Tk()
    app = ACSDownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
