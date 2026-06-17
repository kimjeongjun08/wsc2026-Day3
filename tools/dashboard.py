import subprocess, json, sys, time, threading, gzip, shlex
from datetime import datetime, timedelta
from collections import deque
from flask import Flask, jsonify, render_template

app = Flask(__name__)
REGION = "ap-northeast-2"
NS = "apdev"
WAF_LOG_GROUP = "aws-waf-logs-apdev"
ALB_LOG_BUCKET = "wsi2026-images-053c6633"
config = {"lb_arn": "", "user_tg": "", "product_tg": "", "stress_tg": "", "rds_id": "apdev-rds-instance", "webacl": "wsi2026-acl", "account": ""}
history = deque(maxlen=480)


def cw_get(ns, metric, dims, stat="Average", period=60):
    end = datetime.utcnow()
    start = end - timedelta(minutes=5)
    dim_args = ["--dimensions"] + [f"Name={k},Value={v}" for k, v in dims.items()]
    cmd = ["aws", "cloudwatch", "get-metric-statistics", "--namespace", ns, "--metric-name", metric,
           "--start-time", start.strftime("%Y-%m-%dT%H:%M:%S"), "--end-time", end.strftime("%Y-%m-%dT%H:%M:%S"),
           "--period", str(period), "--statistics", stat, "--region", REGION] + dim_args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        pts = sorted(json.loads(r.stdout).get("Datapoints", []), key=lambda x: x["Timestamp"])
        return pts[-1].get(stat, 0) if pts else 0
    except: return 0


def fetch_loop():
    while True:
        lb = config["lb_arn"]
        if not lb: time.sleep(2); continue
        m = {"ts": datetime.now().strftime("%H:%M:%S"), "epoch": time.time()}
        rds = {"DBInstanceIdentifier": config["rds_id"]}
        m["rds_cpu"] = cw_get("AWS/RDS", "CPUUtilization", rds)
        m["rds_conn"] = cw_get("AWS/RDS", "DatabaseConnections", rds, "Sum")
        m["rds_read_iops"] = round(cw_get("AWS/RDS", "ReadIOPS", rds), 1)
        m["rds_write_iops"] = round(cw_get("AWS/RDS", "WriteIOPS", rds), 1)
        m["rds_free_gb"] = round(cw_get("AWS/RDS", "FreeStorageSpace", rds) / 1e9, 2)
        m["rds_read_lat"] = round(cw_get("AWS/RDS", "ReadLatency", rds) * 1000, 2)
        m["rds_write_lat"] = round(cw_get("AWS/RDS", "WriteLatency", rds) * 1000, 2)
        m["alb_req"] = cw_get("AWS/ApplicationELB", "RequestCount", {"LoadBalancer": lb}, "Sum")
        m["alb_2xx"] = cw_get("AWS/ApplicationELB", "HTTPCode_ELB_2XX_Count", {"LoadBalancer": lb}, "Sum")
        m["alb_4xx"] = cw_get("AWS/ApplicationELB", "HTTPCode_ELB_4XX_Count", {"LoadBalancer": lb}, "Sum")
        m["alb_5xx"] = cw_get("AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count", {"LoadBalancer": lb}, "Sum")
        m["tgt_2xx"] = cw_get("AWS/ApplicationELB", "HTTPCode_Target_2XX_Count", {"LoadBalancer": lb}, "Sum")
        m["tgt_4xx"] = cw_get("AWS/ApplicationELB", "HTTPCode_Target_4XX_Count", {"LoadBalancer": lb}, "Sum")
        m["tgt_5xx"] = cw_get("AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", {"LoadBalancer": lb}, "Sum")
        for name, tg in [("user", config["user_tg"]), ("product", config["product_tg"]), ("stress", config["stress_tg"])]:
            if tg:
                tgd = {"TargetGroup": tg, "LoadBalancer": lb}
                m[f"{name}_rt"] = round(cw_get("AWS/ApplicationELB", "TargetResponseTime", tgd) * 1000, 1)
                m[f"{name}_req"] = cw_get("AWS/ApplicationELB", "RequestCount", tgd, "Sum")
                m[f"{name}_healthy"] = round(cw_get("AWS/ApplicationELB", "HealthyHostCount", tgd, "Maximum"))
                m[f"{name}_unhealthy"] = round(cw_get("AWS/ApplicationELB", "UnHealthyHostCount", tgd, "Maximum"))
                m[f"{name}_5xx"] = cw_get("AWS/ApplicationELB", "HTTPCode_Target_5XX_Count", tgd, "Sum")
                m[f"{name}_4xx"] = cw_get("AWS/ApplicationELB", "HTTPCode_Target_4XX_Count", tgd, "Sum")
        wacl = config["webacl"]
        if wacl:
            wd = {"WebACL": wacl, "Region": REGION, "Rule": "ALL"}
            m["waf_allowed"] = cw_get("AWS/WAFV2", "AllowedRequests", wd, "Sum")
            m["waf_blocked"] = cw_get("AWS/WAFV2", "BlockedRequests", wd, "Sum")
            m["waf_counted"] = cw_get("AWS/WAFV2", "CountedRequests", wd, "Sum")
        m["node_count"] = node_count()
        history.append(m)
        time.sleep(15)


def node_count():
    try:
        r = subprocess.run(["kubectl", "get", "nodes", "-o", "json"], capture_output=True, text=True, timeout=5)
        return len(json.loads(r.stdout).get("items", []))
    except: return 0


@app.route("/")
def index(): return render_template("index.html")

@app.route("/api/metrics")
def api_metrics(): return jsonify(list(history))

@app.route("/api/traffic")
def api_traffic():
    hist = list(history)
    if len(hist) < 3: return jsonify({"status": "collecting", "phases": []})
    totals = [h.get("alb_req", 0) for h in hist]
    total_reqs = sum(totals)
    total_errs = sum(h.get("alb_5xx",0)+h.get("tgt_5xx",0) for h in hist)

    # 구간별 분석 (5분 단위 = 20샘플)
    seg_size = 20
    phases = []
    for i in range(0, len(totals), seg_size):
        seg = totals[i:i+seg_size]
        if not seg: continue
        avg = sum(seg)/len(seg)
        peak = max(seg)
        mn = min(seg)
        variance = sum((x-avg)**2 for x in seg)/len(seg)
        std = variance**0.5

        # 이전 구간 대비
        prev_avg = phases[-1]["avg"] if phases else 0
        if prev_avg > 0:
            change = (avg - prev_avg) / prev_avg * 100
        else:
            change = 0

        # 패턴 판별
        if std > avg * 0.5 and peak > avg * 2:
            pattern = "SPIKE"
            desc = "급격한 트래픽 스파이크 발생"
        elif change > 50:
            pattern = "SURGE"
            desc = "트래픽 급증 구간"
        elif change > 20:
            pattern = "RAMP_UP"
            desc = "트래픽 점진적 증가"
        elif change < -30:
            pattern = "DROP"
            desc = "트래픽 급감"
        elif change < -10:
            pattern = "COOLING"
            desc = "트래픽 감소 추세"
        elif avg > 0 and std < avg * 0.15:
            pattern = "STEADY_HIGH" if avg > (sum(totals)/len(totals)) else "STEADY_LOW"
            desc = "높은 트래픽 유지" if "HIGH" in pattern else "낮은 트래픽 유지"
        else:
            pattern = "NORMAL"
            desc = "보통 수준"

        start_ts = hist[i]["ts"] if i < len(hist) else "?"
        end_ts = hist[min(i+seg_size-1, len(hist)-1)]["ts"]

        phases.append({
            "start": start_ts, "end": end_ts,
            "avg": round(avg, 1), "peak": round(peak, 1), "min": round(mn, 1),
            "std": round(std, 1), "change_pct": round(change, 1),
            "pattern": pattern, "desc": desc
        })

    # 전체 요약
    recent = totals[-10:]
    older = totals[-30:-10] if len(totals) >= 30 else totals[:10]
    avg_r = sum(recent)/len(recent) if recent else 0
    avg_o = sum(older)/len(older) if older else 1
    ratio = avg_r / max(avg_o, 1)
    if ratio > 2: overall = "SPIKE"
    elif ratio > 1.3: overall = "RAMP_UP"
    elif ratio < 0.5: overall = "DROP"
    elif ratio < 0.8: overall = "COOLING"
    else: overall = "STEADY"

    return jsonify({
        "overall_pattern": overall,
        "current": totals[-1] if totals else 0,
        "peak": max(totals), "avg": round(sum(totals)/len(totals), 1),
        "min": min(totals), "total_reqs": total_reqs, "total_errs": total_errs,
        "err_rate": round(total_errs/max(total_reqs,1)*100, 3),
        "trend_pct": round((ratio-1)*100, 1),
        "duration_min": round(len(totals)*15/60, 1),
        "samples": len(totals),
        "phases": phases
    })

@app.route("/api/pods")
def api_pods():
    try:
        r = subprocess.run(["kubectl", "get", "pods", "-n", NS, "-o", "json"], capture_output=True, text=True, timeout=5)
        pods = json.loads(r.stdout).get("items", [])
        out = []
        for p in pods:
            cs = (p["status"].get("containerStatuses") or [{}])[0]
            # restart cause: current waiting reason (CrashLoopBackOff) or last termination (OOMKilled/Error)
            reason = "-"
            w = cs.get("state", {}).get("waiting")
            if w and w.get("reason"): reason = w["reason"]
            else:
                lt = cs.get("lastState", {}).get("terminated")
                if lt and lt.get("reason"): reason = lt["reason"]
            out.append({"name": p["metadata"]["name"], "app": p["metadata"].get("labels", {}).get("app", "?"),
                        "status": p["status"]["phase"],
                        "restarts": cs.get("restartCount", 0),
                        "reason": reason})
        return jsonify(out)
    except: return jsonify([])

@app.route("/api/nodes")
def api_nodes():
    try:
        r = subprocess.run(["kubectl", "get", "nodes", "-o", "json"], capture_output=True, text=True, timeout=5)
        nodes = json.loads(r.stdout).get("items", [])
        return jsonify([{"name":n["metadata"]["name"],
                        "status":"Ready" if any(c["type"]=="Ready" and c["status"]=="True" for c in n["status"].get("conditions",[])) else "NotReady",
                        "cpu":n["status"]["capacity"]["cpu"],"memory":n["status"]["capacity"]["memory"],
                        "provisioner":"karpenter" if "karpenter.sh/nodepool" in n["metadata"].get("labels",{}) else "managed",
                        "pool":n["metadata"].get("labels",{}).get("karpenter.sh/nodepool","-"),
                        "type":n["metadata"].get("labels",{}).get("node.kubernetes.io/instance-type","-")}
                       for n in nodes])
    except: return jsonify([])

@app.route("/api/karpenter")
def api_karpenter():
    def ready(obj):
        return any(c.get("type")=="Ready" and c.get("status")=="True" for c in obj.get("status",{}).get("conditions",[]))
    def kget(kind):
        try:
            r = subprocess.run(["kubectl", "get", kind, "-o", "json"], capture_output=True, text=True, timeout=5)
            return json.loads(r.stdout).get("items", [])
        except: return []
    pools = []
    for p in kget("nodepools.karpenter.sh"):
        res = p.get("status",{}).get("resources",{})
        pools.append({
            "name": p["metadata"]["name"],
            "ready": ready(p),
            "nodes": res.get("nodes","0"), "cpu": res.get("cpu","-"), "memory": res.get("memory","-"),
            "cpu_limit": p.get("spec",{}).get("limits",{}).get("cpu","-"),
        })
    claims = []
    for c in kget("nodeclaims.karpenter.sh"):
        st, lbl = c.get("status",{}), c["metadata"].get("labels",{})
        conds = {x["type"]:x["status"] for x in st.get("conditions",[])}
        # provisioning progress: Launched -> Registered -> Initialized -> Ready
        stage = "Ready" if conds.get("Ready")=="True" else \
                "Initialized" if conds.get("Initialized")=="True" else \
                "Registered" if conds.get("Registered")=="True" else \
                "Launched" if conds.get("Launched")=="True" else "Pending"
        claims.append({
            "name": c["metadata"]["name"],
            "pool": lbl.get("karpenter.sh/nodepool","-"),
            "type": lbl.get("node.kubernetes.io/instance-type","-"),
            "zone": lbl.get("topology.kubernetes.io/zone","-"),
            "capacity": lbl.get("karpenter.sh/capacity-type","-"),
            "node": st.get("nodeName","-"),
            "stage": stage, "ready": conds.get("Ready")=="True",
        })
    return jsonify({"pools": pools, "claims": claims})

def logs_insights(query, minutes=30, wait_s=8):
    start = int(time.time()) - minutes * 60
    end = int(time.time())
    try:
        q = subprocess.run(["aws", "logs", "start-query", "--log-group-name", WAF_LOG_GROUP,
                            "--start-time", str(start), "--end-time", str(end),
                            "--query-string", query, "--region", REGION,
                            "--query", "queryId", "--output", "text"],
                           capture_output=True, text=True, timeout=10)
        qid = q.stdout.strip()
        if not qid or qid == "None": return []
        deadline = time.time() + wait_s
        while time.time() < deadline:
            time.sleep(1)
            r = subprocess.run(["aws", "logs", "get-query-results", "--query-id", qid,
                                "--region", REGION, "--output", "json"],
                               capture_output=True, text=True, timeout=10)
            d = json.loads(r.stdout)
            if d.get("status") == "Complete":
                return [{f["field"]: f["value"] for f in row} for row in d.get("results", [])]
        return []
    except: return []


@app.route("/api/waf")
def api_waf():
    wacl = config["webacl"]
    # per-rule blocked counts (last 5min window)
    rules = ["BlockSQLInjection", "BlockXSS", "BlockScanner"]
    per_rule = []
    for rl in rules:
        c = cw_get("AWS/WAFV2", "BlockedRequests", {"WebACL": wacl, "Region": REGION, "Rule": rl}, "Sum")
        per_rule.append({"rule": rl, "blocked": round(c)})
    # top blocked client IPs / URIs / terminating rules (last 30min, from logs)
    top_ips = logs_insights(
        'fields httpRequest.clientIp as ip | filter action="BLOCK" '
        '| stats count(*) as cnt by ip | sort cnt desc | limit 10')
    top_uris = logs_insights(
        'fields httpRequest.uri as uri | filter action="BLOCK" '
        '| stats count(*) as cnt by uri | sort cnt desc | limit 10')
    top_rules = logs_insights(
        'filter action="BLOCK" | stats count(*) as cnt by terminatingRuleId '
        '| sort cnt desc | limit 10')
    return jsonify({
        "per_rule": per_rule,
        "top_ips": [{"ip": r.get("ip", "?"), "cnt": int(r.get("cnt", 0))} for r in top_ips],
        "top_uris": [{"uri": r.get("uri", "?"), "cnt": int(r.get("cnt", 0))} for r in top_uris],
        "top_rules": [{"rule": r.get("terminatingRuleId", "?"), "cnt": int(r.get("cnt", 0))} for r in top_rules],
    })

def _account():
    if config.get("account"): return config["account"]
    try:
        r = subprocess.run(["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
                           capture_output=True, text=True, timeout=5)
        config["account"] = r.stdout.strip()
    except: pass
    return config.get("account", "")


def _alb_reason(elb_s, tgt_s, actions, error_reason, tgt):
    # derive human "why" from the ALB access-log fields
    if error_reason and error_reason != "-":
        return error_reason
    if elb_s == "403" and "waf" in (actions or ""):
        return "WAF blocked"
    if tgt_s.isdigit() and int(tgt_s) >= 500:
        return f"backend returned {tgt_s}"
    if tgt_s.isdigit() and int(tgt_s) >= 400:
        return f"backend returned {tgt_s}"
    if elb_s in ("502", "503", "504") and tgt in ("-", ""):
        return "target unavailable / no healthy target"
    if elb_s == "460":
        return "client closed connection"
    if elb_s == "463":
        return "malformed X-Forwarded-For"
    return f"ELB-level {elb_s}"


@app.route("/api/alb_errors")
def api_alb_errors():
    acct = _account()
    if not acct:
        return jsonify({"errors": [], "count": 0, "note": "account unresolved"})
    # newest objects under today's (UTC) ELB prefix; fall back to yesterday near midnight
    keys = []
    for d in (datetime.utcnow(), datetime.utcnow() - timedelta(days=1)):
        pfx = f"AWSLogs/{acct}/elasticloadbalancing/{REGION}/{d.strftime('%Y/%m/%d')}/"
        try:
            r = subprocess.run(["aws", "s3api", "list-objects-v2", "--bucket", ALB_LOG_BUCKET,
                                "--prefix", pfx, "--query", "sort_by(Contents,&LastModified)[-6:].Key",
                                "--output", "json"], capture_output=True, text=True, timeout=8)
            ks = json.loads(r.stdout) if r.stdout.strip() and r.stdout.strip() != "None" else []
            keys.extend(ks or [])
        except: pass
        if keys: break
    rows = []
    for key in keys[-6:]:
        try:
            r = subprocess.run(["aws", "s3", "cp", f"s3://{ALB_LOG_BUCKET}/{key}", "-"],
                               capture_output=True, timeout=12)
            data = gzip.decompress(r.stdout).decode("utf-8", "replace")
        except: continue
        for line in data.splitlines():
            try: f = shlex.split(line)
            except: continue
            if len(f) < 13: continue
            elb_s, tgt_s = f[8], f[9]
            err4or5 = (elb_s.isdigit() and int(elb_s) >= 400) or (tgt_s.isdigit() and int(tgt_s) >= 400)
            if not err4or5: continue
            req = f[12].split(" ")
            method = req[0] if req else "-"
            url = req[1] if len(req) > 1 else "-"
            actions = f[22] if len(f) > 22 else "-"
            error_reason = f[24] if len(f) > 24 else "-"
            tgt = f[4]
            rows.append({
                "time": f[1],
                "client": f[3].rsplit(":", 1)[0],
                "method": method, "url": url[:120],
                "elb_status": elb_s, "target_status": tgt_s,
                "target": tgt, "actions": actions,
                "rt": f[6],  # target_processing_time (-1 = no response)
                "reason": _alb_reason(elb_s, tgt_s, actions, error_reason, tgt),
            })
    rows.sort(key=lambda x: x["time"], reverse=True)
    # summary by status
    by_status = {}
    for x in rows:
        s = x["elb_status"]
        by_status[s] = by_status.get(s, 0) + 1
    return jsonify({"errors": rows[:100], "count": len(rows), "by_status": by_status})

@app.route("/api/top")
def api_top():
    try:
        r = subprocess.run(["kubectl", "top", "pods", "-n", NS, "--no-headers"], capture_output=True, text=True, timeout=5)
        pods = [{"name":p[0],"cpu":p[1],"memory":p[2]} for p in (l.split() for l in r.stdout.strip().split("\n") if l.strip()) if len(p)>=3]
        r2 = subprocess.run(["kubectl", "top", "nodes", "--no-headers"], capture_output=True, text=True, timeout=5)
        nodes = [{"name":p[0],"cpu":p[1],"cpu_pct":p[2],"memory":p[3],"mem_pct":p[4]} for p in (l.split() for l in r2.stdout.strip().split("\n") if l.strip()) if len(p)>=5]
        return jsonify({"pods": pods, "nodes": nodes})
    except: return jsonify({"pods":[],"nodes":[]})

@app.route("/api/hpa")
def api_hpa():
    try:
        r = subprocess.run(["kubectl", "get", "hpa", "-n", NS, "-o", "json"], capture_output=True, text=True, timeout=5)
        items = json.loads(r.stdout).get("items", [])
        out = []
        for h in items:
            spec, status = h.get("spec", {}), h.get("status", {})
            def util(metrics):
                for mt in metrics or []:
                    res = mt.get("resource", {})
                    if res.get("name") == "cpu":
                        if "current" in res:  # status side
                            return res["current"].get("averageUtilization")
                        return res.get("target", {}).get("averageUtilization")
                return None
            out.append({
                "name": h["metadata"]["name"],
                "ref": spec.get("scaleTargetRef", {}).get("name", "?"),
                "min": spec.get("minReplicas", 1),
                "max": spec.get("maxReplicas", 0),
                "current": status.get("currentReplicas", 0),
                "desired": status.get("desiredReplicas", 0),
                "cur_util": util(status.get("currentMetrics")),
                "target_util": util(spec.get("metrics")),
            })
        return jsonify(out)
    except: return jsonify([])

@app.route("/api/scale/<app_name>/<int:replicas>", methods=["POST"])
def api_scale(app_name, replicas):
    r = subprocess.run(["kubectl", "scale", "deploy", app_name, "-n", NS, f"--replicas={replicas}"],
                      capture_output=True, text=True, timeout=5)
    return jsonify({"ok": r.returncode==0, "msg": (r.stdout+r.stderr).strip()})

@app.route("/api/autoscale/<action>", methods=["POST"])
def api_autoscale(action):
    if action == "hpa_on":
        subprocess.run(["kubectl", "apply", "-f", "../terraform/k8s/hpa.yaml"], capture_output=True, timeout=5)
        return jsonify({"ok": True, "msg": "HPA applied"})
    elif action == "hpa_off":
        subprocess.run(["kubectl", "delete", "hpa", "--all", "-n", NS], capture_output=True, timeout=5)
        return jsonify({"ok": True, "msg": "HPA deleted"})
    elif action == "karpenter_on":
        subprocess.run(["kubectl", "apply", "-f", "../terraform/k8s/karpenter.yaml"], capture_output=True, timeout=5)
        return jsonify({"ok": True, "msg": "Karpenter active"})
    elif action == "karpenter_off":
        subprocess.run(["kubectl", "delete", "-f", "../terraform/k8s/karpenter.yaml", "--ignore-not-found"], capture_output=True, timeout=5)
        return jsonify({"ok": True, "msg": "Karpenter paused"})
    return jsonify({"ok": False, "msg": "unknown"})


def auto_detect():
    try:
        r = subprocess.run(["aws", "elbv2", "describe-load-balancers", "--query", "LoadBalancers[0].LoadBalancerArn",
                           "--output", "text", "--region", REGION], capture_output=True, text=True, timeout=5)
        config["lb_arn"] = r.stdout.strip().split("loadbalancer/")[1] if "loadbalancer/" in r.stdout else ""
    except: pass
    try:
        r = subprocess.run(["aws", "elbv2", "describe-target-groups", "--query", "TargetGroups[*].[TargetGroupName,TargetGroupArn]",
                           "--output", "json", "--region", REGION], capture_output=True, text=True, timeout=5)
        for name, arn in json.loads(r.stdout):
            suffix = "targetgroup/" + arn.split(":targetgroup/")[1] if ":targetgroup/" in arn else ""
            if "user" in name: config["user_tg"] = suffix
            elif "product" in name: config["product_tg"] = suffix
            elif "stress" in name: config["stress_tg"] = suffix
    except: pass
    try:
        r = subprocess.run(["aws", "wafv2", "list-web-acls", "--scope", "REGIONAL",
                           "--query", "WebACLs[0].Name", "--output", "text", "--region", REGION],
                          capture_output=True, text=True, timeout=5)
        n = r.stdout.strip()
        if n and n != "None": config["webacl"] = n
    except: pass


if __name__ == "__main__":
    print("\n=== Monitor Dashboard ===\n")
    lb = input("  ALB ARN suffix (Enter=auto): ").strip()
    if lb: config["lb_arn"] = lb
    else:
        auto_detect()
        print(f"  LB: {config['lb_arn']}")
    rds = input(f"  RDS ID [{config['rds_id']}]: ").strip()
    if rds: config["rds_id"] = rds
    threading.Thread(target=fetch_loop, daemon=True).start()
    print(f"\n  http://localhost:9090\n")
    app.run(host="0.0.0.0", port=9090, debug=False)