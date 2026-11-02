"""US Census 5-Year ACS Data Batch Downloader.

A desktop application to fetch, format, and export American Community Survey (ACS)
5-year dataset tables across multiple years, geography levels (place, state, county),
and output formats (CSV, Excel).
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
    CensusAPIClient,
    FIPS_MAPPING,
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
    load_tables_from_local,
    load_variable_definitions,
    VARIABLE_DEFINITIONS,
    process_data_and_save,
    sanitize_filename,
    save_excel_workbook,
)

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
