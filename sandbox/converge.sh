#!/bin/sh
# Rounds of fresh simulated traffic; after each, Marshal measures and moves the split. Run from the repo root.
export UV_CACHE_DIR=E:/uv-cache PYTHONIOENCODING=utf-8
for i in 1 2 3 4 5 6; do
  uv run simulator/sim.py --mode ab --transport sdk --users 1500 --prefix "e${i}_" --seed "$i" > /dev/null
  .venv/Scripts/python.exe -m marshal_ab run --mode act | python -c "import sys,json; r=json.loads(sys.stdin.read()); print(r['at'][11:], r['current'], r['decision'], {k:round(v,2) for k,v in r.get('chance_best',{}).items()}, r.get('verdicts',{}).get('treatment',[''])[0])"
done
