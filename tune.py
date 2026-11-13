import envload,json,sys,time,os
from concurrent.futures import ThreadPoolExecutor, as_completed
import v1

def one(item):
    tag,seed,cfg=item
    t=time.time()
    try:
        log=v1.run(seed,8,cfg,tag,save_reveal=False)
        sc=log['score']
        led={}
        for e in log['events']:
            if e['kind']=='ledger':
                k=e['line']['kind']; led[k]=led.get(k,0)+e['line']['cents']
        return {'tag':tag,'seed':seed,'cfg':cfg,'margin':sc['marginCents'],'rev':sc['revenueCents'],
                'won':sc['wonOrders'],'ontime':sc['onTimeShare'],'undel':sc['undeliveredShare'],'ranked':sc['ranked'],
                'ledger':led,'stats':log['stats'],'policy':log.get('policy_stats',{}),'secs':round(time.time()-t,1)}
    except Exception as e:
        return {'tag':tag,'seed':seed,'cfg':cfg,'error':repr(e),'secs':round(time.time()-t,1)}

def main():
    jobs=json.load(open(sys.argv[1]))
    items=[(j.get('tag',str(i)),j['seed'],j.get('cfg',{})) for i,j in enumerate(jobs)]
    out=[]
    with ThreadPoolExecutor(max_workers=min(7,len(items))) as ex:
        futs=[ex.submit(one,it) for it in items]
        for f in as_completed(futs):
            r=f.result(); out.append(r); print(json.dumps(r),flush=True)
    path=sys.argv[2] if len(sys.argv)>2 else 'data/tune_results.jsonl'
    with open(path,'a') as fh:
        for r in out: fh.write(json.dumps(r)+'\n')
if __name__=='__main__': main()
