import envload,json,sys,time
import v1
seed=sys.argv[1]; cfg=json.loads(sys.argv[2]); out=sys.argv[3]
t=time.time(); log=v1.run(seed,8,cfg,'one',save_reveal=True)
print(log['score'], 'secs',round(time.time()-t,1))
json.dump(log,open(out,'w'))
