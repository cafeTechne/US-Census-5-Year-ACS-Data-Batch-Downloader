# 📊 ACS 5-Year Place Census Data Fetcher 📊

Easily fetch and process census data with this Python script.

## 🌟 Features:
1. 🌍 Fetches data from the US Census Bureau.
2. 🎨 Aesthetically pleasing GUI with color schemes.
3. 📈 Efficiently processes and melts data for easier use.
4. 💾 Saves data locally in CSV format.

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
