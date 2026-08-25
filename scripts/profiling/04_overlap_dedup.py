import pandas as pd, os, re
p = os.path.expanduser("~/mnt/Downloads/Outbound_Leads.xlsx")
xl = pd.read_excel(p, sheet_name=None, dtype=object)
frames=[]
for name, df in xl.items():
    d=df.copy(); d.columns=[c.replace("S.No.","S.No") for c in d.columns]; d["__sheet"]=name; frames.append(d)
a=pd.concat(frames, ignore_index=True)

# Day_3 Niche vs Day_1 FB_Category semantics
d1c=set(xl["Day_1"]["FB_Category"].dropna().astype(str)); d3n=set(xl["Day_3"]["Niche"].dropna().astype(str))
d2c=set(xl["Day_2"]["FB_Category"].dropna().astype(str))
print("Day_3.Niche ∩ Day_1.FB_Category =",len(d3n&d1c),"| ∩ Day_2.FB_Category =",len(d3n&d2c),"| Day_3.Niche size",len(d3n))
print("Day_1.Niche values are curated:", set(xl["Day_1"]["Niche"].dropna().astype(str)))

# the 168 overlap: are rows identical?
e1=xl["Day_1"].assign(k=xl["Day_1"]["Email"].str.lower().str.strip())
e3=xl["Day_3"].assign(k=xl["Day_3"]["Email"].str.lower().str.strip())
ov=set(e1.k)&set(e3.k)
m=e1[e1.k.isin(ov)].merge(e3[e3.k.isin(ov)],on="k",suffixes=("_1","_3"))
print("\noverlap merged rows:",len(m))
for col in ["Name","Phone","Website","Facebook","City","Followers","Matched_Query"]:
    same=(m[col+"_1"].astype(str).str.strip()==m[col+"_3"].astype(str).str.strip()).mean()
    print(f"  {col}: identical {same*100:.1f}%")
print(" sample query pairs:", list(zip(m["Matched_Query_1"].head(4),m["Matched_Query_3"].head(4))))
print(" sample niche pairs:", list(zip(m["Niche_1"].head(4),m["Niche_3"].head(4))))

# connected-component dedup estimate
import itertools
def host(u):
    if not isinstance(u,str): return None
    mm=re.match(r"https?://([^/]+)",u.strip(),re.I)
    if not mm: return None
    h=mm.group(1).lower().replace("www.","")
    return None if re.search(r"wa\.me|linktr\.ee|threads\.com|t\.me|share\.google|youtu\.be|amazon\.in|instagram|facebook|linkedin|bit\.ly", h) else h
keys=[]
for i,r in a.iterrows():
    ks=[]
    e=str(r["Email"]).lower().strip(); ks.append("e:"+e)
    ph=re.sub(r"\D","",str(r["Phone"])); ks.append("p:"+ph)
    fb=str(r["Facebook"]).strip(); ks.append("f:"+fb)
    h=host(r["Website"])
    if h: ks.append("h:"+h)
    keys.append(ks)
parent={}
def find(x):
    parent.setdefault(x,x)
    while parent[x]!=x: parent[x]=parent[parent[x]]; x=parent[x]
    return x
def union(x,y):
    rx,ry=find(x),find(y)
    if rx!=ry: parent[rx]=ry
for i,ks in enumerate(keys):
    union("row:%d"%i, ks[0])
    for k in ks[1:]: union(ks[0],k)
comp={}
for i in range(len(a)): comp.setdefault(find("row:%d"%i),[]).append(i)
sizes=pd.Series([len(v) for v in comp.values()])
print("\nDEDUP (union of email|phone|fb|owned-host):")
print("  clusters:",len(comp),"| rows:",len(a),"| rows removed if merged:",len(a)-len(comp))
print("  cluster size dist:",sizes.value_counts().sort_index().to_dict())
big=[v for v in comp.values() if len(v)>3][:2]
for v in big[:2]:
    print("  BIG cluster sample:", a.loc[v,["Name","Email","Website","__sheet"]].to_dict("records")[:6])

# city junk
ci=a["City"].dropna().astype(str).str.strip()
junk=[c for c in ci.unique() if c.lower() in {"nagar","vihar","colony","road","puram","extension","block","sector","enclave","marg","east","west","north","south","new","near","opp","floor","phase"}]
print("\nCITY junk tokens present:", junk)
print("  values appearing once:", list(ci.value_counts()[ci.value_counts()==1].head(20).index))

# matched query taxonomy
mq=a["Matched_Query"].dropna().astype(str)
print("\nMATCHED_QUERY: n=",len(mq),"uniq",mq.nunique())
print("  top:", mq.value_counts().head(12).to_dict())
print("  all end with 'India'? ", mq.str.strip().str.endswith("India").mean())

# followers vs relevance
d1=xl["Day_1"]
print("\nDay_1 Relevance x Niche:\n", pd.crosstab(d1["Niche"],d1["Relevance"]))
