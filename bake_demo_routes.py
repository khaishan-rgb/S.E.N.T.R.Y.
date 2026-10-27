"""Regenerate forecast2026_demo_routes.json: real road geometry + LTA stops for the demo services.
Usage: git clone --depth 1 https://github.com/cheeaun/sgbusdata && python bake_demo_routes.py sgbusdata/data/v1 forecast2026_demo_routes.json
Data: busrouter.sg / sgbusdata (derived from LTA DataMall)."""
import json,math,sys
D=sys.argv[1];OUT=sys.argv[2]
stops=json.load(open(D+'/stops.json'));services=json.load(open(D+'/services.json'));routes=json.load(open(D+'/routes.json'))
updated=open(D+'/last-updated.txt').read().strip().strip('"')
# (service, focus road) in demo order; the focus road is where that service's demo event sits.
DEMO=[('14','Bedok Nth Rd'),('166','Thomson Rd'),('12','Pasir Ris Dr 1'),('43','Punggol Way'),('190','Orchard Rd'),
      ('24','Upp Changi Rd East'),('97','AYE'),('853','Yishun Ave 2'),('156','Clementi Ave 2'),('80','Compassvale Dr')]
def decode(s):
    out,i,lat,lon=[],0,0,0
    while i<len(s):
        vals=[]
        for _ in range(2):
            sh=res=0
            while True:
                b=ord(s[i])-63;i+=1;res|=(b&31)<<sh;sh+=5
                if b<32:break
            vals.append(~(res>>1) if res&1 else res>>1)
        lat+=vals[0];lon+=vals[1];out.append((lon/1e5,lat/1e5))
    return out
K=111320.0
def xy(p,lat0=1.35):return (p[0]*K*math.cos(math.radians(lat0)),p[1]*K)
def dp(pts,tol):
    if len(pts)<3:return pts
    P=[xy(p) for p in pts];keep=[False]*len(pts);keep[0]=keep[-1]=True;stack=[(0,len(pts)-1)]
    while stack:
        a,b=stack.pop();ax,ay=P[a];bx,by=P[b];dx,dy=bx-ax,by-ay;L=dx*dx+dy*dy;best,bi=-1,None
        for i in range(a+1,b):
            px,py=P[i]
            t=0 if L==0 else max(0,min(1,((px-ax)*dx+(py-ay)*dy)/L));d=math.hypot(px-ax-t*dx,py-ay-t*dy)
            if d>best:best,bi=d,i
        if bi is not None and best>tol:keep[bi]=True;stack+=[(a,bi),(bi,b)]
    return [p for p,k in zip(pts,keep) if k]
def project(line,cum,p,start=0):
    """distance along line of the nearest point, searching forward from segment `start` (keeps stop order)."""
    P=xy(p);best=(1e18,0,start)
    for i in range(start,len(line)-1):
        a,b=xy(line[i]),xy(line[i+1]);dx,dy=b[0]-a[0],b[1]-a[1];L=dx*dx+dy*dy
        t=0 if L==0 else max(0,min(1,((P[0]-a[0])*dx+(P[1]-a[1])*dy)/L))
        d=math.hypot(P[0]-a[0]-t*dx,P[1]-a[1]-t*dy)
        if d<best[0]-0.01:best=(d,cum[i]+t*math.sqrt(L),i)
        if best[0]<150 and d>best[0]+3000 and i>best[2]+40:break
    return best
out=[]
for svc,focus in DEMO:
    best=None
    for raw in [p for enc in routes[svc] for p in (decode(enc),decode(enc)[::-1])]:   # pick the polyline whose path actually carries direction-1 stops in order
        line=dp(raw,4.0)
        cum=[0.0]
        for a,b in zip(line,line[1:]):A,B=xy(a),xy(b);cum.append(cum[-1]+math.hypot(B[0]-A[0],B[1]-A[1]))
        seq=[];seg=0;off=[]
        for n,code in enumerate(services[svc]['routes'][0],1):
            lon,lat,name,road=stops[code]
            d,m,seg=project(line,cum,(lon,lat),seg)
            off.append(d);seq.append({'code':code,'name':name,'road':road,'point':[round(lon,6),round(lat,6)],'sequence':n,'route_m':round(m)})
        if best is None or max(off)<max(best[3]):best=(line,cum,seq,off)
    line,cum,seq,off=best
    assert max(off)<120,(svc,max(off))
    idx=[i for i,s in enumerate(seq) if s['road']==focus];assert idx,(svc,focus)
    m=idx[len(idx)//2];lo=hi=m   # contiguous run of focus-road stops around the middle one
    while lo-1>=0 and seq[lo-1]['road']==focus:lo-=1
    while hi+1<len(seq) and seq[hi+1]['road']==focus:hi+=1
    mid=seq[m];a=max(seq[lo]['route_m'],mid['route_m']-800);b=min(seq[hi]['route_m'],mid['route_m']+800)
    if b-a<600:a,b=max(0,mid['route_m']-300),min(cum[-1],mid['route_m']+300)
    out.append({'svc':svc,'dir':1,'name':services[svc]['name'],'focus_road':focus,'focus_stop':mid['code'],'focus_m':mid['route_m'],'stretch_m':[round(a),round(b)],
                'length_m':round(cum[-1]),'line':[[round(x,6),round(y,6)] for x,y in line],'stops':seq})
    print(svc,focus,len(line),'pts',round(cum[-1]),'m',len(seq),'stops','max stop offset',round(max(off)),'m','stretch',round(b-a),'m')
json.dump({'source':'busrouter.sg / sgbusdata (derived from LTA DataMall), data updated '+updated,'services':out},open(OUT,'w'),separators=(',',':'))
