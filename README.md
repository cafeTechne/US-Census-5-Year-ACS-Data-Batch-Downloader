# 📊 US Census 5-Year ACS Data Batch Downloader 📊

Easily fetch and process census data with this Python script.

## 🌟 Features:
1. 🌍 Fetches batches of data from the US Census Bureau from 2010-2019.
2. 🎨 Aesthetically pleasing GUI with color schemes.
3. 📈 Efficiently processes and melts data for easier use and creates an aggregate csv file that can be joined into ArcGIS, SPSS, Excel, etc.
4. 💾 Saves data locally in CSV format.


## 🔬 Research-Grade Pipeline (Phase 5)

The `census/` library, the desktop GUI, and the MCP server share one pipeline (`census.pipeline.run_batch`) with these research features:

| Area | What you get |
| :--- | :--- |
| **Geographies** | Nation (`us`), state, county, **census tract** (11-digit GEOIDs), place, **ZCTA** (ZIP, resolvable via `zip2fips.json`), **metro/micro areas (CBSA)**. Every output carries a standard `GEOID` for joining TIGER/Line shapefiles. |
| **Endpoints** | Detailed tables (`B`/`C`), **Data Profiles** (`DP02`–`DP05`), **Subject tables** (`S1501`, `S1701`, `S1901`, `S2701`, …), **Comparison Profiles** (`CP02`–`CP05`). The endpoint is inferred from the table code. |
| **Sampling error** | Estimates stay paired with their 90% MOEs; optional `_SE` (MOE/1.645), `_CV` (%), and `_RELIABILITY` (High ≤15%, Moderate ≤30%, Low >30%) columns. `census.stats` adds Z-tests (with the overlapping-period adjustment) and root-sum-of-squares MOE aggregation. |
| **Sentinels** | Census jam values (`-666666666`, `-999999999`, `-222222222`, …) become blanks instead of fake negative incomes; optional `_NOTE` columns explain each one and flag top/bottom-coded medians. |
| **Constant dollars** | Dollar-valued variables (medians, means, aggregates, per-capita — not counts in `$` brackets) and their MOEs can be converted to a base year with the BLS CPI-U annual average (2000–2024 built in; pass `cpi_table` to extend). |
| **Comparability** | Requests spanning 2019→2020 log a boundary advisory (2010 vs. 2020 Census geography). Advisories are saved in `<table>_extraction_metadata.json` with every request parameter, for reproducibility. |
| **Outputs** | Wide/melted CSV and Excel as before, plus a **tidy panel** (unique `(geoid, year)` rows, snake_case column slugs) as CSV or **Parquet** (snappy, native dtypes), and `<table>_codebook.csv` (code, slug, measurement type, description, concept, universe, unit). |
| **Cache** | Responses are cached in `.cache/census_cache.db` so re-runs are offline and avoid rate limits (`CENSUS_CACHE_PATH` / `CENSUS_CACHE_DISABLED=1`). |

Headless example:

```python
from census import ResearchOptions, build_entities, run_batch

result = run_batch(
    table_code="B19013",                                   # median household income
    entities=build_entities("tract", "48", ["453"]),       # all tracts in Travis County, TX
    years=range(2015, 2023),
    output_dir="out",
    output_format="parquet",
    options=ResearchOptions(compute_reliability=True, inflation_base_year=2022, include_codebook=True),
)
print(result["panel_path"], result["advisories"])
```

Install the extra dependency for Parquet with `pip install -r requirements.txt` (adds `pyarrow`). See [docs/claude_setup.md](docs/claude_setup.md) for the MCP tools.


# 🐍 Setting Up Python and Using the Command Line 🖥️

If you're new to Python or haven't used the command line (terminal on macOS/Linux, Command Prompt or PowerShell on Windows) before, here's a quick guide to get you started:

## 🛠️ Installing Python:

1. **Windows/Mac**: 
    - Go to [Python's official website](https://www.python.org/downloads/).
    - Download the latest version for your operating system.
    - Run the downloaded installer. Ensure the "Add Python to PATH" option is checked and then complete the installation.

2. **Linux**: 
    - Most Linux distributions come with Python pre-installed. You can check by typing `python3 --version` in the terminal. If it's not installed:
      ```bash
      sudo apt-get update
      sudo apt-get install python3.8
      ```

## 🚀 Using the Command Line:

1. **Opening the Command Line**:
    - **Windows**: Press `Windows + R`, type `cmd`, and press Enter.
    - **Mac**: Press `Cmd + Space`, type `terminal`, and press Enter.
    - **Linux**: Usually `Ctrl + Alt + T` or search for `terminal` in the app menu.

2. **Navigating Directories**:
    - `cd [Directory Name]`: Change to a specific directory.
    - `cd ..`: Go up one directory.
    - `ls` (macOS/Linux) or `dir` (Windows): List all files in the current directory.

3. **Running Python Scripts**:
    - After navigating to the directory containing your Python script, use:
      ```bash
      python [script_name].py
      ```
    - If you encounter issues on some systems, try using `python3` instead of `python`.

4. **Installing Python Libraries**:
    - Use pip (Python's package installer) to install libraries. For example:
      ```bash
      pip install requests
      ```
    - If `pip` doesn't work, try using `pip3` instead.

## 📚 Additional Resources:

- [Python for Beginners](https://docs.python.org/3/tutorial/index.html)
- [Command Line Basics](https://tutorial.djangogirls.org/en/intro_to_command_line/)

---

Now you're ready to run the Census Data Fetcher script! Next, read the [How to Use](#-how-to-use) section for instructions.



## 🚀 How to Use:

1. **Clone the Repository**
```bash

git clone https://github.com/cafeTechne/acs5year_place_downloader

cd acs5year_place_downloader

pip install requests pandas tkinter

python acs_downloader.py

```
    Follow the on-screen instructions provided by the GUI to:
        Select the desired year
        Pick a state
        Choose a place
        Fetch the required table data

    🎉 The data will be processed and saved locally in a specified directory.

🔧 Configuration:

You can adjust the following configurations:

    Color Scheme: Modify the color constants at the top of the script (e.g., BACKGROUND_COLOR, FOREGROUND_COLOR, etc.) to fit your aesthetic needs.

    API Key: You might want to replace the existing API key (API_KEY constant) if needed. I left mine in for classmates in GPY324 & GPY209 to use until the end of the semester as the process of obtaining an API key would introduce an undue hardship on 
    my fellow time-constrained students.

    State Files and Mapping: The list STATE_FILES and dictionary STATE_MAPPING can be updated to add or modify the existing state files or mappings.

💡 Note: Please star if this is useful and add feature requests as bugs and I'll see what I can do to extend this app to work for your research.

🔐 License:
Why?
