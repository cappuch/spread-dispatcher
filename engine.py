"""Dispatcher engine: event-driven desk + policy hooks, for practice worlds and the final."""
from __future__ import annotations
import envload  # loads SPREAD_URL/SPREAD_KEY
import json, math, time, hashlib
from dataclasses import dataclass, field, asdict
from typing import Any, Optional
from spread.client import Client, SpreadError, Unreachable
from spread.starter import (HOUR, DAY, Retry, stops_for, without_windows, retry_from,
                            undelivered, pickups, narrower)

WAKE = ["price_request","order_won","order_changed","order_cancelled","visit_failed",
        "gig_expired","deadline_close","gig_dropped","no_show","forecast","release"]

@dataclass
class Params:
    # pricing
    m_same: float = 1.30
    m_next: float = 1.24
    m_3: float = 1.18
    lane_gamma: float = 0.10       # lane supply adjustment strength
    dealer_adj_max: float = 1.25
    min_margin_cents: int = 500    # decline orders whose expected margin below this
    # driver fees
    fee_open: float = 0.85         # opening fee / reference
    fee_peak: float = 1.75         # fee at booking deadline / reference
    fee_ramp_hours: float = 6.0
    fee_cap: float = 2.6
    urgent_start: float = 1.4
    urgent_step: float = 0.12
    # operations
    deliver_buffer: int = 60       # minutes before the deadline a delivery must arrive
    fee_floor: float = 1.20        # opening fee floor / reference
    fee_late_1: float = 1.45       # public fee at T-8h
    fee_late_2: float = 1.80       # public fee at T-5h
    fee_cap: float = 1.60          # opening fee cap / reference
    immediate_offers: bool = False
    offer_initial: float = 1.30    # initial direct-offer fee / reference
    offer_late: float = 1.60
    offer_work_scale: float = 0.85
    sel_origin_min: int = 15
    sel_sup_min: int = 20
    sel_long_min: int = 240
    sel_long_sup: int = 80
    sel_short_lead: int = 8
    sel_short_sup: int = 100
    sel_cap_factor: float = 0.5
    combine_min_saving: int = 40
    combine_max_cars: int = 3
    group_delay: int = 3
    group_delay_batch: int = 15
    group_delay_long: int = 3
    group_delay_mid: int = 3
    group_delay_short: int = 3
    group_epoch: bool = False
    epoch_long: int = 60
    epoch_mid: int = 20
    threshold_long: int = 18
    threshold_mid: int = 6
    group_discount: float = 0.90
    group_discount_batch: float = 0.90
    fee_scale: float = 1.0
    short_step: bool = False
    timed_steps: bool = False
    stage1_mult: float = 1.25
    stage2_mult: float = 1.35
    calibrate: bool = False
    probe: bool = False
    probe_n: int = 80
    trust_relax: bool = False
    hard_guard: bool = True
    war_guard: bool = True
    adapt: bool = False
    guard: bool = True
    quote_base: float = 1.303
    lane_shrink: float = 1.0
    dealer_prior: bool = False
    merge_existing: bool = False
    direct_offer: bool = True
    offer_lead: int = 180          # minutes before lastPickupAt to start offers
    offer_count: int = 3
    combine: bool = True
    max_cars: int = 3
    log_events: bool = True

class Desk:
    def __init__(self, world, level, params: Params, policy, log=None):
        self.world, self.level, self.p = world, level, params
        self.policy = policy
        self.now = 0
        self.outbox = []
        self.requests = {}
        self.orders = {}
        self.leg_gigs = {}     # orderId -> {leg: gigId}
        self.booked = set()
        self.posts = {}        # gigId -> post action
        self.urgent = set()
        self.gig_meta = {}     # gigId -> {orderIds, cars, postedAt, schedule}
        self.log = log if log is not None else {'events': [], 'actions': [], 'results': []}
        self.trust = {}        # dealerId -> on-time count
        self.dealer_quotes = {}  # dealerId -> [won?]
        self.lane_stats = {}   # (a,b) -> {'won':n,'lost':n}
        self.offers_sent = {}  # (driverId, day) -> count
        self.offers_open = []  # (gigId, driverId, at)
        self.stats = {'quotes':0,'quote_ok':0,'wins':0,'posts':0,'offers':0,'booked_first':0,'booked_total':0}
        self.checked=0; self.on_time_ok=0
        self.gig_driver={}; self.driver_pos={}; self.driver_free={}
    # ---- snapshot / rejoin ----
    def rejoin(self, snapshot, history):
        self.now = snapshot['now']
        self.booked = {g['id'] for g in snapshot['gigs'] if g['status'] in ('booked','working','done')}
        posted = {}
        for gig in snapshot['gigs']:
            posted.setdefault(gig['stops'][0]['orderId'], []).append(gig)
            if gig['status'] in ('open','booked','working'):
                action = {'type':'post','stops':gig['stops'],'audience':gig['audience'],'schedule':gig['schedule']}
                self.carry(action, gig['id'])
                self.gig_meta[gig['id']] = {'postedAt':gig['postedAt']}
        self.requests = {e['request']['id']: e['request'] for e in history if e['kind']=='price_request'}
        self.urgent = {e['orderId'] for e in history if e['kind']=='deadline_close'}
        self.orders = {o['id']: o for o in snapshot['orders'] if o['id'] in self.requests and o['status']!='cancelled'}
        for order in list(self.orders.values()):
            if order['status']=='waiting' and not self.gigs_of(order['id']):
                self.post(order, undelivered(order))
        for event in history:
            order = self.orders.get(event.get('orderId',''))
            if order is None: continue
            if event['kind']=='visit_failed' and order['deliveredAt'][event['leg']] is None:
                later=[g for g in posted.get(order['id'],[]) if g['postedAt']>=event['at'] and g['id']!=event['gigId']]
                if not any(s['leg']==event['leg'] for g in later for s in g['stops']):
                    self.retry(order['id'],event['leg'],event['reason'])
        for req in snapshot['openRequests']:
            self.quote(req)
    # ---- events ----
    def handle(self, e):
        if self.p.log_events: self.log['events'].append(e)
        k = e['kind']
        if k=='price_request': self.quote(e['request'])
        elif k=='order_won':
            self.stats['wins']+=1
            o=e['order']; self.orders[o['id']]=o
            self.policy.on_win(self,o)
        elif k=='order_lost':
            self.note_outcome(e.get('requestId'), False)
        elif k=='order_changed':
            self.orders[e['order']['id']]=e['order']
            if self.withdraw(e['order']['id']): self.post(e['order'],undelivered(e['order']))
        elif k=='order_cancelled':
            for books in (self.orders,self.leg_gigs): books.pop(e['orderId'],None)
        elif k=='visit_failed': self.retry(e['orderId'],e['leg'],e['reason'])
        elif k=='gig_expired': self.repost(e['gigId'])
        elif k=='deadline_close': self.policy.on_deadline(self,e['orderId'])
        elif k=='gig_booked':
            self.booked.add(e['gigId']); self.stats['booked_total']+=1
            self.gig_driver[e['gigId']]=e.get('driverId')
            meta=self.gig_meta.get(e['gigId'],{})
            if e['at'] - meta.get('postedAt',e['at']) <= 0: self.stats['booked_first']+=1
            self.offers_open=[x for x in self.offers_open if x[0]!=e['gigId']]
        elif k in ('gig_dropped','no_show'):
            self.booked.discard(e['gigId']); self.policy.on_drop(self,e['gigId'])
        elif k=='delivered':
            did=self.gig_driver.get(e['gigId'])
            if did:
                self.driver_pos[did]=e['placeId']; self.driver_free[did]=e['at']
            self.leg_gigs.get(e['orderId'],{}).pop(e['leg'],None)
            o=self.orders.get(e['orderId'])
            if o is not None:
                o['deliveredAt'][e['leg']]=e['at']
                if all(x is not None for x in o['deliveredAt']):
                    self.checked+=1
                    if max(o['deliveredAt'])<=o['deadline']:
                        self.on_time_ok+=1
                        self.trust[o['dealerId']]=min(25,self.trust.get(o['dealerId'],0)+1)
        elif k=='release':
            o=self.orders.get(e['orderId'])
            if o and 'legs' in o:
                pass
        elif k=='offer_answered':
            self.offers_open=[x for x in self.offers_open if not (x[0]==e['gigId'] and x[1]==e['driverId'])]
    def note_outcome(self, rid, won):
        req=self.requests.get(rid)
        if not req: return
        lane=(req['legs'][0]['pickup']['placeId'], req['legs'][0]['delivery']['placeId'])
        d=self.lane_stats.setdefault(lane,{'won':0,'lost':0}); d['won' if won else 'lost']+=1
        self.dealer_quotes.setdefault(req['dealerId'],[]).append(won)
    # ---- quoting ----
    def quote(self, req):
        self.requests[req['id']]=req
        prices=self.policy.quote(self,req)
        if prices is not None:
            self.outbox.append({'type':'quote','requestId':req['id'],'prices':prices})
            self.stats['quotes']+=1
    # ---- gigs ----
    def initial_post(self, order):
        self.post(order, undelivered(order))
    def post(self, order, legs, retry=Retry(), clients=None):
        aud={'experienced': any(l['car']['premium'] for l in order['legs']),
             'manualLicence': retry.manual_licence}
        stops=without_windows(stops_for(order,legs,self.requests[order['id']]['note'],retry), self.now)
        sched=self.policy.fees(self,order,len(legs))
        self.outbox.append({'type':'post','stops':stops,'audience':aud,'schedule':sched})
        self.stats['posts']+=1
        if len(legs)==1: return
        self.send()
        if not self.gigs_of(order['id']):
            for leg in legs: self.post(order,[leg],retry)
    def fees(self, order, cars): return self.policy.fees(self,order,cars)
    def repost(self,gid):
        a=self.posts.get(gid)
        if a is None or not any(self.leg_gigs.get(s['orderId'],{}).get(s['leg'])==gid for s in a['stops']): return
        for s in a['stops']: self.leg_gigs.get(s['orderId'],{}).pop(s['leg'],None)
        order=self.orders.get(a['stops'][0]['orderId'])
        if order is None: return
        self.outbox.append({**a,'stops':without_windows(a['stops'],float('inf')),'schedule':self.fees(order,pickups(a))})
    def retry(self,oid,leg,reason):
        if oid not in self.orders: return
        order=self.orders[oid]; self.leg_gigs.get(oid,{}).pop(leg,None)
        car_not_there=reason in ('not_ready','closed')
        pf=retry_from(order,self.requests[oid],leg,self.now) if car_not_there else 0
        self.post(order,[leg],Retry(pf,manual_licence=reason=='licence',plates=reason=='no_plates'))
    def reprice(self,gid,sched):
        if gid in self.booked: return
        self.outbox.append({'type':'reprice','gigId':gid,'schedule':sched})
        if gid in self.posts: self.posts[gid]={**self.posts[gid],'schedule':sched}
    def offer(self,gid,driver,fee):
        if self.offers_sent.get((driver,self.now//DAY),0)>=3: return False
        if sum(1 for x in self.offers_open if x[0]==gid)>=3: return False
        self.offers_sent[(driver,self.now//DAY)]=self.offers_sent.get((driver,self.now//DAY),0)+1
        self.offers_open.append((gid,driver,self.now))
        self.outbox.append({'type':'offer','gigId':gid,'driverId':driver,'feeCents':fee})
        self.stats['offers']+=1
        return True
    def gigs_of(self,oid): return list(dict.fromkeys(self.leg_gigs.get(oid,{}).values()))
    def carry(self,a,gid):
        for s in a['stops']: self.leg_gigs.setdefault(s['orderId'],{})[s['leg']]=gid
        self.posts[gid]=a
        self.gig_meta[gid]={'postedAt':self.now}
    def withdraw(self,oid):
        self.send_post_of(oid); gigs=self.gigs_of(oid)
        if any(g in self.booked for g in gigs): return False
        self.leg_gigs.pop(oid,None)
        self.outbox += [{'type':'withdraw','gigId':g} for g in gigs]
        return True
    def send_post_of(self,oid):
        if any(a['type']=='post' and a['stops'][0]['orderId']==oid for a in self.outbox): self.send()
    def send(self):
        actions,self.outbox=self.outbox,[]
        if actions:
            results=self.world.act(actions)
            self.log['actions'].append({'at':self.now,'actions':actions})
            self.read_results(actions,results)
        return actions
    def read_results(self,actions,results):
        self.log['results'].append({'at':self.now,'results':results})
        for a,r in zip(actions,results):
            if a['type']=='post' and r.get('ok'):
                self.carry(a,r['id'])
                self.log.setdefault('posts',[]).append({'at':self.now,'gid':r['id'],'stops':a['stops'],
                                                        'schedule':a['schedule'],'audience':a['audience']})
    def flush_advance(self, **kw):
        actions,self.outbox=self.outbox,[]
        step=self.world.advance(actions=actions,**kw)
        self.read_results(actions,step['results'])
        return step

# ---------------- policy ----------------
class Policy:
    def __init__(self, p: Params, roster=None, mapdata=None):
        self.p=p
        self.roster=roster or []
        self.map=mapdata
        self.supply=None
        if mapdata: self._build_supply()
    def _build_supply(self):
        # drivers per town
        from collections import Counter
        cnt=Counter(d['homePlaceId'] for d in self.roster)
        towns=[pl['id'] for pl in self.map['places']]
        mins={(r['from'],r['to']):r['minutes'] for r in self.map['roads']}
        self.supply={}
        for t in towns:
            near=0
            for o in towns:
                if mins.get((t,o),1e9)<=60: near+=cnt.get(o,0)
            self.supply[t]=(cnt.get(t,0),near)
        self.avg_supply=sum(n for n,_ in self.supply.values())/max(1,len(self.supply))
    def lane_adj(self,a,b):
        if not self.supply: return 1.0
        sa=self.supply.get(a,(0,0)); sb=self.supply.get(b,(0,0))
        # more local/near drivers at both ends = easier lane = competitors cheaper; quote lower
        score=(math.sqrt(sa[0]+1)*math.sqrt(sb[0]+1)) / max(1,self.avg_supply)
        # score ranges wildly; map to factor around 1
        return max(0.85, min(1.15, 1.0 + self.p.lane_gamma*math.log(max(0.05,score))/max(0.01,abs(math.log(max(0.05,score)))+1)))
    def quote(self, desk, req):
        p=self.p
        base={'same_day':p.m_same,'next_day':p.m_next,'within_3_days':p.m_3}
        lane_adj=self.lane_adj(req['legs'][0]['pickup']['placeId'], req['legs'][0]['delivery']['placeId'])
        # dealer adjustment: trust and recent wins
        trust=desk.trust.get(req['dealerId'],0)
        d_adj=1.0+0.01*trust
        prices={}
        for o in req['options']:
            m=base[o['kind']]*lane_adj*d_adj
            prices[o['kind']]=max(1,int(round(o['listPriceCents']*m)))
        return prices
    def fees(self, desk, order, cars):
        p=self.p
        req=desk.requests[order['id']]
        base=req['referenceFeeCents']*cars/len(order['legs'])
        # lane factor: scarce lanes need more
        a=req['legs'][0]['pickup']['placeId']; b=req['legs'][0]['delivery']['placeId']
        adj=1.0 if not self.supply else max(0.9,min(1.5,1.0+0.25*math.log(max(0.1,(self.supply.get(a,(0,0))[1]+1)/max(1,self.avg_supply)))) )
        if order['id'] in desk.urgent:
            s=base*adj*p.urgent_start
            steps=5 if desk.level>1 else 1
            return [{'at':desk.now+k*30,'feeCents':max(1,int(base*adj*(p.urgent_start+k*p.urgent_step)+.5))} for k in range(steps)]
        # ramp from open to peak by the booking target
        target=max(desk.now+60, order['lastPickupAt']-p.deliver_buffer)
        hours=max(1.0,(target-desk.now)/HOUR)
        hi=base*adj*min(p.fee_cap,p.fee_peak)
        lo=base*adj*p.fee_open
        steps=min(12,max(2,int(hours)+1))
        out=[]
        for k in range(steps):
            at=desk.now+int(k*hours*HOUR/(steps-1))
            frac=k/(steps-1)
            fee=lo+(hi-lo)*frac**0.8
            out.append({'at':at,'feeCents':max(1,int(round(fee)))})
        return out
    def on_win(self,desk,order):
        desk.post(order,undelivered(order))
    def on_deadline(self,desk,oid):
        order=desk.orders.get(oid)
        if order is None: return
        desk.urgent.add(oid); desk.send_post_of(oid)
        for gid in desk.gigs_of(oid):
            if gid not in desk.booked: desk.reprice(gid, self.fees(desk,order,pickups(desk.posts[gid])))
        if self.p.direct_offer: self.send_offers(desk,order)
    def on_drop(self,desk,gid):
        post=desk.posts.get(gid)
        if not post: return
        order=desk.orders.get(post['stops'][0]['orderId'])
        if order and gid not in desk.booked:
            desk.reprice(gid,self.fees(desk,order,pickups(post)))
            if self.p.direct_offer and desk.now>=order['lastPickupAt']-self.p.offer_lead:
                self.send_offers(desk,order)
    def send_offers(self,desk,order):
        gigs=[g for g in desk.gigs_of(order['id']) if g not in desk.booked]
        if not gigs: return
        pickup=order['legs'][0]['pickup']['placeId']
        drive=order['legs'][0]
        # candidates: drivers home near pickup (by road minutes), not already offered today recently
        near=[]
        if self.map:
            mins={(r['from'],r['to']):r['minutes'] for r in self.map['roads']}
            for d in self.roster:
                m=mins.get((pickup,d['homePlaceId']),1e9)
                if m<=90: near.append((m,d['id']))
        near.sort()
        fee=int(order['priceCents']*0.0) # placeholder
        # fee from policy: urgent level
        req=desk.requests[order['id']]
        fee=max(1,int(req['referenceFeeCents']*(self.p.urgent_start+self.p.urgent_step*2)))
        sent=0
        for m,did in near:
            if sent>=self.p.offer_count: break
            for gid in gigs:
                if desk.offer(gid,did,fee): sent+=1; break
            if sent>=self.p.offer_count: break

# ---------------- runners ----------------
def run_practice(seed, level=8, params=None, policy=None, tag='', client=None):
    client=client or Client()
    w=client.create(level=level,seed=seed)
    snap=w.snapshot()
    roster=w.roster()
    mapdata=client.map()
    pol=policy or Policy(params or Params(),roster,mapdata)
    if isinstance(pol,type): pol=pol(params or Params(),roster,mapdata)
    desk=Desk(w,level,params or Params(),pol)
    desk.now=snap['now']; ends=snap['endsAt']
    while desk.now<ends:
        day=desk.now//DAY; midnight=(day+1)*DAY
        step=desk.flush_advance(until=WAKE,max=min(ends,midnight))
        desk.now=step['now']
        for e in step['events']: desk.handle(e)
        if desk.now>=ends: break
    log=desk.log; log['score']=w.score(); log['stats']=desk.stats; log['world']=w.id
    try: log['reveal']=w.reveal()
    except SpreadError: pass
    w.delete()
    return log

if __name__=='__main__':
    import sys
    seed=sys.argv[1] if len(sys.argv)>1 else 'study-1'
    p=Params()
    log=run_practice(seed,8,p)
    print(log['score'],log['stats'])
    json.dump(log,open(f'run_{seed}.json','w'))
