import json, os
path = "bandit_report.json"
if not os.path.exists(path):
    print("No report found")
    exit()
with open(path) as f:
    d = json.load(f)
results = d.get("results", [])
if not results:
    print("No issues found by bandit.")
else:
    for r in results:
        print(f"[{r['issue_severity']}/{r['issue_confidence']}] {r['issue_text']}")
        print(f"  File: {r['filename']}:{r['line_number']}")
        print(f"  CWE:  {r.get('issue_cwe', {})}")
        print()
