# Spread dispatcher — team jaffa

Submission hash: **d9e2c-dca84-e7656-760ed**
Code: https://github.com/cappuch/spread-dispatcher

## Run the final

```sh
cd ~/bahnexpress
. .venv/bin/activate
caffeinate -i python dispatch.py --final --config config_final.json
```

Start it before Sunday 08:00. It waits for the final, then plays and exits at the end.
If it stops, run the same command again: it rejoins from snapshot + events.

## Ranking rounds

```sh
python dispatch.py --board --level 8 --config config_final.json
```

Play all five worlds of the round. A round counts only if all five are ranked
(each world at least 90% of won orders on time).

## Practice

```sh
python dispatch.py --level 8 --seed study-1 --config config_final.json
```

## What is here

- `engine.py` — world model: supply, pricing probes, note retry, scoring helpers.
- `v1.py` — the policy and desk: quotes, decline tests, fees, windows, combine, repair.
- `combine.py` — two/three-car route planner for one gig.
- `notes.py` — Finnish/Swedish/English note reader (times, hours, licence, plates, tyres).
- `dispatch.py` — entry point; practice, ranking and final modes.
- `config_final.json` — the parameters used in the final.
- `tune.py`, `jobs*.json`, `data/` — experiment harness and measured runs.
