# SPDX-License-Identifier: AGPL-3.0-or-later
"""US Census 5-Year ACS Data Batch Downloader.

A desktop application to fetch, format, and export American Community Survey (ACS)
5-year dataset tables across multiple years and geography levels (nation, state,
county, tract, place, ZCTA, CBSA), with research options: MOE-derived standard
errors and CVs, Census sentinel cleaning, CPI-U constant dollars, tidy panels,
Parquet, and codebooks.

Maintenance mode: no new features. The GUI runs the same pipeline as the
library, MCP server and CLI, so it inherits their defaults (concordance_mode
"fail", R-CPI-U-RS) and reports blocked runs in its log and error dialog.
"""

from datetime import datetime
import os
import queue
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
import pandas as pd
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from census import (
    CPI_U_ANNUAL,
    DEFAULT_PRICE_INDEX,
    archived_vintages,
    load_price_index,
    DATASET_TABLES,
    CensusAPIClient,
    FIPS_MAPPING,
    ResearchOptions,
    build_entities,
    counties_for_state,
    get_default_cache,
    run_batch,
    zctas_for_state,
    STATE_FILES,
    STATE_MAPPING,
    enhanced_melt,
    extract_state_abbr_from_filename,
    fetch_census_data,
    format_dataframe,
    get_local_places_file,
    get_resource_path,
    get_variable_descriptions,
    handle_duplicate_columns,
    load_places_from_local,
    default_metadata_file,
    load_table_catalog,
    load_tables_from_local,
    load_variable_definitions,
    VARIABLE_DEFINITIONS,
    process_data_and_save,
    sanitize_filename,
    save_excel_workbook,
)
from census.pipeline import ZCTA_CHUNK_SIZE

load_dotenv()

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

GEO_LEVEL_OPTIONS: List[Tuple[str, str]] = [
    ("place", "Places (Cities/CDPs)"),
    ("state", "Entire State"),
    ("county", "All Counties in State"),
    ("tract", "Tracts in County"),
    ("zcta", "ZCTA (ZIP)"),
    ("msa", "Metro/Micro Areas (CBSA)"),
    ("us", "United States"),
]

# Levels that use the multi-select entity list, and its heading per level
LIST_LEVEL_TITLES: Dict[str, str] = {
    "place": "3. Select Places (Multi-select):",
    "tract": "3. Select Counties (all tracts in each):",
    "zcta": "3. Select ZCTAs / ZIP Codes (Multi-select):",
}

# Levels that are national and do not need a state
NATIONAL_LEVELS = ("msa", "us")

DATASET_OPTIONS: List[Tuple[str, str]] = [
    ("acs5", "Detailed Tables (B/C)"),
    ("profile", "Data Profiles (DP02-DP05)"),
    ("subject", "Subject Tables (S)"),
    ("cprofile", "Comparison Profiles (CP)"),
]

FORMAT_OPTIONS: List[Tuple[str, str]] = [
    ("Both Wide & Melted CSVs", "both"),
    ("Melted CSV only", "melted_only"),
    ("Wide CSV only", "wide_only"),
    ("Excel Workbook (.xlsx)", "excel"),
    ("Tidy Panel CSV (GEOID x Year)", "panel"),
    ("Parquet (.parquet, tidy panel)", "parquet"),
]

_VINTAGES = archived_vintages("acs5") or [2009, 2024]
MIN_YEAR = min(_VINTAGES)
MAX_YEAR = max(_VINTAGES)
# Default constant-dollar index (R-CPI-U-RS, the Census Bureau standard); the
# spinbox range falls back to CPI-U years until R-CPI-U-RS data is installed.
_PRICE_YEARS = sorted(load_price_index(DEFAULT_PRICE_INDEX) or CPI_U_ANNUAL)


class ACSDownloaderApp:
    """Tkinter-based GUI for Census ACS batch data downloading."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("ACS 5-Year Data Batch Downloader")
        self.root.geometry("1100x900")
        self.root.minsize(980, 780)
        self.root.config(bg=BACKGROUND_COLOR)

        self.msg_queue: queue.Queue[Tuple[str, Any]] = queue.Queue()
        self.is_downloading = False

        # Data caches
        self.all_tables: Dict[str, str] = {}
        # Multi-select entity list items: (code, display label)
        self.current_items: List[Tuple[str, str]] = []
        self.displayed_items: List[Tuple[str, str]] = []
        self.selected_state_code: Optional[str] = None
        self.county_names: Dict[str, Dict[str, str]] = {}  # state -> {county: name}

        self._load_initial_data()
        self._configure_styles()
        self._build_ui()

        # Start periodic queue polling
        self.root.after(50, self._poll_queue)

    def _load_initial_data(self) -> None:
        """Load detailed-table definitions from the latest archived vintage."""
        try:
            self.all_tables = load_table_catalog("acs5")
        except Exception as e:
            print(f"Error loading table catalog: {e}")

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
        style.configure(
            "TCheckbutton",
            background=BACKGROUND_COLOR,
            foreground=FOREGROUND_COLOR,
            font=("Segoe UI", 9),
        )
        style.map("TCheckbutton", background=[("active", BACKGROUND_COLOR)])
        style.map("TRadiobutton", background=[("active", BACKGROUND_COLOR)])

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
            text="Research Edition",
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
        for idx, (level, text) in enumerate(GEO_LEVEL_OPTIONS):
            rb = ttk.Radiobutton(
                geo_button_frame,
                text=text,
                value=level,
                variable=self.geo_level_var,
                command=self._on_geo_level_changed,
            )
            rb.grid(row=idx // 3, column=idx % 3, sticky="w", padx=(0, 10))

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

        self.list_title_label = tk.Label(
            place_header,
            text=LIST_LEVEL_TITLES["place"],
            font=("Segoe UI", 10, "bold"),
            bg=BACKGROUND_COLOR,
            fg=GREEN,
        )
        self.list_title_label.pack(side=tk.LEFT)

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
            from_=MIN_YEAR,
            to=MAX_YEAR,
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
            from_=MIN_YEAR,
            to=MAX_YEAR,
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
            values=[label for label, _ in FORMAT_OPTIONS],
            state="readonly",
            width=38,
        )
        self.format_combo.current(0)
        self.format_combo.pack(fill=tk.X, pady=(0, 6))

        # 6. Research Options
        tk.Label(
            parent,
            text="6. Research Options:",
            font=("Segoe UI", 10, "bold"),
            bg=BACKGROUND_COLOR,
            fg=PURPLE,
        ).pack(anchor="w", pady=(2, 2))

        options_frame = tk.Frame(parent, bg=BACKGROUND_COLOR)
        options_frame.pack(fill=tk.X, pady=(0, 4))

        self.reliability_var = tk.BooleanVar(value=False)
        self.sentinel_var = tk.BooleanVar(value=True)
        self.codebook_var = tk.BooleanVar(value=True)
        self.cache_var = tk.BooleanVar(value=True)
        self.inflation_var = tk.BooleanVar(value=False)

        checks = [
            (self.reliability_var, "Calculate Standard Errors & CVs"),
            (self.sentinel_var, "Clean Census sentinel codes (-666666666 -> blank)"),
            (self.codebook_var, "Include Codebook (codebook.csv)"),
            (self.cache_var, "Use local query cache (.cache/census_cache.db)"),
        ]
        for idx, (var, text) in enumerate(checks):
            ttk.Checkbutton(options_frame, text=text, variable=var).grid(
                row=idx, column=0, columnspan=2, sticky="w"
            )

        ttk.Checkbutton(
            options_frame,
            text="Adjust Financial Values to Constant Dollars (R-CPI-U-RS), base year:",
            variable=self.inflation_var,
        ).grid(row=len(checks), column=0, sticky="w")
        self.inflation_year_spin = tk.Spinbox(
            options_frame,
            from_=min(_PRICE_YEARS),
            to=max(_PRICE_YEARS),
            width=6,
            bg=SELECTION_COLOR,
            fg=FOREGROUND_COLOR,
            buttonbackground=SELECTION_COLOR,
            highlightthickness=0,
        )
        self.inflation_year_spin.delete(0, tk.END)
        self.inflation_year_spin.insert(0, str(max(_PRICE_YEARS)))
        self.inflation_year_spin.grid(row=len(checks), column=1, sticky="w", padx=(4, 0))

        # 7. Call Estimation Banner
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
        # 0. Dataset / endpoint selector
        tk.Label(
            parent,
            text="Census Endpoint:",
            font=("Segoe UI", 10, "bold"),
            bg=BACKGROUND_COLOR,
            fg=ORANGE,
        ).pack(anchor="w", pady=(0, 4))
        self.dataset_combo = ttk.Combobox(
            parent,
            values=[label for _, label in DATASET_OPTIONS],
            state="readonly",
            width=38,
        )
        self.dataset_combo.current(0)
        self.dataset_combo.pack(fill=tk.X, pady=(0, 6))
        self.dataset_combo.bind("<<ComboboxSelected>>", self._on_dataset_changed)

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

        self._populate_table_listbox(sorted(self._dataset_tables().keys()))

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

    def _selected_dataset(self) -> str:
        label = self.dataset_combo.get()
        for key, text in DATASET_OPTIONS:
            if text == label:
                return key
        return "acs5"

    def _dataset_tables(self) -> Dict[str, str]:
        """Tables available for the selected Census endpoint."""
        dataset = self._selected_dataset() if hasattr(self, "dataset_combo") else "acs5"
        if dataset == "acs5":
            return self.all_tables
        return DATASET_TABLES.get(dataset, {})

    def _on_dataset_changed(self, event: Optional[tk.Event] = None) -> None:
        self.table_search_entry.delete(0, tk.END)
        self._populate_table_listbox(sorted(self._dataset_tables().keys()))
        self.description_text.delete(1.0, tk.END)

    def _on_geo_level_changed(self) -> None:
        """Handle geography level radiobutton selection."""
        level = self.geo_level_var.get()
        if level in LIST_LEVEL_TITLES:
            self.list_title_label.config(text=LIST_LEVEL_TITLES[level])
            self.place_section_frame.pack(
                fill=tk.BOTH, expand=True, pady=(4, 8), after=self.state_combo
            )
            self._reload_items()
        else:
            self.place_section_frame.pack_forget()
        self._update_call_estimate()

    def _parse_state_code(self) -> Optional[str]:
        selection = self.state_combo.get().strip()
        for abbr in STATE_MAPPING:
            if f"({abbr})" in selection:
                return abbr
        return None

    def _on_state_selected(self, event: Optional[tk.Event] = None) -> None:
        """Reload the entity list when a new state is chosen."""
        self.selected_state_code = self._parse_state_code()
        self._reload_items()

    def _reload_items(self) -> None:
        """Populate the multi-select list for the current level and state."""
        level = self.geo_level_var.get()
        state = self.selected_state_code
        items: List[Tuple[str, str]] = []

        if state and level == "place":
            places_file = get_local_places_file(state)
            if places_file and os.path.exists(places_file):
                items = [(p[1], p[2]) for p in load_places_from_local(places_file)]
        elif state and level == "tract":
            names = self.county_names.get(state)
            if names:
                items = sorted(names.items(), key=lambda kv: kv[1])
            else:
                items = [(c, f"County {state}{c}") for c in counties_for_state(state)]
                self._load_county_names_async(state)
        elif state and level == "zcta":
            items = [(z, f"ZCTA {z}") for z in zctas_for_state(state)]

        self.current_items = items
        self._on_place_filter_changed()

    def _load_county_names_async(self, state_code: str) -> None:
        """Fetch county names from the Census API without blocking the UI."""

        # Read Tk variables on the UI thread only
        year = min(self._get_year_range()[1], MAX_YEAR)
        use_cache = self.cache_var.get()

        def worker() -> None:
            client = CensusAPIClient(cache=get_default_cache() if use_cache else None)
            rows = client.list_geographies(year, "county", state_code=state_code)
            self.msg_queue.put(("COUNTIES", (state_code, rows)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_counties_loaded(self, state_code: str, rows: List[Dict[str, str]]) -> None:
        if not rows:
            self._append_log(
                f"Could not load county names for state {state_code}; showing FIPS codes."
            )
            return
        self.county_names[state_code] = {
            r.get("county", ""): r.get("NAME", "").split(",")[0] for r in rows
        }
        if self.geo_level_var.get() == "tract" and self.selected_state_code == state_code:
            self._reload_items()

    def _on_place_filter_changed(self, event: Optional[tk.Event] = None) -> None:
        """Filter the entity list based on the search entry text."""
        query = self.place_filter_entry.get().strip().lower()
        self.place_listbox.delete(0, tk.END)

        if not query:
            self.displayed_items = list(self.current_items)
        else:
            self.displayed_items = [
                item
                for item in self.current_items
                if query in item[1].lower() or query in item[0].lower()
            ]

        for _, label in self.displayed_items:
            self.place_listbox.insert(tk.END, label)

        self._update_place_count()
        self._update_call_estimate()

    def _select_all_places(self) -> None:
        """Select all visible items in the entity listbox."""
        self.place_listbox.select_set(0, tk.END)
        self._update_place_count()
        self._update_call_estimate()

    def _clear_place_selection(self) -> None:
        """Deselect all items in the entity listbox."""
        self.place_listbox.selection_clear(0, tk.END)
        self._update_place_count()
        self._update_call_estimate()

    def _on_place_selection_changed(self, event: Optional[tk.Event] = None) -> None:
        """Update count label and estimate when selection changes."""
        self._update_place_count()
        self._update_call_estimate()

    def _update_place_count(self) -> None:
        """Update the selected items counter."""
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

    def _selected_codes(self) -> List[str]:
        return [self.displayed_items[i][0] for i in self.place_listbox.curselection()]

    def _count_requests(self) -> int:
        """Number of API requests per year for the current selection."""
        level = self.geo_level_var.get()
        has_state = bool(self.state_combo.get().strip())
        selected = len(self.place_listbox.curselection())
        if level in ("place", "tract"):
            return selected
        if level == "zcta":
            return -(-selected // ZCTA_CHUNK_SIZE)  # ceil
        if level in NATIONAL_LEVELS:
            return 1
        return 1 if has_state else 0

    def _update_call_estimate(self) -> None:
        """Recalculate and display the estimated number of API calls."""
        start_yr, end_yr = self._get_year_range()
        year_count = max(0, end_yr - start_yr + 1)
        entity_count = self._count_requests()

        total_calls = entity_count * year_count
        color = GREEN if total_calls <= 50 else (ORANGE if total_calls <= 150 else RED)

        self.estimate_label.config(
            text=f"Estimated API calls: {total_calls} ({entity_count} requests x {year_count} years)",
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
        tables = self._dataset_tables()
        if not query:
            filtered = sorted(tables.keys())
        else:
            filtered = [
                code
                for code, desc in sorted(tables.items())
                if query in code.lower() or query in desc.lower()
            ]
        self._populate_table_listbox(filtered)

    def _on_table_selected(self, event: Optional[tk.Event] = None) -> None:
        """Display table description and lazy-loaded variable details."""
        selection = self.table_listbox.curselection()
        if not selection:
            return

        table_code = self.table_listbox.get(selection[0])
        description = self._dataset_tables().get(
            table_code, "No description available."
        )
        if self._selected_dataset() != "acs5":
            self.description_text.delete(1.0, tk.END)
            self.description_text.insert(
                tk.END,
                f"Table: {table_code}\nTitle: {description}\n"
                f"Endpoint: /acs/acs5/{self._selected_dataset()}\n" + "-" * 50 + "\n"
                "Variable labels are fetched from the Census API at download time "
                "and written to the codebook.\n"
                "Percent columns end in PE (estimate) and PM (margin of error).",
            )
            return

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
                elif msg_type == "COUNTIES":
                    self._on_counties_loaded(*payload)
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

    def _collect_options(self) -> Optional[ResearchOptions]:
        """Read research toggles; None (after an error dialog) if invalid."""
        base_year: Optional[int] = None
        if self.inflation_var.get():
            try:
                base_year = int(self.inflation_year_spin.get())
            except ValueError:
                messagebox.showerror("Error", "Base year must be an integer.")
                return None
            if not load_price_index(DEFAULT_PRICE_INDEX):
                messagebox.showerror(
                    "Error",
                    "R-CPI-U-RS data is not installed. Run scripts/update_price_indexes.py "
                    "(see its instructions) to enable constant-dollar conversion.",
                )
                return None
        return ResearchOptions(
            clean_sentinels=self.sentinel_var.get(),
            compute_reliability=self.reliability_var.get(),
            inflation_base_year=base_year,
            include_codebook=self.codebook_var.get(),
            use_cache=self.cache_var.get(),
        )

    def on_submit(self) -> None:
        """Validate input and trigger background worker thread."""
        if self.is_downloading:
            return

        table_selection = self.table_listbox.curselection()
        if not table_selection:
            messagebox.showerror("Error", "Please select a table.")
            return
        table_code = self.table_listbox.get(table_selection[0])
        dataset = self._selected_dataset()

        geo_level = self.geo_level_var.get()
        state_code = self._parse_state_code()
        if not state_code and geo_level not in NATIONAL_LEVELS:
            messagebox.showerror("Error", "Please select a valid state.")
            return
        if geo_level in NATIONAL_LEVELS:
            state_code = None

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

        options = self._collect_options()
        if options is None:
            return

        codes: Optional[List[str]] = None
        names: Dict[str, str] = {}
        if geo_level in LIST_LEVEL_TITLES:
            codes = self._selected_codes()
            if not codes:
                messagebox.showerror(
                    "Error", "Please select at least one item from the list."
                )
                return
            names = dict(self.displayed_items)
            if geo_level == "tract":
                names = {
                    code: self.county_names.get(state_code or "", {}).get(code, label)
                    for code, label in names.items()
                }

        try:
            entities = build_entities(geo_level, state_code, codes, names)
        except ValueError as exc:
            messagebox.showerror("Error", str(exc))
            return

        years = list(range(year_start, year_end + 1))
        total_calls = len(entities) * len(years)
        if total_calls > 50:
            confirm = messagebox.askyesno(
                "Large Batch Warning",
                f"This operation will make up to {total_calls} API calls to the Census "
                f"Bureau.\nRequests: {len(entities)}, Years: {len(years)}.\n"
                f"(Cached responses are not re-requested.)\n\n"
                f"Do you want to proceed?",
            )
            if not confirm:
                return

        folder_path = filedialog.askdirectory(title="Select Destination Folder")
        if not folder_path:
            return

        format_mapping = dict(FORMAT_OPTIONS)
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
                table_code,
                dataset,
                entities,
                years,
                folder_path,
                output_format,
                options,
            ),
            daemon=True,
        ).start()

    def _download_worker(
        self,
        table_code: str,
        dataset: str,
        entities: List[Any],
        years: List[int],
        base_folder: str,
        output_format: str,
        options: ResearchOptions,
    ) -> None:
        """Worker thread to execute batch downloads without freezing UI."""
        def log(message: str) -> None:
            self.msg_queue.put(("LOG", message))

        def progress(index: int, total: int, entity_name: str, year: int) -> None:
            self.msg_queue.put(
                ("STATUS", f"Fetching [{index}/{total}]: {entity_name} ({year})...")
            )

        try:
            result = run_batch(
                table_code=table_code,
                entities=entities,
                years=years,
                output_dir=base_folder,
                output_format=output_format,
                options=options,
                dataset=dataset,
                log=log,
                progress=progress,
            )
        except Exception as exc:  # surface any failure in the UI, never hang
            self.msg_queue.put(("ERROR", f"{type(exc).__name__}: {exc}"))
            return

        if not result.get("success"):
            self.msg_queue.put(("ERROR", result.get("error", "Unknown error")))
            return

        lines = [
            "Download completed.",
            f"Successful API calls: {result['successful_calls']} / "
            f"{result['total_calls_attempted']}",
            f"Saved to: {base_folder}",
        ]
        if result.get("panel_path"):
            lines.append(f"Panel: {os.path.basename(result['panel_path'])}")
        if result.get("codebook_path"):
            lines.append(f"Codebook: {os.path.basename(result['codebook_path'])}")
        if result.get("advisories"):
            lines.append("")
            lines.append("See the log for boundary/comparability advisories.")
        self.msg_queue.put(("DONE", "\n".join(lines)))


def main() -> None:
    """Application main entry point."""
    root = tk.Tk()
    app = ACSDownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
