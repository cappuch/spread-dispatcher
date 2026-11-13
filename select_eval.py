import json,collections,statistics as st,math
d=json.load(open('data/v1_study-1_A.json')); ev=d['events']
reqs={r['request']['id']:r['request'] for r in d['reveal']['requests']}
chosen={r['request']['id']:r['option'] for r in d['reveal']['requests'] if r['outcome']=='won'}
roster=json.load(open('data/roster.json')); MAP=json.load(open('data/map.json'))
MIN={(r['from'],r['to']):r['minutes'] for r in MAP['roads']}
cnt=collections.Counter(x['homePlaceId'] for x in roster)
N30={p:sum(c for t,c in cnt.items() if MIN.get((p,t),1e9)<=30) for p in [pl['id'] for pl in MAP['places']]}
exp=collections.Counter(e['gigId'] for e in ev if e['kind']=='gig_expired')
g2o=collections.defaultdict(set); postof={}
for p in d.get('posts',[]):
    postof[p['gid']]=p
    for s in p['stops']: g2o[p['gid']].add(s['orderId'])
# ledger
rev={}; late={}; pen={}
for e in ev:
    if e['kind']=='ledger':
        l=e['line']
        if l['kind']=='revenue': rev[l['orderId']]=l['cents']
        elif l['kind']=='late_refund': late[l['orderId']]=-l['cents']
        elif l['kind']=='undelivered_penalty': pen[l['orderId']]=-l['cents']
gfee=collections.Counter()
fails=collections.Counter()
for e in ev:
    if e['kind']=='ledger':
        l=e['line']
        if l['kind']=='driver_fee': gfee[l['gigId']]+=-l['cents']
        if l['kind']=='failed_visit': fails[l['gigId']]+=-l['cents']
rows=[]
for o in d['reveal']['orders']:
    rid=o['orderId']; req=reqs.get(rid); kind=chosen.get(rid)
    if not req or not kind: continue
    dl=next(x['deadline'] for x in req['options'] if x['kind']==kind)
    L=next(x['listPriceCents'] for x in req['options'] if x['kind']==kind)
    ds=[l['deliveredAt'] for l in o['legs']]
    ontime=all(x is not None for x in ds) and max(ds)<=dl
    a=req['legs'][0]['pickup']['placeId']; b=req['legs'][0]['delivery']['placeId']
    gids=[g for g,ords in g2o.items() if rid in ords]
    fee=sum(gfee[g]/max(1,len(g2o[g])) for g in gids); fail=sum(fails[g]/max(1,len(g2o[g])) for g in gids)
    rows.append({'rid':rid,'ontime':ontime,'rev':rev.get(rid,0),'late':late.get(rid,0),'pen':pen.get(rid,0),'fee':fee,'fail':fail,
      'lead':(dl-req['arrivedAt'])/60,'mins':MIN.get((a,b),120),'sup':N30[a]+.5*N30[b],'n30a':N30[a],'a':a,'b':b,'kind':kind,
      'private':any(l['pickup']['privateAddress'] or l['delivery']['privateAddress'] for l in req['legs']),
      'r':req['referenceFeeCents']/L,'ready_h':(req['legs'][0]['pickup']['window']['from']%1440)/60,
      'exp':any(exp[g] for g in gids),'batch':req['batchId'] is not None})
def evalset(rows,name):
    n=len(rows); ot=sum(r['ontime'] for r in rows)
    margin=sum(r['rev']-r['fee']-r['late']-r['pen']-r['fail'] for r in rows)
    print(f"{name:<28} n={n:4d} ontime={ot/n:6.2%} margin={margin/100:9.0f} eur/order={margin/100/n:7.1f}")
evalset(rows,'all')
def rule(r):
    return not ((r['n30a']<15) or (r['sup']<20) or (r['mins']>240 and r['sup']<80) or (r['lead']<8 and r['sup']<60) or (r['private'] and r['sup']<40))
evalset([r for r in rows if rule(r)],'conservative')
def rule2(r):
    return not ((r['n30a']<15) or (r['sup']<25) or (r['mins']>240 and r['sup']<100) or (r['lead']<10 and r['sup']<80) or (r['private'] and r['sup']<60))
evalset([r for r in rows if rule2(r)],'more conservative')
def rule3(r):
    return not ((r['n30a']<20) or (r['sup']<30) or (r['mins']>240 and r['sup']<120) or (r['lead']<12) or (r['private']))
evalset([r for r in rows if rule3(r)],'aggressive')
# print best/worst
print('\nmisses kept by conservative:')
for r in [r for r in rows if rule(r) and not r['ontime']][:10]: print(r)
