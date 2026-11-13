import envload,json,sys,time
import v1
cfg=json.loads(sys.argv[2]) if len(sys.argv)>2 else {}
seed=sys.argv[1]
t=time.time(); log=v1.run(seed,8,cfg)
print('CFG',cfg)
print('score',log['score'],'stats',log['stats'],'secs',round(time.time()-t,1))
json.dump(log,open(f"data/v1_{seed}_{cfg.get('tag','x')}.json",'w'))
