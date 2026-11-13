"""Experiment harness: plays a practice world with a parameterised policy and logs everything."""
from __future__ import annotations
import envload
import json, math, time, sys
from spread.client import Client, SpreadError
from spread.types import *
from spread.starter import (HOUR, DAY, WAKE_ON, Retry, stops_for, without_windows,
                            retry_from, undelivered, pickups, rising, steep, narrower)

class Config:
    quote_mult = 1.0          # quote = list * quote_mult (per option)
    quote_mode = "same"        # same | first | best
    fee_mult = 1.0             # opening fee = ref * fee_mult
    fee_rise_per_h = 0.10      # fraction of base per hour
    fee_steps = 6
    fee_cap = 1.6
    urgent_mult = 1.5
    urgent_rise_per_h = 0.5
    urgent_cap = 2.5
    post_delay = 0             # minutes after win to post
    direct_offer = False
    offer_mult = 1.3
    combine = False

class SimDesk:
    def __init__(self, world, level, cfg, log):
        self.world, self.level, self.cfg, self.log = world, level, cfg, log
        self.now = world.snapshot()["now"]
        self.outbox=[]
        self.requests={}; self.orders={}; self.leg_gigs={}; self.booked=set()
        self.posts={}; self.urgent=set()
        self.win_times={}
    def handle(self, e):
        self.log['events'].append(e)
        k=e['kind']
        if k=='price_request': self.quote(e['request'])
        elif k=='order_won':
            self.orders[e['order']['id']]=e['order']; self.win_times[e['order']['id']]=e['at']; self.post(e['order'], undelivered(e['order']))
        elif k=='order_changed':
            self.orders[e['order']['id']]=e['order']
            if self.withdraw(e['order']['id']): self.post(e['order'], undelivered(e['order']))
        elif k=='order_cancelled':
            for books in (self.orders,self.leg_gigs): books.pop(e['orderId'],None)
        elif k=='visit_failed':
            self.retry(e['orderId'],e['leg'],e['reason'])
        elif k=='gig_expired': self.repost(e['gigId'])
        elif k=='deadline_close': self.raise_fees(e['orderId'])
        elif k=='gig_booked': self.booked.add(e['gigId'])
        elif k in ('gig_dropped','no_show'): self.booked.discard(e['gigId'])
        elif k=='delivered':
            self.leg_gigs.get(e['orderId'],{}).pop(e['leg'],None)
            if e['orderId'] in self.orders: self.orders[e['orderId']]['deliveredAt'][e['leg']]=e['at']
    def quote(self, r):
        self.requests[r['id']]=r
        prices={o['kind']: int(round(o['listPriceCents']*self.cfg.quote_mult)) for o in r['options']}
        if self.cfg.quote_mode=='first': prices={r['options'][0]['kind']:prices[r['options'][0]['kind']]}
        self.outbox.append({'type':'quote','requestId':r['id'],'prices':prices})
    def post(self, order, legs, retry=Retry()):
        aud={'experienced': any(l['car']['premium'] for l in order['legs']), 'manualLicence': retry.manual_licence}
        stops=without_windows(stops_for(order,legs,self.requests[order['id']]['note'],retry), self.now)
        self.outbox.append({'type':'post','stops':stops,'audience':aud,'schedule':self.fees(order,len(legs))})
        if len(legs)==1: return
        self.send()
        if not self.gigs_of(order['id']):
            for leg in legs: self.post(order,[leg],retry)
    def fees(self, order, cars):
        base=self.requests[order['id']]['referenceFeeCents']*cars/len(order['legs'])
        if order['id'] in self.urgent:
            return steep(self.now, base*self.cfg.urgent_mult/1.5, self.level) if False else [{'at':self.now+k*30,'feeCents':max(1,int(base*(self.cfg.urgent_mult+k*self.cfg.urgent_rise_per_h/2)+.5))} for k in range(1 if self.level==1 else 5)]
        n=1 if self.level==1 else self.cfg.fee_steps
        return [{'at':self.now+k*HOUR,'feeCents':max(1,int(base*(self.cfg.fee_mult+min(self.cfg.fee_cap-self.cfg.fee_mult,k*self.cfg.fee_rise_per_h))+.5))} for k in range(n)]
    def repost(self,gid):
        a=self.posts.get(gid)
        if a is None or not any(self.leg_gigs.get(s['orderId'],{}).get(s['leg'])==gid for s in a['stops']): return
        for s in a['stops']: self.leg_gigs.get(s['orderId'],{}).pop(s['leg'],None)
        order=self.orders.get(a['stops'][0]['orderId'])
        if order is None: return
        self.outbox.append({**a,'stops':without_windows(a['stops'],float('inf')),'schedule':self.fees(order,pickups(a))})
    def raise_fees(self, oid):
        order=self.orders.get(oid)
        if order is None: return
        self.urgent.add(oid); self.send_post_of(oid)
        for gid in self.gigs_of(oid):
            if gid not in self.booked:
                self.outbox.append({'type':'reprice','gigId':gid,'schedule':self.fees(order,pickups(self.posts[gid]))})
    def retry(self,oid,leg,reason):
        if oid not in self.orders: return
        order=self.orders[oid]; self.leg_gigs.get(oid,{}).pop(leg,None)
        car_not_there=reason in ('not_ready','closed')
        pf=retry_from(order,self.requests[oid],leg,self.now) if car_not_there else 0
        self.post(order,[leg],Retry(pf,manual_licence=reason=='licence',plates=reason=='no_plates'))
    def gigs_of(self,oid): return list(dict.fromkeys(self.leg_gigs.get(oid,{}).values()))
    def carry(self,a,gid):
        for s in a['stops']: self.leg_gigs.setdefault(s['orderId'],{})[s['leg']]=gid
        self.posts[gid]=a
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
        if actions: self.read_results(actions,self.world.act(actions))
    def read_results(self,actions,results):
        for a,r in zip(actions,results):
            self.log['action_results'].append({'at':self.now,'action':a,'result':r})
            if a['type']=='post' and r.get('ok'): self.carry(a,r['id'])

def play(client, world, level, cfg, tag):
    snap=world.snapshot(); desk=SimDesk(world,level,cfg,{'events':[],'action_results':[],'tag':tag})
    ends=snap['endsAt']
    wake=WAKE_ON+['gig_dropped','no_show','gig_started','forecast','delivered','order_lost','release']
    while desk.now<ends:
        day=desk.now//DAY; midnight=(day+1)*DAY
        actions,desk.outbox=desk.outbox,[]
        step=world.advance(until=wake,max=min(ends,midnight),actions=actions)
        desk.read_results(actions,step['results']); desk.now=step['now']
        for e in step['events']: desk.handle(e)
        if desk.now>=ends: break
    log=desk.log; log['score']=world.score(); log['now']=desk.now
    return log

def run(cfg, seed, level=8, tag=''):
    client=Client(); w=client.create(level=level,seed=seed)
    t=time.time(); log=play(client,w,level,cfg,tag); log['seconds']=time.time()-t
    try: log['reveal']=w.reveal()
    except SpreadError as e: log['reveal_error']=str(e)
    log['world']=w.id; w.delete()
    return log

if __name__=='__main__':
    cfg=Config(); log=run(cfg,'study-1',8,'base')
    json.dump(log,open('base_study1.json','w'))
    print(log['score'], 'secs',log['seconds'])
