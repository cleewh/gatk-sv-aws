"""Pull settled Cost Explorer numbers for the cohorts we tagged."""
import boto3
from datetime import datetime, timedelta, timezone

ce = boto3.client("ce", region_name="us-east-1")
end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
start = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")

print(f"Cost Explorer query for cohort tags, {start} to {end}")
print()

for cohort in [
    "gatk-sv-validation-2026q2-rerun-2026-05-25",
    "customer-sim-2026q2",
    "gatk-sv-validation-2026q2",  # historical
]:
    print(f"=== {cohort} ===")
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="DAILY",
            Metrics=["UnblendedCost"],
            Filter={"Tags": {"Key": "gatk-sv:cohort-id", "Values": [cohort]}},
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
        total = 0.0
        services = {}
        for r in resp.get("ResultsByTime", []):
            for g in r.get("Groups", []):
                cost = float(g["Metrics"]["UnblendedCost"]["Amount"])
                total += cost
                services[g["Keys"][0]] = services.get(g["Keys"][0], 0) + cost
        for svc, c in sorted(services.items(), key=lambda x: -x[1]):
            if c > 0.001:
                print(f"  {svc}: ${c:.4f}")
        print(f"  TOTAL: ${total:.4f}")
    except Exception as e:
        print(f"  ERROR: {e}")
    print()

print("Note: Cost Explorer settlement lag is typically 8-24 hours.")
