import pandas as pd, os, re, collections
p = os.path.expanduser("~/mnt/Downloads/Outbound_Leads.xlsx")
xl = pd.read_excel(p, sheet_name=None, dtype=object)
frames=[]
for name, df in xl.items():
    d=df.copy(); d.columns=[c.replace("S.No.","S.No") for c in d.columns]
    d["__sheet"]=name; frames.append(d)
all_ = pd.concat(frames, ignore_index=True)
print("UNIFIED rows:", len(all_), "cols:", list(all_.columns))
S=lambda c: all_[c].dropna().astype(str).str.strip()

# ---------- EMAIL ----------
em = S("Email").str.lower()
rx = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")
bad = em[~em.map(lambda x: bool(rx.match(x)))]
print("\nEMAIL: total",len(em),"invalid_syntax",len(bad)); print(" samples:", list(bad.head(8)))
dom = em.str.split("@").str[-1]
print(" top domains:", dom.value_counts().head(12).to_dict())
free = {"gmail.com","yahoo.com","hotmail.com","outlook.com","rediffmail.com","yahoo.in","yahoo.co.in","icloud.com","ymail.com","live.com","aol.com","protonmail.com"}
print(" freemail share: %.1f%%" % (dom.isin(free).mean()*100))
print(" role-based (info@/contact@/admin@ etc): %.1f%%" % (em.str.split("@").str[0].isin(["info","contact","admin","support","sales","hello","hi","enquiry","enquiries","office","care","help","team"]).mean()*100))
dup_em = em.value_counts(); print(" duplicate emails: uniq",em.nunique(),"| rows with dup", int((dup_em[dup_em>1]).sum()), "| dup groups", int((dup_em>1).sum()))
print(" top dup emails:", dup_em[dup_em>1].head(6).to_dict())

# ---------- PHONE ----------
ph = S("Phone")
digits = ph.str.replace(r"\D","",regex=True)
print("\nPHONE: uniq",ph.nunique(),"| len distribution", digits.str.len().value_counts().head(6).to_dict())
print(" non +91 prefix:", int((~ph.str.startswith("+91")).sum()))
dp = ph.value_counts(); print(" dup phone groups", int((dp>1).sum()), "rows involved", int(dp[dp>1].sum()))
print(" top dup phones:", dp[dp>1].head(5).to_dict())

# ---------- WEBSITE ----------
ws = S("Website")
print("\nWEBSITE: present",len(ws),"of",len(all_))
def host(u):
    m=re.match(r"https?://([^/]+)",u.strip(),re.I)
    return m.group(1).lower().replace("www.","") if m else None
h = ws.map(host)
print(" unparseable:", int(h.isna().sum()), list(ws[h.isna()].head(5)))
hv=h.dropna()
print(" top hosts:", hv.value_counts().head(15).to_dict())
noise = hv[hv.str.contains("wa.me|facebook|instagram|linktr|bit.ly|youtube|linkedin|t.me|forms.gle|docs.google|api.whatsapp|shorturl|zcal|calendly", regex=True)]
print(" non-owned/redirect hosts count:", len(noise), "unique:", noise.nunique())
print(" of which wa.me/whatsapp:", int(hv.str.contains("wa.me|whatsapp").sum()))
dh=hv.value_counts(); print(" duplicate website hosts groups", int((dh>1).sum()), "rows", int(dh[dh>1].sum()))

# ---------- FACEBOOK ----------
fb = S("Facebook")
print("\nFACEBOOK: uniq", fb.nunique(), "| numeric-id pages:", int(fb.str.contains(r"facebook\.com/\d{8,}$").sum()))
dfb=fb.value_counts(); print(" dup fb groups", int((dfb>1).sum()))

# ---------- FOLLOWERS ----------
fo = all_["Followers"].dropna().astype(str).str.strip()
def parse(v):
    v=v.replace(",","").upper()
    try:
        if v.endswith("K"): return float(v[:-1])*1e3
        if v.endswith("M"): return float(v[:-1])*1e6
        return float(v)
    except: return None
pv = fo.map(parse)
print("\nFOLLOWERS: present",len(fo),"unparseable",int(pv.isna().sum()), list(fo[pv.isna()].head(5)))
q = pv.dropna()
print(" min",q.min(),"p25",q.quantile(.25),"median",q.median(),"p75",q.quantile(.75),"p95",q.quantile(.95),"max",q.max())
print(" <100 followers: %.1f%%  | <10: %.1f%%" % ((q<100).mean()*100,(q<10).mean()*100))

# ---------- CITY ----------
ci = S("City")
print("\nCITY: present",len(ci),"uniq",ci.nunique())
print(" top:", ci.value_counts().head(15).to_dict())
print(" singletons (appear once):", int((ci.value_counts()==1).sum()))

# ---------- NAME ----------
nm = S("Name")
print("\nNAME: uniq", nm.nunique(), "of", len(nm))
print(" placeholder 'Advertiser NNNN':", int(nm.str.match(r"(?i)advertiser\s*\d+").sum()))
dn = nm.str.lower().str.replace(r"[^a-z0-9]","",regex=True).value_counts()
print(" normalized-name dup groups:", int((dn>1).sum()), "rows", int(dn[dn>1].sum()))

# ---------- CATEGORY / NICHE ----------
print("\nFB_Category uniq:", all_["FB_Category"].dropna().nunique(), "| Niche uniq:", all_["Niche"].dropna().nunique())
print(" Niche values (Day_1):", xl["Day_1"]["Niche"].value_counts().to_dict())
print(" Relevance (Day_1):", xl["Day_1"]["Relevance"].value_counts().to_dict())
print(" FB_Category top:", all_["FB_Category"].dropna().value_counts().head(15).to_dict())

# ---------- CROSS-SHEET OVERLAP ----------
print("\nCROSS-SHEET overlap (by email):")
sets={n:set(df["Email"].dropna().astype(str).str.lower().str.strip()) for n,df in xl.items()}
ks=list(sets)
for i in range(len(ks)):
    for j in range(i+1,len(ks)):
        print(f"  {ks[i]} ∩ {ks[j]} = {len(sets[ks[i]]&sets[ks[j]])}")
# multi-signal duplicates
print("\nMULTI-SIGNAL duplicate rows (any of email/phone/fb/host repeated):")
key = pd.DataFrame({"e":all_["Email"].astype(str).str.lower().str.strip(),
                    "p":all_["Phone"].astype(str).str.replace(r"\D","",regex=True),
                    "f":all_["Facebook"].astype(str).str.strip(),
                    "h":all_["Website"].astype(str).map(lambda x: host(x) if isinstance(x,str) else None)})
for c in key.columns:
    vc=key[c].value_counts()
    print(f"  {c}: rows in dup groups = {int(vc[vc>1].sum())}")
