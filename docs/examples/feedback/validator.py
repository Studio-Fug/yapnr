"""Parent-authored synthetic displacement evaluator; no native routing claims."""

import json
import math
import sys

p = json.load(open(sys.argv[1]))
cases = []
for ref, target in [("R5", (0, 0)), ("R6", (0, 2))]:
    d = math.dist(p[ref], target)
    cases.append({"id": ref + "-pad-proximity", "passed": d <= 0.25, "distance": d})
cases.append({"id": "Q1-displaced", "passed": p["Q1"][0] >= 2.5})
sizes = {"R5": (0.4, 0.4), "R6": (0.4, 0.4), "Q1": (2, 4)}
collisions = []
for a, b in [("R5", "R6"), ("R5", "Q1"), ("R6", "Q1")]:
    if all(abs(p[a][i] - p[b][i]) < (sizes[a][i] + sizes[b][i]) / 2 + 0.1 for i in range(2)):
        collisions.append([a, b])
cases.append({"id": "non-overlap", "passed": not collisions, "collisions": collisions})
print(json.dumps({"cases": cases}))
raise SystemExit(not all(c["passed"] for c in cases))
