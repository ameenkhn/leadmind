import pandas as pd, os
pd.set_option("display.width",200)
p = os.path.expanduser("~/mnt/Downloads/Outbound_Leads.xlsx")
xl = pd.read_excel(p, sheet_name=None, dtype=object)
for name, df in xl.items():
    print("#"*75); print("SHEET", name)
    for c in df.columns:
        s = df[c].dropna()
        types = s.map(lambda v: type(v).__name__).value_counts().to_dict()
        vals = s.astype(str).map(lambda x: x.strip())
        uniq = vals.nunique()
        samples = list(vals.drop_duplicates().head(4))
        maxlen = vals.map(len).max() if len(vals) else 0
        print(f"\n-- {c} | uniq={uniq} | types={types} | maxlen={maxlen}")
        for sm in samples: print("     ", sm[:150])
