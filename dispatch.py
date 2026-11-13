"""Spread dispatcher entry point.

  python dispatch.py --level 8 --seed study-1     # practice world
  python dispatch.py --board --level 8            # next ranking run
  python dispatch.py --final                      # the final (waits for it)
"""
from __future__ import annotations
import argparse, json, sys, time
import envload
from spread.client import Client, SpreadError, Unreachable
from spread.starter import DAY
import engine, v1

def report(label, score):
    print(f"{label:<9} margin €{score['marginCents']/100:>11,.2f}  revenue €{score['revenueCents']/100:>10,.2f}  "
          f"{score['wonOrders']:>4} orders  {score['onTimeShare']:6.1%} on time  {score['undeliveredShare']:5.1%} not delivered", flush=True)

class FinalDesk(v1.V1Desk):
    def rejoin(self, snapshot, history):
        super().rejoin(snapshot, history)
        for o in snapshot['orders']:
            if o['status']=='delivered' and o.get('deliveredAt') and all(x is not None for x in o['deliveredAt']):
                if max(o['deliveredAt'])<=o['deadline']:
                    self.trust[o['dealerId']]=min(25,self.trust.get(o['dealerId'],0)+1)

def play_final(world, params):
    client=world.client
    history=world.events(); snap=world.snapshot()
    if snap['now']>=snap['endsAt']: return world.score()
    pol=v1.V1Policy(params, world.roster(), client.map())
    desk=FinalDesk(world,8,params,pol)
    desk.now=snap['now']
    desk.rejoin(snap,history)
    desk.flush_pending()
    desk.send()
    last=history[-1]['seq'] if history else -1
    for ev in world.stream(after=last, idle=1.0):
        try:
            now=client.final()['now'] if ev is None else max(desk.now, ev['at'])
        except SpreadError:
            now=desk.now
        desk.now=now
        if ev is not None: desk.handle(ev)
        desk.check_timers()
        desk.flush_pending()
        desk.send()
        if desk.now>=snap['endsAt']: break
    return world.score()

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--level',type=int,default=8)
    ap.add_argument('--seed',default=None)
    ap.add_argument('--board',action='store_true')
    ap.add_argument('--final',action='store_true')
    ap.add_argument('--config',default=None,help='JSON file of Params overrides')
    args=ap.parse_args()
    p=engine.Params()
    if args.config:
        for k,val in json.load(open(args.config)).items(): setattr(p,k,val)
    client=Client()
    if args.final:
        while True:
            try:
                f=client.final(); world=client.world(f['worldId']); snap=world.snapshot()
                if snap['now']<snap['endsAt']: break
            except SpreadError: pass
            print('No final is set or the last one has ended. Waiting.',flush=True); time.sleep(10)
        print(f"Final world {world.id}.",flush=True)
        while True:
            try:
                score=play_final(world,p); break
            except Unreachable as e:
                print(f"spread: {e}. Rebuilding in 5 seconds.",file=sys.stderr,flush=True); time.sleep(5)
        report('End',score)
        return
    world=client.create(level=args.level,seed=args.seed,board=args.board)
    kind='Ranking run' if args.board else 'Practice world'
    print(f"{kind} {world.id}, level {args.level}.",flush=True)
    snap=world.snapshot()
    pol=v1.V1Policy(p,world.roster(),client.map())
    desk=v1.V1Desk(world,args.level,p,pol)
    desk.now=snap['now']; ends=snap['endsAt']
    while desk.now<ends:
        nxt=desk.next_due()
        lim=min(ends,(desk.now//DAY+1)*DAY,nxt if nxt is not None else ends)
        step=desk.flush_advance(until=engine.WAKE,max=lim)
        desk.now=step['now']
        for e in step['events']: desk.handle(e)
        desk.check_timers()
        desk.flush_pending()
        if desk.now//DAY!= (desk.now-1)//DAY:
            pass
        if desk.now>=ends: break
    desk.flush_pending()
    score=world.score()
    if not args.board:
        try: world.reveal()
        except SpreadError: pass
        world.delete()
    report('End',score)
    print('Ranked.' if score['ranked'] else 'Not ranked.')

if __name__=='__main__':
    main()
