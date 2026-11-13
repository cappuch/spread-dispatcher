"""V1 dispatcher: measured quote curve, flat fee market, explicit windows, escalation, batch combining."""
from __future__ import annotations
import envload, json, math, os, time, collections
from collections import Counter, defaultdict
from spread.client import Client, SpreadError
from spread.starter import HOUR, DAY, Retry, stops_for, without_windows, retry_from, undelivered, pickups
from engine import Desk, WAKE
import combine, notes

ROOT=os.path.dirname(os.path.abspath(__file__))
_lane=json.load(open(os.path.join(ROOT,'data','lane_prior.json')))
_lanes=_lane['lanes']; _origins=_lane['origins']; _dests=_lane['destinations']
_dp=json.load(open(os.path.join(ROOT,'data','dealer_prior.json')))
GLOBAL_THETA=1.303
MAP=json.load(open(os.path.join(ROOT,'data','map.json')))
ROADS=combine.Roads(MAP)
MINUTES={(r['from'],r['to']):r['minutes'] for r in MAP['roads']}

def lane_mult(a,b):
    k=f'{a}>{b}'
    if k in _lanes and _lanes[k].get('n',0)>=8 and _lanes[k].get('confidence')!='low':
        return _lanes[k]['multiplier']
    mo=_origins.get(a,{}).get('multiplier'); md=_dests.get(b,{}).get('multiplier')
    if mo and md: return math.sqrt(mo*md)
    return 1.0

class Supply:
    def __init__(self, roster):
        self.cnt=Counter(d['homePlaceId'] for d in roster)
        self.towns=set(self.cnt)|{p['id'] for p in MAP['places']}
        self.n30={}
        for p in {pl['id'] for pl in MAP['places']}:
            n=0
            for t,c in self.cnt.items():
                if MINUTES.get((p,t),1e9)<=30: n+=c
            self.n30[p]=n
        self.drivers=roster
        self.byhome=defaultdict(list)
        for d in roster: self.byhome[d['homePlaceId']].append(d)
    def sup(self,a,b): return self.n30.get(a,0)+0.5*self.n30.get(b,0)

class V1Policy:
    def __init__(self, p, roster, mapdata, seed=''):
        self.p=p
        self.supply=Supply(roster)
        self.roster=roster
        self.map=mapdata
        self.m_day=1.0
        self.trust=defaultdict(int)
        self.resolved=[]           # (k, won, day, lane)
        self.booked_ratios=[]
        self.stats=Counter()
        self.mu=1.08
        self.notes={}
        self.accepted_place_day=Counter()
        self.open_region=Counter()
        self.place_boost=defaultdict(lambda:1.0)
        self.res_by_place=defaultdict(list)
        self.theta_used={}
        self.calib=[]
        self.probe_k={}
        self.probe_rows=[]
        self.probe_done=False
        self.fee_boost=1.0
        self.cur_day=-1
        self.day_res=[]
    def _day_record(self, k_eff, won, desk):
        if not self.p.war_guard: return
        self.day_res.append((k_eff,won))
        if len(self.day_res)>=30 and len(self.day_res)%10==0 and desk.now%DAY<19*HOUR:
            near=[w for k,w in self.day_res if k<=self.p.quote_base*1.25]
            if len(near)>=20:
                wr=sum(near)/len(near)
                if wr<0.10: self.m_day=0.72
                elif wr<0.16: self.m_day=0.80
                elif wr>0.78: self.m_day=1.08
    def _probe_record(self, rid, won):
        if not self.p.probe or self.probe_done or rid not in self.probe_k: return
        k=self.probe_k.pop(rid); self.probe_rows.append((k,won))
        if len(self.probe_rows)>=self.p.probe_n:
            def ll(th):
                z=0.0
                for kk,w in self.probe_rows:
                    y=-10*math.log(max(1e-6,kk/th))
                    p=1/(1+math.exp(-y)) if y<50 else 1.0
                    z+=math.log(max(1e-9,p)) if w else math.log(max(1e-9,1-p))
                return z
            best=max((ll(th),th) for th in [1.02+0.005*i for i in range(99)])
            self.p.quote_base=max(1.08,min(1.45,best[1]))
            self.probe_done=True
            self.stats['probe_base']=int(round(self.p.quote_base*1000))
    def _calib(self, theta_at, k_eff, won):
        if theta_at<=0: return
        self.calib.append((k_eff/theta_at, won))
        if len(self.calib)%40==0 and len(self.calib)>=80:
            def ll(s):
                z=0.0
                for r,w in self.calib[-200:]:
                    if not (0.75<=r<=1.9): continue
                    y=-10*math.log(max(1e-6,r*s))
                    p=1/(1+math.exp(-y)) if y<50 else 1.0
                    z+=math.log(max(1e-9,p)) if w else math.log(max(1e-9,1-p))
                return z
            best=max((ll(s),s) for s in [0.80+0.005*i for i in range(101)])
            s=best[1]
            self.p.quote_base=max(1.12,min(1.45,self.p.quote_base*(1+0.7*(s-1))))
    def _record(self, place, k_eff, won, now):
        rows=self.res_by_place[place]; rows.append((now//DAY,k_eff,won))
        if len(rows)>=12 and len(rows)%4==0:
            recent=rows[-24:]
            near=[w for (d,k,w) in recent if k<=self.p.quote_base*1.15]
            if len(near)>=10:
                wr=sum(near)/len(near)
                if wr>=0.75: self.place_boost[place]=min(1.30,self.place_boost[place]*1.04+0.005)
                elif wr<=0.22: self.place_boost[place]=max(0.88,self.place_boost[place]*0.97-0.003)
    def nc(self, req):
        if req['id'] in self.notes: return self.notes[req['id']]
        note=req.get('note')
        if not note: out={}
        else:
            try: out=notes.parse_note(note['text'],note.get('language','fi'),req)
            except Exception: out={}
        self.notes[req['id']]=out
        return out
    # ---------- quoting ----------
    def theta(self, a, b, dealer, trust=0):
        lm=1.0+(lane_mult(a,b)-1.0)*self.p.lane_shrink
        dm=_dp.get(dealer,{}).get('multiplier',1.0) if self.p.dealer_prior else 1.0
        return self.p.quote_base*lm*dm*self.place_boost[a]*self.m_day*(1+0.01*min(25,trust))
    def expected_fee(self, req, a, b):
        sup=self.supply.sup(a,b)
        m0=min(1.25,1.00+0.16*math.exp(-sup/30))
        return req['referenceFeeCents']*m0*self.mu
    def quote(self, desk, req):
        day=req['arrivedAt']//DAY
        if day!=self.cur_day:
            self.cur_day=day; self.day_res=[]; self.m_day=1.0
        legs=req['legs']; a=legs[0]['pickup']['placeId']; b=legs[0]['delivery']['placeId']
        minutes=MINUTES.get((a,b),120)
        th=self.theta(a,b,req['dealerId'],desk.trust.get(req['dealerId'],0))
        sup=self.supply.sup(a,b)
        # ---- decline tests ----
        decline=False
        reasons=[]
        if len(legs)==1:
            L=req['options'][0]['listPriceCents']; r=req['referenceFeeCents']/max(1,L)
            if self.p.sel_min_list and L<self.p.sel_min_list: decline=True; reasons.append('cheap')
            if r>1.05: decline=True; reasons.append('r')
            if sup<20 and minutes>240: decline=True; reasons.append('remote')
            if legs[0]['car']['premium'] and r>0.75: decline=True; reasons.append('premium')
            # expected margin at theta on the option we prefer (cheapest list)
            fee=self.expected_fee(req,a,b)
            best=max((th*1.0*o['listPriceCents']-fee) for o in req['options'])
            if best<=0: decline=True; reasons.append('e0')
        else:
            L=sum(o['listPriceCents'] for o in req['options'])/len(req['options'])
            r=req['referenceFeeCents']/max(1,L)
            if r>1.0: decline=True; reasons.append('r2')
            fee=self.expected_fee(req,a,b)*1.05
            if th*L-fee<=0: decline=True; reasons.append('e0')
        # delivery risk: pay drivers more instead of refusing cars
        if desk.checked>=30:
            overdue=sum(1 for o in desk.orders.values() if o['deadline']<desk.now and any(x is None for x in o['deliveredAt']))
            rate=desk.on_time_ok/max(1,desk.checked+overdue)
            self.fee_boost = 1.30 if rate<0.90 else 1.15 if rate<0.94 else 1.0
        # note deadline / ready
        nc=self.nc(req)
        nd=nc.get('deadline_override') or nc.get('delivery_before')
        dl=min(o['deadline'] for o in req['options'])
        if nd: dl=min(dl,nd)
        ready=max(l['pickup']['window']['from'] for l in legs)
        if nc.get('pickup_after'): ready=max(ready,nc['pickup_after'])
        if dl-ready-minutes<90: decline=True; reasons.append('slack')
        if nc.get('summer_tyres'): decline=True; reasons.append('summer')
        lead=(dl-desk.now)/60
        strict=1.0
        if self.p.guard and desk.checked>=40:
            overdue=sum(1 for o in desk.orders.values() if o['deadline']<desk.now and any(x is None for x in o['deliveredAt']))
            rate=desk.on_time_ok/max(1,desk.checked+overdue)
            if rate<0.90: strict=1.20
            elif rate<0.95: strict=1.0+(0.95-rate)*4
            elif rate>=0.975: strict=max(0.82,1.0-(rate-0.975)*6)
        if self.p.trust_relax and desk.trust.get(req['dealerId'],0)>=18: strict*=0.78
        origin_min=max(8,int(self.p.sel_origin_min*strict))
        sup_min=max(8,int(self.p.sel_sup_min*strict))
        long_min=self.p.sel_long_min if strict<=1.0 else int(self.p.sel_long_min/max(1.0,(strict-1)*2+1))
        capf=self.p.sel_cap_factor/max(0.5,strict)
        # supply selection
        if self.supply.n30.get(a,0)<origin_min: decline=True; reasons.append('thin_origin')
        if sup<sup_min: decline=True; reasons.append('sup_min')
        if minutes>long_min and sup<self.p.sel_long_sup: decline=True; reasons.append('long_thin')
        nd=self.supply.n30.get(b,0)
        if minutes>240 and (min(self.supply.n30.get(a,0),nd)<25 or nd<20): decline=True; reasons.append('dest_thin')
        if minutes>180 and nd<12: decline=True; reasons.append('dest_remote')
        maxL=max(o['listPriceCents'] for o in req['options'])
        if maxL>20000 and (sup<60 or lead<12): decline=True; reasons.append('high_value_risk')
        if lead<self.p.sel_short_lead and sup<self.p.sel_short_sup*strict: decline=True; reasons.append('short_lead')
        priv=[l['pickup']['placeId'] if l['pickup']['privateAddress'] else l['delivery']['placeId'] for l in legs if l['pickup']['privateAddress'] or l['delivery']['privateAddress']]
        if priv and min(self.supply.n30.get(x,0) for x in priv)<20: decline=True; reasons.append('private_thin')
        if any(l['pickup']['privateAddress'] or l['delivery']['privateAddress'] for l in legs) and sup<40: decline=True; reasons.append('private')
        day=req['arrivedAt']//DAY
        cap=max(3,int(capf*self.supply.n30.get(a,0)))
        if self.accepted_place_day[(a,day)]>=cap: decline=True; reasons.append('cap')
        if decline:
            self.stats['decline_'+'_'.join(reasons) if reasons else 'decline']+=1
            return {o['kind']:max(1,int(round(o['listPriceCents']*2.6))) for o in req['options']}
        if self.p.probe and not self.probe_done and len(self.probe_rows)<self.p.probe_n:
            # deterministic spread over the plausible threshold range
            h=0
            for ch in req['id']: h=(h*131+ord(ch))%100003
            k=1.00+0.50*((h%997)/996)
            self.probe_k[req['id']]=k
            prices={o['kind']:max(1,int(round(o['listPriceCents']*k))) for o in req['options']}
            self.stats['probe_quotes']+=1
            return prices
        delta={'same_day':0.0,'next_day':-0.05,'within_3_days':-0.12}
        prices={}
        for o in req['options']:
            k=th+delta[o['kind']]
            prices[o['kind']]=max(1,int(round(o['listPriceCents']*k)))
        self.theta_used[req['id']]=th
        self.stats['contest']+=1
        return prices
    def note_win(self, desk, order):
        req=desk.requests.get(order['id'])
        if req:
            a=req['legs'][0]['pickup']['placeId']; self.accepted_place_day[(a,desk.now//DAY)]+=1
            b=req['legs'][0]['delivery']['placeId']
            chosen=order['option']; L=next(o['listPriceCents'] for o in req['options'] if o['kind']==chosen)
            ratio=order['priceCents']/max(1,L)
            self.resolved.append((ratio, True, desk.now//DAY,(a,b)))
            if self.p.adapt: self._record(a, ratio, True, desk.now)
            self._day_record(ratio, True, desk)
            if self.p.calibrate: self._calib(self.theta_used.get(order['id'],GLOBAL_THETA), ratio, True)
            self._probe_record(order['id'], True)
    def note_loss(self, desk, rid):
        req=desk.requests.get(rid)
        if not req: return
        a=req['legs'][0]['pickup']['placeId']; b=req['legs'][0]['delivery']['placeId']
        # ratio of the closest-to-threshold option we quoted
        prices=desk.quoted.get(rid,{})
        if prices:
            ratio=min(prices[o['kind']]/max(1,o['listPriceCents']) for o in req['options'] if o['kind'] in prices)
            self.resolved.append((ratio,False,desk.now//DAY,(a,b)))
            if self.p.adapt: self._record(a,ratio,False,desk.now)
            if ratio<1.9: self._day_record(ratio, False, desk)
            if self.p.calibrate and ratio<1.9: self._calib(self.theta_used.get(rid,GLOBAL_THETA), ratio, False)
            self._probe_record(rid, False)
    # ---------- fees ----------
    def m0(self,req):
        a=req['legs'][0]['pickup']['placeId']; b=req['legs'][0]['delivery']['placeId']
        sup=self.supply.sup(a,b)
        return min(1.25,1.00+0.16*math.exp(-sup/30))
    def t_hour(self, req):
        ready=req['legs'][0]['pickup']['window']['from']
        day=ready//DAY; hour=(ready%DAY)/60
        t=1.0
        if hour>=17: t+=0.08
        if ready//DAY%7==5: t+=0.10
        if 7<=hour<8: t+=0.05
        return min(1.15,t)
    def t_risk(self, req):
        t=1.0
        if any(l['pickup']['privateAddress'] or l['delivery']['privateAddress'] for l in req['legs']): t+=0.10
        if len(req['legs'])>1: t+=0.05
        if any(l['car']['premium'] for l in req['legs']): t+=0.15
        return t
    def f0(self, req, cars=1, group_ref=None):
        if group_ref is not None:
            base=group_ref*self.p.group_discount*self.m0(req)   # one driver, shared trips
        else:
            base=req['referenceFeeCents']*cars/len(req['legs'])*self.m0(req)*self.t_hour(req)*self.t_risk(req)
        return max(1,int(round(base)))
    def fees(self, desk, order, cars):
        req=desk.requests[order['id']]
        base=int(self.f0(req,cars)*self.p.fee_scale*self.fee_boost)
        if max(o['listPriceCents'] for o in req['options'])>20000: base=int(base*1.10)
        cap=int(req['referenceFeeCents']*1.25*cars/len(order['legs'])*self.fee_boost)
        base=min(base,cap)
        T=order['lastPickupAt']-desk.now
        ref_car=req['referenceFeeCents']*cars/len(order['legs'])
        if T < 8*HOUR: base=max(base,int(ref_car*self.p.fee_floor))
        if T < 5*HOUR: base=max(base,int(ref_car*self.p.fee_late_2))
        base=min(base,int(ref_car*self.p.fee_cap))
        steps=[{'at':desk.now,'feeCents':base}]
        if self.p.timed_steps:
            return steps
        if self.p.short_step and T<8*HOUR:
            steps.append({'at':desk.now+30,'feeCents':max(base,int(ref_car*1.45))})
        if T>8*HOUR:
            steps.append({'at':order['lastPickupAt']-8*HOUR,'feeCents':max(base,int(ref_car*self.p.fee_late_1))})
        if T>5*HOUR:
            steps.append({'at':order['lastPickupAt']-5*HOUR,'feeCents':max(base,int(ref_car*self.p.fee_late_2))})
        # nondecreasing
        out=[]
        for s in steps:
            if out and s['feeCents']<out[-1]['feeCents']: s={**s,'feeCents':out[-1]['feeCents']}
            out.append(s)
        return out
    def urgent_schedule(self, desk, order, gid, mult=1.6):
        post=desk.posts[gid]
        req=desk.requests[order['id']]
        cars=pickups(post)
        fee=int(req['referenceFeeCents']*mult*cars/len(order['legs']))
        return [{'at':desk.now,'feeCents':max(1,fee)}]
    def on_win(self, desk, order):
        self.note_win(desk,order)
        req=desk.requests.get(order['id'])
        lead=order['lastPickupAt']-desk.now
        if req and req.get('batchId'):
            delay=self.p.group_delay_batch
        elif lead>12*HOUR: delay=self.p.group_delay_long
        elif lead>6*HOUR: delay=self.p.group_delay_mid
        else: delay=self.p.group_delay_short
        if self.p.group_epoch:
            if lead>self.p.threshold_long*HOUR: due=(desk.now//self.p.epoch_long+1)*self.p.epoch_long
            elif lead>self.p.threshold_mid*HOUR: due=(desk.now//self.p.epoch_mid+1)*self.p.epoch_mid
            else: due=desk.now+delay
        else: due=desk.now+delay
        desk.pending.append((due, order['id']))
    def on_deadline(self, desk, oid):
        order=desk.orders.get(oid)
        if order is None: return
        desk.urgent.add(oid); desk.send_post_of(oid)
        for gid in desk.gigs_of(oid):
            if gid not in desk.booked:
                desk.reprice(gid,self.urgent_schedule(desk,order,gid,1.55))
        self.send_offers(desk,order,1.55)
    def on_drop(self, desk, gid):
        post=desk.posts.get(gid)
        if not post: return
        order=desk.orders.get(post['stops'][0]['orderId'])
        if order:
            desk.reprice(gid,self.urgent_schedule(desk,order,gid,1.45))
            self.send_offers(desk,order,1.6)
    def initial_offers(self, desk, gid):
        post=desk.posts.get(gid)
        if not post: return
        order=desk.orders.get(post['stops'][0]['orderId'])
        if order is None: return
        T=order['lastPickupAt']-desk.now
        if T>12*HOUR: return
        req=desk.requests[order['id']]; a=req['legs'][0]['pickup']['placeId']
        fee=max(1,int(req['referenceFeeCents']*self.p.offer_initial))
        need_manual=post['audience'].get('manualLicence'); need_exp=post['audience'].get('experienced')
        byid={d['id']:d for d in self.roster}
        # 1) a driver who is already working and will be free near the pickup
        start=max(req['legs'][0]['pickup']['window']['from'],desk.now)
        work=[]
        for did,pos in desk.driver_pos.items():
            free=desk.driver_free.get(did,desk.now)
            if free>start-30: continue
            d=byid.get(did)
            if not d: continue
            if need_exp and not (d['rating']>=4.8 and d['completedGigs']>=50): continue
            if d['completedGigs'] and d['drops']/d['completedGigs']>0.10: continue
            m=MINUTES.get((pos,a),1e9)
            if m<=120: work.append((m,did))
        work.sort()
        sent=0
        for m,did in work[:3]:
            if desk.offer(gid,did,max(1,int(fee*self.p.offer_work_scale))): sent+=1
        if sent: return
        # 2) otherwise the nearest homes
        cand=[]
        for d in self.roster:
            if need_exp and not (d['rating']>=4.8 and d['completedGigs']>=50): continue
            if d['completedGigs'] and d['drops']/d['completedGigs']>0.10: continue
            m=MINUTES.get((a,d['homePlaceId']),1e9)
            if m<=90: cand.append((m,d['id']))
        cand.sort()
        for m,did in cand[:3]:
            desk.offer(gid,did,fee)
    def send_offers(self, desk, order, mult=1.6):
        gigs=[g for g in desk.gigs_of(order['id']) if g not in desk.booked]
        if not gigs: return
        req=desk.requests[order['id']]; a=req['legs'][0]['pickup']['placeId']
        fee=max(1,int(req['referenceFeeCents']*mult*len(order['legs'])/len(order['legs'])))
        # candidates near pickup, good history
        byid={d['id']:d for d in self.roster}
        seen=[]
        for at,did in desk.viewers.get(gigs[0],[])[-10:]:
            d=byid.get(did)
            if not d: continue
            if any(l['car']['premium'] for l in order['legs']) and not (d['rating']>=4.8 and d['completedGigs']>=50): continue
            if d['completedGigs'] and d['drops']/d['completedGigs']>0.15: continue
            seen.append((0,d['id']))
        cand=seen
        if not cand:
            for d in self.roster:
                if d['completedGigs'] and d['drops']/d['completedGigs']>0.10: continue
                if any(l['car']['premium'] for l in order['legs']) and not (d['rating']>=4.8 and d['completedGigs']>=50): continue
                m=MINUTES.get((a,d['homePlaceId']),1e9)
                if m<=120: cand.append((m,d['id']))
            cand.sort()
        sent=0
        for row in cand:
            did=row[-1]
            if sent>=3: break
            for gid in gigs:
                if desk.offer(gid,did,fee): sent+=1; break
            if sent>=3: break

class V1Desk(Desk):
    group_delay=3
    def __init__(self,*a,**kw):
        super().__init__(*a,**kw)
        self.pending=[]
        self.urgent_sent=set()
        self.quoted={}
        self.viewers=collections.defaultdict(list)
        self.stage={}
        self.refused_late=set()
    def initial_post(self, order):
        if order['id'] not in self.leg_gigs:
            self.pending.append((self.now, order['id']))
    def rejoin(self, snapshot, history):
        super().rejoin(snapshot, history)
        for g in snapshot['gigs']:
            if g['status']=='open': self.stage.setdefault(g['id'],0)
    def quote(self, req):
        self.requests[req['id']]=req
        prices=self.policy.quote(self,req)
        if prices is not None:
            self.quoted[req['id']]=prices
            self.outbox.append({'type':'quote','requestId':req['id'],'prices':prices})
            self.stats['quotes']+=1
    def read_results(self,actions,results):
        before=set(self.posts)
        super().read_results(actions,results)
        # A refused post: take the cars out and repost each as a late-open single.
        for a,r in zip(actions,results):
            if a['type']=='post' and not r.get('ok'):
                self.log.setdefault('refused_posts',[]).append({'at':self.now,'stops':a['stops'],'reason':r.get('reason')})
                ids={s['orderId'] for s in a['stops']}
                for oid in ids:
                    o=self.orders.get(oid)
                    if o and not self.gigs_of(oid) and oid not in self.refused_late:
                        self.refused_late.add(oid); self.post_late(o)
        for gid in set(self.posts)-before:
            self.stage.setdefault(gid,0)
            if self.policy.p.immediate_offers:
                self.policy.initial_offers(self,gid)
    def _late_fee(self, order, cars=1):
        req=self.requests[order['id']]
        ref=req['referenceFeeCents']*cars/len(order['legs'])
        return max(1,int(ref*1.9))
    def post_late(self, order):
        legs=undelivered(order)
        if not legs: return
        req=self.requests[order['id']]; nc=self.policy.nc(req)
        aud={'experienced':any(l['car']['premium'] for l in order['legs']),'manualLicence':bool(nc.get('manual'))}
        if len(legs)>1:
            for leg in legs:
                stops=without_windows(stops_for(order,[leg],req.get('note'),Retry(plates=bool(nc.get('plates')))),float('inf'))
                self.outbox.append({'type':'post','stops':stops,'audience':aud,
                                    'schedule':[{'at':self.now,'feeCents':self._late_fee(order,1)}]})
            return
        stops=without_windows(stops_for(order,legs,req.get('note'),Retry(plates=bool(nc.get('plates')))),float('inf'))
        self.outbox.append({'type':'post','stops':stops,'audience':aud,
                            'schedule':[{'at':self.now,'feeCents':self._late_fee(order,len(legs))}]})
    def repost(self,gid):
        a=self.posts.get(gid)
        if a is None: return
        ids={s['orderId'] for s in a['stops']}
        for s in a['stops']: self.leg_gigs.get(s['orderId'],{}).pop(s['leg'],None)
        for oid in ids:
            o=self.orders.get(oid)
            if o and not self.gigs_of(oid): self.post_late(o)
    def handle(self,e):
        k=e['kind']
        if k=='gig_viewed':
            self.viewers[e['gigId']].append((e['at'],e['driverId']))
        if k=='order_lost':
            self.policy.note_loss(self,e.get('requestId'))
        if k=='order_cancelled':
            self._cancel(e)
            return
        if k=='order_changed':
            self._changed(e)
            return
        super().handle(e)
        if k=='order_won': self.urgent_sent.discard(e['order']['id'])
    def _cancel(self,e):
        oid=e['orderId']
        for books in (self.orders,self.leg_gigs): books.pop(oid,None)
        for g in e.get('gigs',[]):
            gid=g['gigId']; post=self.posts.get(gid)
            if not post: continue
            others={s['orderId'] for s in post['stops'] if s['orderId']!=oid and s['orderId'] in self.orders}
            if g['outcome']=='withdrawn':
                for s in post['stops']: self.leg_gigs.get(s['orderId'],{}).pop(s['leg'],None)
                for other in others:
                    o=self.orders.get(other)
                    if o and not self.gigs_of(other): self.post_late(o)
            elif g['outcome'] in ('released','continues'):
                pass
    def _changed(self,e):
        if self.p.log_events: self.log['events'].append(e)
        order=e['order']
        old=self.orders.get(order['id'])
        self.orders[order['id']]=order
        gids=self.gigs_of(order['id'])
        if any(g in self.booked for g in gids):
            return
        # unbooked: take the whole gigs off, then repost every car on them
        orders=set()
        for gid in gids:
            post=self.posts.get(gid)
            if not post: continue
            for s in post['stops']:
                self.leg_gigs.get(s['orderId'],{}).pop(s['leg'],None)
                if s['orderId'] in self.orders: orders.add(s['orderId'])
            self.outbox.append({'type':'withdraw','gigId':gid})
        for oid in orders:
            o=self.orders.get(oid)
            if o and not self.gigs_of(oid): self.post_late(o)
    def next_due(self):
        ds=[d for d,_ in self.pending]
        if self.p.timed_steps:
            for gid,st in self.stage.items():
                if gid in self.booked: continue
                post=self.posts.get(gid)
                if not post: continue
                o=self.orders.get(post['stops'][0]['orderId'])
                if not o: continue
                t=self._stage_time(o,st+1)
                if t is not None and t>=self.now: ds.append(t)
        return min(ds, default=None)
    def _stage_time(self, order, stage):
        if stage<=0: return None
        if stage==1: return order['lastPickupAt']-8*HOUR
        if stage==2: return order['lastPickupAt']-5*HOUR
        return None
    def check_timers(self):
        if not self.p.timed_steps: return
        for gid,st in list(self.stage.items()):
            if gid in self.booked or st>=2: continue
            post=self.posts.get(gid)
            if not post: continue
            order=self.orders.get(post['stops'][0]['orderId'])
            if not order: continue
            t=self._stage_time(order,st+1)
            if t is None or self.now<t: continue
            req=self.requests[order['id']]
            mult=self.p.stage1_mult if st==0 else self.p.stage2_mult
            fee=int(req['referenceFeeCents']*mult*pickups(post)/len(order['legs']))
            self.reprice(gid,[{'at':self.now,'feeCents':max(1,fee)}])
            self.stage[gid]=st+1
    def flush_pending(self):
        if not self.pending: return
        now=self.now
        ready=[oid for due,oid in self.pending if due<=now]
        self.pending=[(d,o) for d,o in self.pending if d>now]
        if not ready: return
        orders=[self.orders[o] for o in ready if o in self.orders and o not in self.leg_gigs]
        if not orders: return
        self.post_orders(orders)
    def candidates_for(self, order):
        req=self.requests[order['id']]
        out=[]
        # use stops_for to get stop structure/notes, then build candidate windows
        nc=self.policy.nc(req)
        base=stops_for(order,list(range(len(order['legs']))),req['note'],Retry(plates=bool(nc.get('plates'))))
        for i in range(0,len(base),2):
            p=base[i]; d=base[i+1]
            leg=order['legs'][p['leg']]
            pfrom=max(leg['pickup']['window']['from'],self.now)
            if nc.get('pickup_after'): pfrom=max(pfrom,nc['pickup_after'])
            pw=p.get('window') or {'from':pfrom,'to':max(pfrom,order['lastPickupAt'])}
            if leg['pickup']['privateAddress']:
                pw=dict(leg['pickup']['window'])
                if nc.get('pickup_after'): pw['from']=max(pw['from'],nc['pickup_after'])
            dto=order['deadline']-self.p.deliver_buffer
            for kk in ('delivery_before','deadline_override'):
                if nc.get(kk): dto=min(dto,nc[kk])
            dw=d.get('window') or {'from':max(leg['delivery']['window']['from'],self.now),'to':max(self.now,dto)}
            if leg['delivery']['privateAddress']:
                dw=dict(leg['delivery']['window'])
            out.append({'orderId':order['id'],'leg':p['leg'],
                        'pickup':{'placeId':leg['pickup']['placeId'],'window':pw},
                        'delivery':{'placeId':leg['delivery']['placeId'],'window':dw},
                        'withPlates':bool(p.get('withPlates') or nc.get('plates')),
                        'withCharging':p.get('withCharging',False),
                        'ref':req['referenceFeeCents']/len(order['legs']),
                        'premium':leg['car']['premium'],
                        'manual':bool(nc.get('manual'))})
        return out
    def post_orders(self, orders):
        cands=[]; bycand={}; existing={}
        for o in orders:
            for c in self.candidates_for(o):
                key=(c['orderId'],c['leg']); bycand[key]=c; cands.append(c)
        if self.policy.p.merge_existing:
            for gid,post in self.posts.items():
                if gid in self.booked or len(post['stops'])!=2: continue
                s0,s1=post['stops']
                if s0['kind']!='pickup' or s1['kind']!='delivery': continue
                oid=s0['orderId']; o=self.orders.get(oid)
                if not o or any(x is not None for x in o['deliveredAt']): continue
                req=self.requests[oid]
                pw=s0.get('window') or {'from':max(o['lastPickupAt']-720,self.now),'to':o['lastPickupAt']}
                dw=s1.get('window') or {'from':self.now,'to':o['deadline']-self.policy.p.deliver_buffer}
                c={'orderId':oid,'leg':s0['leg'],
                   'pickup':{'placeId':o['legs'][s0['leg']]['pickup']['placeId'],'window':pw},
                   'delivery':{'placeId':o['legs'][s0['leg']]['delivery']['placeId'],'window':dw},
                   'withPlates':bool(s0.get('withPlates')),'withCharging':bool(s0.get('withCharging')),
                   'ref':req['referenceFeeCents']/len(o['legs']),'premium':o['legs'][s0['leg']]['car']['premium'],
                   'manual':bool(self.policy.nc(req).get('manual')),'gid':gid}
                existing[(oid,s0['leg'])]=gid
                cands.append(c)
        groups=[]
        if self.policy.p.combine and len(cands)>1:
            try:
                groups=combine.group(cands,self.now,max_cars=self.policy.p.combine_max_cars,min_saving=self.policy.p.combine_min_saving,max_drive_minutes=660,roads=ROADS,
                                     max_span_minutes=1440)
            except Exception:
                groups=[[c] for c in cands]
        else:
            groups=[[c] for c in cands]
        for g in groups:
            olds=[c.get('gid') for c in g if c.get('gid')]
            if olds and len(g)>1:
                for gid in olds:
                    if gid in self.booked: continue
                    post=self.posts.get(gid)
                    if post:
                        for st in post['stops']: self.leg_gigs.get(st['orderId'],{}).pop(st['leg'],None)
                    self.outbox.append({'type':'withdraw','gigId':gid})
            if len(g)==1 and g[0].get('gid'):
                continue
            self.post_group(g)
    def post_group(self, cands):
        if len(cands)==1:
            c=cands[0]; o=self.orders[c['orderId']]
            stops=combine.gig_stops([c],None,with_windows=True)
            req=self.requests[o['id']]
            aud={'experienced': c['premium'], 'manualLicence': bool(c.get('manual'))}
            self.outbox.append({'type':'post','stops':stops,'audience':aud,
                                'schedule':self.policy.fees(self,o,1)})
            self.stats['posts']+=1
            return
        plan=self.best_plan(cands)
        if plan is None:
            for c in cands: self.post(self.orders[c['orderId']],[c['leg']])
            return
        stops=combine.gig_stops(cands,plan,with_windows=True)
        self.stats['grouped_cars']=self.stats.get('grouped_cars',0)+len(cands)
        self.stats['group_gigs']=self.stats.get('group_gigs',0)+1
        ref=sum(c['ref'] for c in cands)
        first=self.orders[cands[0]['orderId']]
        aud={'experienced':any(c['premium'] for c in cands),'manualLicence':any(c.get('manual') for c in cands)}
        same_pickup=len({c['pickup']['placeId'] for c in cands})==1
        disc=self.policy.p.group_discount_batch if same_pickup else self.policy.p.group_discount
        base=ref*disc*self.policy.m0(self.requests[first['id']])*self.policy.fee_boost
        base=max(1,int(round(base)))
        deadline=min(self.orders[c['orderId']]['deadline'] for c in cands)
        T=deadline-self.now
        steps=[{'at':self.now,'feeCents':base}]
        if T>8*HOUR: steps.append({'at':deadline-8*HOUR,'feeCents':max(base,int(base*1.2))})
        if T>5*HOUR: steps.append({'at':deadline-5*HOUR,'feeCents':max(base,int(base*1.35))})
        self.outbox.append({'type':'post','stops':stops,'audience':aud,'schedule':steps})
        self.stats['posts']+=1
    def best_plan(self,cands):
        try:
            return combine.plan(ROADS,cands,self.now,max_drive_minutes=660,max_span_minutes=1440)
        except Exception:
            return None

def run(seed, level=8, overrides=None, tag='', save_reveal=True):
    c=Client(); w=c.create(level=level,seed=seed); snap=w.snapshot()
    from engine import Params
    p=Params()
    if overrides:
        for k,v in overrides.items(): setattr(p,k,v)
    pol=V1Policy(p,w.roster(),c.map(),seed)
    d=V1Desk(w,level,p,pol)
    d.now=snap['now']; ends=snap['endsAt']
    while d.now<ends:
        nxt=d.next_due()
        lim=min(ends,(d.now//DAY+1)*DAY, nxt if nxt is not None else ends)
        st=d.flush_advance(until=WAKE,max=lim)
        d.now=st['now']
        for e in st['events']: d.handle(e)
        d.check_timers()
        d.flush_pending()
        if d.now>=ends: break
    d.flush_pending()
    log=d.log; log['score']=w.score(); log['stats']=dict(d.stats); log['policy_stats']=dict(pol.stats); log['final_base']=pol.p.quote_base; log['world']=w.id
    if save_reveal:
        try: log['reveal']=w.reveal()
        except SpreadError: pass
    w.delete()
    return log

if __name__=='__main__':
    import sys
    seed=sys.argv[1] if len(sys.argv)>1 else 'study-1'
    t=time.time(); log=run(seed)
    print('score',log['score'],'stats',log['stats'],'secs',round(time.time()-t,1))
    json.dump(log,open(f'data/v1_{seed}.json','w'))
