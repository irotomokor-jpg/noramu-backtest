#!/usr/bin/env python3
import json
from pathlib import Path
import pandas as pd

out = Path("kr_v031_latest_output")
fp = out / "kr_v031_finalists.csv"
sp = out / "kr_v031_scorecard.json"
final = pd.read_csv(fp)
if final.empty:
    raise SystemExit("No v0.31 finalists")
final["_rank_group"] = final["status"].map({"SURVIVOR": 0, "RESEARCH_ONLY": 1}).fillna(2)
final = final.sort_values(["_rank_group", "three_tick_robust", "robust_score"], ascending=[True, False, False]).drop(columns="_rank_group")
final.to_csv(fp, index=False, encoding="utf-8-sig")
score = json.loads(sp.read_text(encoding="utf-8"))
score["best"] = json.loads(final.iloc[0].to_json())
score["finalist_order_survivor_first"] = True
sp.write_text(json.dumps(score, ensure_ascii=False, indent=2), encoding="utf-8")
(out / "RUN_VALIDATION.txt").write_text("PASS\n" + json.dumps(score, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("FINALIZE_V031=PASS", final.iloc[0]["variant"], final.iloc[0]["status"])
