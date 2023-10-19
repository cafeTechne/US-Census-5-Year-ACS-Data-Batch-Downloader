import os
import sys
import json
import requests
import pandas as pd
import tkinter as tk
from tkinter import filedialog, messagebox,ttk



def get_resource_path(relative_path):
    """ Get the path to the resource in either development or in the packaged application """
    if getattr(sys, '_MEIPASS', False):
        # Running in a PyInstaller bundle
        base_path = sys._MEIPASS
    else:
        # Running in normal mode (e.g., from source)
        base_path = os.path.dirname(__file__)

    return os.path.join(base_path, relative_path)


if getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS
else:
    base_path = os.path.dirname(os.path.abspath(__file__))

current_directory = os.path.dirname(os.path.abspath(__file__))
json_file_path = get_resource_path("variables.json")

json_file = get_resource_path("variables.json")

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
API_KEY = "f803cbec090c9ae9aa7fe1390bedd70ac37af6fd"
VARIABLE_DEFINITIONS = {}
STATE_FILES = [
    "st01_al_place2020.txt", "st02_ak_place2020.txt", "st04_az_place2020.txt", "st05_ar_place2020.txt",
    "st06_ca_place2020.txt", "st08_co_place2020.txt", "st09_ct_place2020.txt", "st10_de_place2020.txt",
    "st11_dc_place2020.txt", "st12_fl_place2020.txt", "st13_ga_place2020.txt", "st15_hi_place2020.txt",
    "st16_id_place2020.txt", "st17_il_place2020.txt", "st18_in_place2020.txt", "st19_ia_place2020.txt",
    "st20_ks_place2020.txt", "st21_ky_place2020.txt", "st22_la_place2020.txt", "st23_me_place2020.txt",
    "st24_md_place2020.txt", "st25_ma_place2020.txt", "st26_mi_place2020.txt", "st27_mn_place2020.txt",
    "st28_ms_place2020.txt", "st29_mo_place2020.txt", "st30_mt_place2020.txt", "st31_ne_place2020.txt",
    "st32_nv_place2020.txt", "st33_nh_place2020.txt", "st34_nj_place2020.txt", "st35_nm_place2020.txt",
    "st36_ny_place2020.txt", "st37_nc_place2020.txt", "st38_nd_place2020.txt", "st39_oh_place2020.txt",
    "st40_ok_place2020.txt", "st41_or_place2020.txt", "st42_pa_place2020.txt", "st44_ri_place2020.txt",
    "st45_sc_place2020.txt", "st46_sd_place2020.txt", "st47_tn_place2020.txt", "st48_tx_place2020.txt",
    "st49_ut_place2020.txt", "st50_vt_place2020.txt", "st51_va_place2020.txt", "st53_wa_place2020.txt",
    "st54_wv_place2020.txt", "st55_wi_place2020.txt", "st56_wy_place2020.txt", "st60_as_place2020.txt",
    "st66_gu_place2020.txt", "st69_mp_place2020.txt", "st72_pr_place2020.txt", "st78_vi_place2020.txt"
]
STATE_MAPPING = {
    '01': 'Alabama', '02': 'Alaska', '04': 'Arizona', '05': 'Arkansas', '06': 'California',
    '08': 'Colorado', '09': 'Connecticut', '10': 'Delaware', '11': 'District of Columbia',
    '12': 'Florida', '13': 'Georgia', '15': 'Hawaii', '16': 'Idaho', '17': 'Illinois',
    '18': 'Indiana', '19': 'Iowa', '20': 'Kansas', '21': 'Kentucky', '22': 'Louisiana',
    '23': 'Maine', '24': 'Maryland', '25': 'Massachusetts', '26': 'Michigan', '27': 'Minnesota',
    '28': 'Mississippi', '29': 'Missouri', '30': 'Montana', '31': 'Nebraska', '32': 'Nevada',
    '33': 'New Hampshire', '34': 'New Jersey', '35': 'New Mexico', '36': 'New York',
    '37': 'North Carolina', '38': 'North Dakota', '39': 'Ohio', '40': 'Oklahoma',
    '41': 'Oregon', '42': 'Pennsylvania', '44': 'Rhode Island', '45': 'South Carolina',
    '46': 'South Dakota', '47': 'Tennessee', '48': 'Texas', '49': 'Utah', '50': 'Vermont',
    '51': 'Virginia', '53': 'Washington', '54': 'West Virginia', '55': 'Wisconsin',
    '56': 'Wyoming', '60': 'American Samoa', '66': 'Guam', '69': 'Northern Mariana Islands',
    '72': 'Puerto Rico', '74': 'U.S. Minor Outlying Islands', '78': 'U.S. Virgin Islands'
}
FIPS_MAPPING = {v: k for k, v in STATE_MAPPING.items()}



def get_local_places_file(state_abbr):
    matching_files = [file for file in STATE_FILES if f"st{state_abbr}" in file]
    print("Matching files:", matching_files)
    return get_resource_path(os.path.join("state_places", matching_files[0])) if matching_files else None

def get_variable_descriptions(table_code):
    if not isinstance(table_code, str):
        raise ValueError(f"Expected table_code to be a string but got {type(table_code)}")

    if VARIABLE_DEFINITIONS:
        return {variable: label for variable, label in VARIABLE_DEFINITIONS.items() if variable.startswith(table_code)}
    else:
        print("VARIABLE_DEFINITIONS not loaded correctly.")
        return {}


def load_variable_definitions():
    global VARIABLE_DEFINITIONS
    with open(json_file_path, 'r') as f:
        data = json.load(f)
        VARIABLE_DEFINITIONS = {var: details["label"] for var, details in data["variables"].items()}

# --------- Data Functions -------------
def load_places_from_local(path):
    with open(path, "r") as file:
        lines = file.readlines()
    lines = lines[1:]  # Skip the header

    places = []
    for line in lines:
        parts = line.strip().split("|")
        state, state_code, place_code, _, place_name, place_type, _, _, counties = parts
        places.append((state_code, place_code, place_name, place_type, counties))
    return places


def load_tables_from_local(path):
    with open(path, "r") as file:
        data = json.load(file)
    tables = {item['name']: item['description'] for item in data['groups']}
    for key, value in tables.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(f"Invalid structure for all_tables. Key: {key}, Value: {value}")
    return tables

def fetch_census_data(year, state_code, place_code, table_code):
    endpoint = f"{BASE_URL}/{year}/acs/acs5"
    params = {
        "get": f"NAME,group({table_code})",
        "for": f"place:{place_code}",
        "in": f"state:{state_code}",
        "key": API_KEY
    }

    try:
        response = requests.get(endpoint, params=params)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"Failed to fetch census data due to: {e}")
        return None


def format_dataframe(df, table_code):
    variable_descriptions = get_variable_descriptions(table_code)

    hybrid_names = {}
    for var, description in variable_descriptions.items():
        attributes = VARIABLE_DEFINITIONS[var]["attributes"].split(",") if "attributes" in VARIABLE_DEFINITIONS[var] else []
        if var in attributes:
            hybrid_names[var] = f"Margin of Error ({var}) for {description}"
        else:
            hybrid_names[var] = description

    df.rename(columns=hybrid_names, inplace=True)
    return df



def handle_duplicate_columns(df):
    cols = pd.Series(df.columns)
    for dup in cols[cols.duplicated()].unique():
        cols[cols[cols == dup].index.values.tolist()] = [dup + '_' + str(i) if i != 0 else dup for i in range(sum(cols == dup))]
    df.columns = cols

    return df


def enhanced_melt(df, id_vars, var_name, value_name):
    print("DataFrame:\n", df)
    print("Columns in DataFrame: ", df.columns)
    print("DataFrame Columns and Data:")
    print(df.head())  # This will print the top 5 rows. If you want more/less, adjust the number inside head().


    # Check if DataFrame is empty
    if df.empty:
        print("DataFrame is empty. Aborting the melt operation.")
        return pd.DataFrame()

    # Check if id_vars columns exist in the dataframe
    missing_columns = [col for col in id_vars if col not in df.columns]
    if missing_columns:
        print(f"Following columns are missing in the dataframe and are required for melt: {missing_columns}")
        return pd.DataFrame()

    return df.melt(id_vars=id_vars, var_name=var_name, value_name=value_name)


def process_data_and_save(formatted_df, folder_name, selected_place, table_code, year, descriptions, all_tables):
    formatted_df = handle_duplicate_columns(formatted_df)

    if "NAME" in formatted_df.columns and "GEO_ID" in formatted_df.columns:
        # Save the formatted DataFrame by appending to a single CSV file
        compiled_file_name = os.path.join(folder_name, f"compiled_unmelted_data.csv")

        # Check if the file already exists; if not, write headers
        write_headers = not os.path.exists(compiled_file_name)

        with open(compiled_file_name, 'a') as f:
            formatted_df.to_csv(f, header=write_headers, index=False)

        print(f"Formatted data appended to: {compiled_file_name}")

        print("Formatted DataFrame before melting:")
        print(formatted_df.head())

        melted_df = enhanced_melt(formatted_df, ["NAME", "GEO_ID"], "Variable", "Value")

        # Save the melted DataFrame
        melted_file_name = os.path.join(folder_name, f"{selected_place.replace(' ', '_')}_{table_code}_{year}.csv")
        melted_df.to_csv(melted_file_name, index=False)
        print(f"Melted data saved to: {melted_file_name}")
    else:
        print("Unexpected DataFrame structure or missing columns.")


# --------- UI Callbacks -------------
def search_tables(event):
    query = search_entry.get().lower()
    table_listbox.delete(0, tk.END)
    for table_code, table_description in sorted(all_tables.items(), key=lambda x: x[0]):
        if query in table_code.lower() or query in table_description.lower():
            table_listbox.insert(tk.END, table_code)

def update_table_description(event):
    current_selection = table_listbox.curselection()
    if current_selection:
        table_code = table_listbox.get(current_selection[0])
        description = all_tables.get(table_code, 'No description available.')
        description_text.delete(1.0, tk.END)
        description_text.insert(tk.END, description)



def on_submit():
    current_selection = table_listbox.curselection()
    if not current_selection:
        messagebox.showerror("Error", "Please select a table.")
        return

    selected_state_name = state_combo.get().strip()

    # Use a substring match to find the state code
    matched_states = [code for code, name in STATE_MAPPING.items() if name.lower() in selected_state_name.lower()]

    if not matched_states:
        messagebox.showerror("Error", f"{selected_state_name} not found in states mapping.")
        return

    # Take the first matched state code
    selected_state_code = matched_states[0]
    print("selected state code is " + selected_state_code)

    places_file = get_local_places_file(selected_state_code)
    if not places_file:
        messagebox.showerror("Error", f"No places file found for {selected_state_name}.")
        return

    selected_place = combo.get()
    places = load_places_from_local(places_file)

    table_code = table_listbox.get(current_selection[0])

    if not isinstance(table_code, str):
        messagebox.showerror("Error", "Invalid table code selected.")
        return

    load_variable_definitions()

    state_code, place_code = next((place[0], place[1]) for place in places if place[2] == selected_place)

    folder_path = filedialog.askdirectory(title="Select Directory to Save Data")
    if not folder_path:
        return
    folder_name = os.path.join(folder_path, f"{selected_place.replace(' ', '_')}_{table_code}")
    os.makedirs(folder_name, exist_ok=True)

    descriptions = get_variable_descriptions(table_code)

    for year in range(2010, 2020):
        data = fetch_census_data(year, state_code, place_code, table_code)
        if data:
            df = pd.DataFrame(data[1:], columns=data[0])  # data[0] is the header
            formatted_df = format_dataframe(df, table_code)
            print("Formatted DataFrame:")
            print(formatted_df.head())

            process_data_and_save(formatted_df, folder_name, selected_place, table_code, year, descriptions, all_tables)

# Define constants
LABEL_FONT = ("Arial", 14, "bold")
DESCRIPTION_FONT = ("Arial", 12)
BUTTON_FONT = ("Arial", 12, "bold")

def configure_styles():
    style = ttk.Style()
    style.theme_use('default')
    style.configure('TFrame', background=FOREGROUND_COLOR)
    style.configure('TLabel', background=BACKGROUND_COLOR, foreground=FOREGROUND_COLOR, font=LABEL_FONT)
    style.configure('TButton', background=FOREGROUND_COLOR, foreground=BACKGROUND_COLOR, font=BUTTON_FONT)
    style.configure('TCombobox', background=FOREGROUND_COLOR, selectbackground=SELECTION_COLOR,
                    fieldbackground=FOREGROUND_COLOR, bordercolor=COMMENT, font=LABEL_FONT)
    style.configure('TEntry', background=BACKGROUND_COLOR, fieldbackground=FOREGROUND_COLOR,
                    foreground=BACKGROUND_COLOR, insertcolor=FOREGROUND_COLOR, bordercolor=COMMENT, font=LABEL_FONT)

def extract_state_abbr_from_filename(filename):
    return filename[2:4]

def on_state_selected(event):
    selected_state_name_and_code = state_combo.get()
    selected_state = selected_state_name_and_code[-3:-1]

    places_file = get_local_places_file(selected_state)
    if not places_file:
        combo['values'] = []
        combo.set('')
        messagebox.showerror("Error", "No data found for the selected state.")
        return

    places = load_places_from_local(places_file)
    combo['values'] = [place[2] for place in places]
    combo.set('')

# Initialize main application window
app = tk.Tk()
app.title("ACS 5-Year Data Downloader")
app.geometry("800x550")
app.config(bg=BACKGROUND_COLOR)

# Set styles
configure_styles()


# Set main frame
main_frame = tk.Frame(app, bg=BACKGROUND_COLOR)
main_frame.pack(pady=20, padx=20, expand=True, fill=tk.BOTH)

# Add labels and dropdown for states
state_label = ttk.Label(main_frame, text="Select a State or U.S. Territory:", foreground=RED)
state_label.grid(row=0, column=0, pady=(0, 10), sticky="w")

state_abbreviations = [extract_state_abbr_from_filename(file) for file in STATE_FILES]
state_values = [f"{STATE_MAPPING[abbr]} ({abbr})" for abbr in state_abbreviations]
state_combo = ttk.Combobox(main_frame, values=state_values, width=40)
state_combo.grid(row=0, column=1, pady=(0, 20))
state_combo.bind("<<ComboboxSelected>>", on_state_selected)

# Add labels and dropdown for places
place_label = ttk.Label(main_frame, text="Select a Place:", foreground=YELLOW)
place_label.grid(row=1, column=0, pady=10, sticky="w")
combo = ttk.Combobox(main_frame, values=[], width=40)
combo.grid(row=1, column=1, pady=10)

# Add search field
search_label = ttk.Label(main_frame, text="Search Table:", foreground=GREEN)
search_label.grid(row=2, column=0, pady=10, sticky="w")
search_entry = ttk.Entry(main_frame, width=40)
search_entry.grid(row=2, column=1, pady=10, padx=(0, 20))
search_entry.bind("<KeyRelease>", search_tables)

# Listbox setup for table selection
table_label = ttk.Label(main_frame, text="Select a Table:", foreground=PINK)
table_label.grid(row=3, column=0, pady=10, sticky="w")
local_groups_file = get_resource_path("groups.json")
all_tables = load_tables_from_local(local_groups_file)
table_listbox = tk.Listbox(main_frame, bg=SELECTION_COLOR, fg=FOREGROUND_COLOR, selectbackground=COMMENT, width=40)
table_listbox.grid(row=3, column=1, pady=10, padx=(0, 20))
for table_code in sorted(all_tables.keys()):
    table_listbox.insert(tk.END, table_code)
table_listbox.bind("<<ListboxSelect>>", update_table_description)


# Description field setup
description_label = ttk.Label(main_frame, text="Table Description:", foreground=CYAN)
description_label.grid(row=4, column=0, pady=(20, 0), sticky="nw")
description_text = tk.Text(main_frame, height=6, width=40, font=DESCRIPTION_FONT)
description_text.grid(row=4, column=1, pady=20)

# Submit button setup
submit_button = ttk.Button(main_frame, text="Submit", command=on_submit)
submit_button.grid(row=5, column=0, columnspan=2, pady=20)

# Start main loop
app.mainloop()
