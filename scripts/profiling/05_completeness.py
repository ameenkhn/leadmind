import pandas as pd, os, re, difflib
p = os.path.expanduser("~/mnt/Downloads/Outbound_Leads.xlsx")
xl = pd.read_excel(p, sheet_name=None, dtype=object)
fr=[]
for n,df in xl.items():
    d=df.copy(); d.columns=[c.replace("S.No.","S.No") for c in d.columns]; d["__sheet"]=n; fr.append(d)
a=pd.concat(fr,ignore_index=True)
em=a["Email"].astype(str).str.lower().str.strip(); dom=em.str.split("@").str[-1]
common=["gmail.com","yahoo.com","hotmail.com","outlook.com","rediffmail.com"]
typos={}
for d,c in dom.value_counts().items():
    if d in common: continue
    m=difflib.get_close_matches(d,common,n=1,cutoff=0.85)
    if m: typos[d]=(c,m[0])
print("TYPO/lookalike freemail domains:",typos)

def parse(v):
    if pd.isna(v): return None
    v=str(v).replace(",","").strip().upper()
    try:
        if v.endswith("K"): return float(v[:-1])*1e3
        if v.endswith("M"): return float(v[:-1])*1e6
        return float(v)
    except: return None
f=a["Followers"].map(parse)
print("followers ==0:",int((f==0).sum()),"| missing:",int(f.isna().sum()))

has=lambda c: a[c].notna()
def own(u):
    if not isinstance(u,str): return False
    return not re.search(r"wa\.me|linktr\.ee|threads\.com|t\.me|share\.google|youtu\.be|instagram\.com|facebook\.com|linkedin\.com|bit\.ly|forms\.gle|docs\.google",u,re.I)
owned=a["Website"].map(own)
print("owned website rows:",int(owned.sum()),"of",len(a))
thin = (~owned) & (~has("LinkedIn")) & (f.fillna(0)<100)
print("THIN leads (no owned site, no linkedin, <100 followers):",int(thin.sum()))
fields=["Name","Email","Phone","Website","Facebook","Instagram","YouTube","LinkedIn","FB_Category","City","Followers"]
comp=a[fields].notna().sum(axis=1)
print("completeness (n fields of 11) distribution:", comp.value_counts().sort_index().to_dict())
print("mean completeness: %.2f"%comp.mean())
fbnum=a["Facebook"].astype(str).str.contains(r"facebook\.com/\d{8,}$")
print("FB numeric-id (no vanity handle) rows:",int(fbnum.sum()))
print("rows with owned website AND linkedin:",int((owned&has("LinkedIn")).sum()))
print("rows scrapable for RAG (owned website):",int(owned.sum()))
# name looks personal vs business
biz=a["Name"].astype(str).str.contains(r"(?i)\b(pvt|ltd|llp|inc|academy|institute|clinic|hospital|centre|center|solutions|services|consult|technolog|india|studio|labs|group|foundation|school|college|university|wellness|care)\b")
print("business-ish names: %.1f%%"%(biz.mean()*100))
