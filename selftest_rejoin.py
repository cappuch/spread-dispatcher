import envload, json, sys
from spread.client import Client
import engine, v1
from spread.starter import DAY

def step_loop(desk, until):
    while desk.now < until:
        nxt = desk.next_due()
        lim = min(until, (desk.now//DAY+1)*DAY, nxt if nxt is not None else until)
        st = desk.flush_advance(until=engine.WAKE, max=lim)
        desk.now = st['now']
        for e in st['events']: desk.handle(e)
        desk.check_timers(); desk.flush_pending()
        if desk.now >= until: break

def main(seed='rejoin-1'):
    c=Client(); w=c.create(level=8,seed=seed); snap=w.snapshot()
    p=engine.Params();
    for k,v in json.load(open('config_final.json')).items(): setattr(p,k,v)
    pol=v1.V1Policy(p,w.roster(),c.map())
    d=v1.V1Desk(w,8,p,pol); d.now=snap['now']
    step_loop(d, 3000)
    print('phase1',d.now,d.stats.get('wins'))
    snap=w.snapshot(); hist=w.events()
    p2=engine.Params();
    for k,v in json.load(open('config_final.json')).items(): setattr(p2,k,v)
    pol2=v1.V1Policy(p2,w.roster(),c.map())
    d2=v1.V1Desk(w,8,p2,pol2); d2.now=snap['now']
    d2.rejoin(snap,hist); d2.flush_pending(); d2.send()
    print('rejoined',len(d2.orders),'orders',len(d2.posts),'posts',len(d2.pending),'pending')
    step_loop(d2, snap['endsAt'])
    print('score',w.score())
    w.delete()
if __name__=='__main__': main(sys.argv[1] if len(sys.argv)>1 else 'rejoin-1')
