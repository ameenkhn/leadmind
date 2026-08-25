import pandas as pd, os
p = os.path.expanduser("~/mnt/Downloads/Outbound_Leads.xlsx")
xl = pd.read_excel(p, sheet_name=None, dtype=object)
tot=0
for name, df in xl.items():
    tot+=len(df)
    print("="*70)
    print(f"SHEET {name!r}  rows={len(df)}  cols={len(df.columns)}")
    print("COLUMNS:")
    for c in df.columns:
        nn = df[c].notna().sum()
        print(f"   - {c!r:38} nonnull={nn:5} ({nn/max(len(df),1)*100:5.1f}%)")
print("TOTAL ROWS:", tot)
