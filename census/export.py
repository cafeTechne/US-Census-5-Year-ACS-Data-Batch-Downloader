"""Data transformation, formatting, and file export utilities for Census datasets."""

import os
import re
from typing import Dict, List, Optional
import openpyxl  # Ensure dependency presence for Excel export
import pandas as pd
from census.tables import get_variable_descriptions


def format_dataframe(
    df: pd.DataFrame,
    table_code: str,
    variable_descriptions: Optional[Dict[str, str]] = None,
) -> pd.DataFrame:
    """Rename raw Census variable codes to their human-readable labels."""
    descriptions = (
        variable_descriptions
        if variable_descriptions is not None
        else get_variable_descriptions(table_code)
    )
    df.rename(columns=descriptions, inplace=True)
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
    """Replace non-alphanumeric filesystem characters and punctuation with underscores."""
    return re.sub(r'[,\\/*?:"<>| ]', "_", name).strip("_")


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
