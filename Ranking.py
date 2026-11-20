import pandas as pd
import numpy as np

INPUT_FILE = r"C:\Users\Admin\Desktop\pp\consolidation_report.xlsx"
OUTPUT_FILE = r"C:\Users\Admin\Desktop\pp\consolidation_report_ranked.xlsx"

excel = pd.ExcelFile(INPUT_FILE)

summary = pd.read_excel(excel, sheet_name="Summary")

# Fill blanks
summary["Total Number of Consolidation Weeks"] = (
    summary["Total Number of Consolidation Weeks"]
    .fillna(0)
)

summary["Still in Consolidation (YES/NO)"] = (
    summary["Still in Consolidation (YES/NO)"]
    .astype(str)
    .str.upper()
)

# Ranking
summary["Priority Rank"] = np.select(
    [
        summary["Still in Consolidation (YES/NO)"] == "YES",

        (summary["Still in Consolidation (YES/NO)"] == "NO") &
        (summary["Total Number of Consolidation Weeks"] > 0),

        (summary["Still in Consolidation (YES/NO)"] == "NO") &
        (summary["Total Number of Consolidation Weeks"] == 0),
    ],
    [1, 2, 3],
    default=3,
)

summary["Priority Reason"] = np.select(
    [
        summary["Priority Rank"] == 1,
        summary["Priority Rank"] == 2,
        summary["Priority Rank"] == 3,
    ],
    [
        "Currently Consolidating",
        "Historical Consolidation Found",
        "Never Consolidated",
    ],
    default=""
)

# Sort
summary = summary.sort_values(
    ["Priority Rank", "Total Number of Consolidation Weeks"],
    ascending=[True, False]
)

# Save all sheets
with pd.ExcelWriter(OUTPUT_FILE, engine="openpyxl") as writer:

    summary.to_excel(writer, sheet_name="Summary", index=False)

    for sheet in excel.sheet_names:
        if sheet == "Summary":
            continue

        df = pd.read_excel(excel, sheet_name=sheet)
        df.to_excel(writer, sheet_name=sheet, index=False)

print(f"\nDone! Ranked report saved to:\n{OUTPUT_FILE}")