#!/bin/sh
# The demo, start to finish: a fresh 50/50 experiment, then rounds of simulated traffic. After each round Marshal
# measures, decides and moves the split. Watch it on http://localhost:8765 (python -m marshal_ab serve).
# Run from the repo root.  sh sandbox/demo.sh [rounds] [users-per-round] [guardrail]
# With a third argument "guardrail" the treatment also inflates a checkout_error metric, and Marshal must halt on it.
export UV_CACHE_DIR=E:/uv-cache PYTHONIOENCODING=utf-8
ROUNDS=${1:-6}; USERS=${2:-1000}; G=""; H=""; [ "$3" = guardrail ] && G="--guardrail" && H="--health"  # --health: Flagsmith shows the feature Unhealthy while Marshal holds
PY=.venv/Scripts/python.exe
$PY sandbox/seed_experiment.py --reset $G > /dev/null && mkdir -p .marshal && rm -f .marshal/decisions.jsonl
echo "fresh experiment, 50/50"
for i in $(seq 1 "$ROUNDS"); do
  uv run simulator/sim.py --mode ab --transport sdk --users "$USERS" --prefix "d${i}_" --seed "$i" $G > /dev/null
  $PY -m marshal_ab run --mode act $H | $PY -c "import sys,json; r=json.loads(sys.stdin.read()); print('round $i:', {k: round(v*100) for k,v in r['current'].items()}, '->', r['decision'], '| P(best)', {k: round(v,2) for k,v in r.get('chance_best',{}).items()})"
done
$PY -m marshal_ab segments device
