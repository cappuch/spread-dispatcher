import envload, json, hashlib, time, collections
from spread.client import Client
from spread.starter import HOUR, DAY, undelivered, stops_for, without_windows, Retry, pickups
from engine import Desk, Params, WAKE

KS=[0.8,1.0,1.2,1.4,1.6,1.8]
def kfor(seed,rid):
    u=int.from_bytes(hashlib.sha256((seed+'/'+rid).encode()).digest()[:8],'big')/2**64
    return KS[min(len(KS)-1,int(u*len(KS)))]

class Flat:
    def __init__(self,p,roster,mapdata,seed): self.p=p; self.seed=seed
    def quote(self,desk,req):
        o=req['options'][0]
        return {o['kind']: o['listPriceCents']}
    def fees(self,desk,order,cars):
        ref=desk.requests[order['id']]['referenceFeeCents']
        k=kfor(self.seed,order['id'])
        return [{'at':desk.now,'feeCents':max(1,int(ref*k*cars/len(order['legs'])))}]
    def on_win(self,desk,order): desk.post(order,undelivered(order))
    def on_deadline(self,desk,oid): pass
    def on_drop(self,desk,gid): pass

def run(seed):
    c=Client(); w=c.create(level=8,seed=seed); snap=w.snapshot()
    d=Desk(w,8,Params(),Flat(None,w.roster(),c.map(),seed))
    d.now=snap['now']; ends=snap['endsAt']
    while d.now<ends:
        day=d.now//DAY; mid=(day+1)*DAY
        st=d.flush_advance(until=['price_request','order_won'],max=min(ends,mid))
        d.now=st['now']
        for e in st['events']: d.handle(e)
        if d.now>=ends: break
    rev=w.reveal(); sc=w.score(); w.delete()
    return {'seed':seed,'events':d.log['events'],'reveal':rev,'score':sc}

if __name__=='__main__':
    import sys
    seed=sys.argv[1] if len(sys.argv)>1 else 'flat-1'
    t=time.time(); out=run(seed); print('done',seed,time.time()-t,out['score'])
    json.dump(out,open(f'data/flat_{seed}.json','w'))
