import json,sys,collections,statistics as st
d=json.load(open(sys.argv[1])); ev=d['events']; rev=d.get('reveal',{})
reqs={r['request']['id']:r['request'] for r in rev.get('requests',[])}
chosen={r['request']['id']:r['option'] for r in rev.get('requests',[]) if r['outcome']=='won'}
led=[e['line'] for e in ev if e['kind']=='ledger']
by=collections.Counter()
for l in led: by[l['kind']]+=l['cents']
print('score',d['score'])
print('money',{k:round(v/100,1) for k,v in by.items()})
print('events',collections.Counter(e['kind'] for e in ev))
g2o=collections.defaultdict(set)
for e in ev:
    if e['kind']=='picked_up': g2o[e['gigId']].add(e['orderId'])
book=[e for e in ev if e['kind']=='gig_booked']
rat=[]
for e in book:
    o=next(iter(g2o.get(e['gigId'],[])),None)
    if o and o in reqs: rat.append(e['feeCents']/reqs[o]['referenceFeeCents'])
print('bookings',len(book),'fee/ref med/mean/p90',round(st.median(rat),3) if rat else None,round(st.mean(rat),3) if rat else None,round(sorted(rat)[int(.9*len(rat))],3) if rat else None)
late=collections.Counter(); lead=collections.defaultdict(lambda:[0,0]); rows=[]
for o in rev.get('orders',[]):
    rid=o['orderId']; req=reqs.get(rid); kind=chosen.get(rid)
    if not req or not kind: continue
    dl=next(x['deadline'] for x in req['options'] if x['kind']==kind)
    L=next(x['listPriceCents'] for x in req['options'] if x['kind']==kind)
    ds=[l['deliveredAt'] for l in o['legs']]
    if any(x is None for x in ds): k='undel'
    else:
        lated=max(ds)-dl
        k='ontime' if lated<=0 else 'late<2h' if lated<=120 else 'late2-24h' if lated<=1440 else 'late>24h'
    late[k]+=1
    b=(req['arrivedAt']//1440)
    lh=(dl-req['arrivedAt'])/60
    bucket='<6h' if lh<6 else '6-12h' if lh<12 else '12-24h' if lh<24 else '24-48h' if lh<48 else '>48h'
    lead[bucket][0]+= k=='ontime'; lead[bucket][1]+=1
    rows.append((rid,k,lh,req['referenceFeeCents']/max(1,L)))
print('late',late,'orders',len(rows))
print('ontime by lead',{b:(round(v[0]/v[1],2),v[1]) for b,v in sorted(lead.items())})
print('ontime',d['score'].get('onTimeShare'))
# posts group sizes
posts=d.get('posts',[])
sizes=collections.Counter(len(p['stops'])//2 for p in posts)
print('post car-count',sizes)
# fees by group size
gfee={}
for e in ev:
    if e['kind']=='ledger' and e['line']['kind']=='driver_fee': gfee[e['line']['gigId']]=-e['line']['cents']
for n in sorted(sizes):
    xs=[gfee[p['gid']] for p in posts if len(p['stops'])//2==n and p['gid'] in gfee]
    if xs: print(' group',n,'n',len(xs),'fee',round(st.mean(xs)/100,1))
