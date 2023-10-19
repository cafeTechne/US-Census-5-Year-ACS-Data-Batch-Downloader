# 📊 ACS 5-Year Place Census Data Fetcher 📊

Easily fetch and process census data with this Python script.

## 🌟 Features:
1. 🌍 Fetches batches of data from the US Census Bureau from 2010-2019.
2. 🎨 Aesthetically pleasing GUI with color schemes.
3. 📈 Efficiently processes and melts data for easier use and creates an aggregate csv file that can be joined into ArcGIS, SPSS, Excel, etc.
4. 💾 Saves data locally in CSV format.


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
