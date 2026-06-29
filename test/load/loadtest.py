"""
loadtest.py
채점기준 기반 부하 테스트 도구
- 실시간 대시보드 (rich)
- availability / performance / 비정상 요청 처리 / cost ratio 측정
- 1회차(완만) + 2회차(공격적) 30분 시나리오

사용법: python loadtest.py <endpoint> [--aws-region ap-northeast-2] [--cluster-name apdev-cluster]
"""
import asyncio
import aiohttp
import argparse
import time
import uuid
import random
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text
from rich import box
from rich.columns import Columns
from rich.rule import Rule

try:
    import boto3
    AWS_AVAILABLE = True
except ImportError:
    AWS_AVAILABLE = False

console = Console()

# ── 시나리오 정의 (초 단위) ──────────────────────────────────────────────
STAGES = [
    # 1회차: 완만한 증가 (0~15분) — 스케일아웃 관찰
    (120, 5),    # 2m → 5 VU   (한산)
    (120, 10),   # 2m → 10 VU
    (180, 30),   # 3m → 30 VU
    (180, 60),   # 3m → 60 VU
    (120, 80),   # 2m → 80 VU
    (180, 100),  # 3m → 100 VU (완만하게 최고점)
    # 2회차: 스파이크 → 고부하 유지 → 점진적 감소 (15~30분) — 스케일인 관찰
    (60,  250),  # 1m → 250 VU (급격한 스파이크)
    (180, 250),  # 3m → 250 유지 (고부하)
    (120, 200),  # 2m → 200 VU
    (120, 150),  # 2m → 150 VU (점진적 감소 시작)
    (120, 100),  # 2m → 100 VU
    (120, 50),   # 2m → 50 VU
    (120, 20),   # 2m → 20 VU
    (60,  5),    # 1m → 5 VU  (거의 없는 채로 마무리)
]

SLO = {
    "user":    {"avail": 5.0,  "perf": 0.2},
    "product": {"avail": 5.0,  "perf": 0.2},
    "stress":  {"avail": 5.0,  "perf": 1.0},
    "image":   {"avail": 5.0,  "perf": 5.0},
}

ATTACK_CATEGORIES = {
    "wrong_method":   "잘못된 HTTP 메소드",
    "missing_param":  "파라미터 누락/형식 오류",
    "sqli":           "SQL 인젝션",
    "bad_body":       "잘못된 body",
    "bad_header":     "비정상 헤더",
    "bad_ctype":      "잘못된 Content-Type",
}

# ── 데이터 컨테이너 ──────────────────────────────────────────────────────
@dataclass
class APIStats:
    latencies: list = field(default_factory=list)
    status_codes: dict = field(default_factory=lambda: defaultdict(int))
    errors: int = 0

    def record(self, status: int, latency_ms: float):
        self.latencies.append(latency_ms)
        self.status_codes[status] += 1
        if status == 0 or status >= 500:
            self.errors += 1

    @property
    def total(self): return len(self.latencies)

    @property
    def availability(self):
        if not self.total: return 0.0
        ok = sum(v for k, v in self.status_codes.items() if 200 <= k < 300)
        return 100.0 * ok / self.total

    def performance(self, slo_sec: float):
        if not self.total: return 0.0
        ok = sum(1 for l in self.latencies if l <= slo_sec * 1000)
        return 100.0 * ok / self.total

    def percentile(self, p: float):
        if not self.latencies: return 0.0
        s = sorted(self.latencies)
        idx = int(len(s) * p / 100)
        return s[min(idx, len(s)-1)]

    @property
    def avg(self):
        return statistics.mean(self.latencies) if self.latencies else 0.0


@dataclass
class AttackStats:
    category: str
    total: int = 0
    blocked: int = 0   # 403
    passed: int = 0    # 비403 (WAF miss)
    status_dist: dict = field(default_factory=lambda: defaultdict(int))

    def record(self, status: int):
        self.total += 1
        self.status_dist[status] += 1
        if status == 403:
            self.blocked += 1
        else:
            self.passed += 1

    @property
    def block_rate(self):
        return 100.0 * self.blocked / self.total if self.total else 0.0


class LoadTestState:
    def __init__(self):
        self.api_stats: dict[str, APIStats] = {k: APIStats() for k in SLO}
        self.attack_stats: dict[str, AttackStats] = {
            k: AttackStats(v) for k, v in ATTACK_CATEGORIES.items()
        }
        self.current_vus = 0
        self.target_vus = 0
        self.current_rps = 0.0
        self.start_time = time.time()
        self.phase = "대기"
        self.phase_num = 0
        self.rps_window: list = []   # (timestamp, count)
        self._lock = asyncio.Lock()
        self.instance_counts: list[int] = []   # 샘플링된 EC2 수
        self.base_instance_count: Optional[int] = None
        self.seed_user: Optional[str] = None
        self.seed_product: Optional[str] = None

    def elapsed(self): return time.time() - self.start_time

    def update_rps(self):
        now = time.time()
        self.rps_window.append(now)
        cutoff = now - 5.0
        self.rps_window = [t for t in self.rps_window if t >= cutoff]
        self.current_rps = len(self.rps_window) / 5.0


# ── 헬퍼 ─────────────────────────────────────────────────────────────────
def rid(): return str(random.randint(100000000000, 999999999999))
def uid(): return str(uuid.uuid4())
def json_headers(): return {"Content-Type": "application/json"}


def fmt_ms(ms: float) -> str:
    if ms < 1000: return f"{ms:.0f}ms"
    return f"{ms/1000:.2f}s"


def pct_color(v: float, thresholds=(90, 80, 50)) -> str:
    if v >= thresholds[0]: return "green"
    if v >= thresholds[1]: return "yellow"
    if v >= thresholds[2]: return "orange1"
    return "red"


# ── AWS 비용 조회 ─────────────────────────────────────────────────────────
def get_ec2_instance_count(region: str, cluster_name: str) -> int:
    if not AWS_AVAILABLE: return 0
    try:
        ec2 = boto3.client("ec2", region_name=region)
        resp = ec2.describe_instances(Filters=[
            {"Name": "tag:aws:eks:cluster-name", "Values": [cluster_name]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ])
        count = sum(len(r["Instances"]) for r in resp["Reservations"])
        return count
    except Exception:
        return 0


def calc_cost_ratio(state: LoadTestState, base_count: int) -> Optional[float]:
    if not state.instance_counts or base_count == 0: return None
    avg_count = statistics.mean(state.instance_counts)
    return avg_count / base_count


# ── 대시보드 렌더링 ────────────────────────────────────────────────────────
def render_dashboard(state: LoadTestState, base_count: int, region: str) -> Layout:
    elapsed = state.elapsed()
    mins, secs = divmod(int(elapsed), 60)

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="attack", size=20),
        Layout(name="footer", size=3),
    )
    layout["main"].split_row(
        Layout(name="api_col", ratio=2),
        Layout(name="side", ratio=1),
    )
    layout["api_col"].split_column(
        Layout(name="api", ratio=3),
        Layout(name="timeline", size=10),
    )

    # ── 헤더 ──
    phase_str = f"[cyan]{state.phase}[/cyan]"
    header_text = (
        f"[bold white]⚡ LoadTest[/bold white]  "
        f"경과: [yellow]{mins:02d}:{secs:02d}[/yellow]  "
        f"VU: [cyan]{state.current_vus}[/cyan] / [dim]{state.target_vus}[/dim]  "
        f"RPS: [green]{state.current_rps:.1f}[/green]  "
        f"페이즈: {phase_str}"
    )
    layout["header"].update(Panel(header_text, style="bold"))

    # ── API 통계 테이블 ──
    api_table = Table(box=box.ROUNDED, expand=True, show_header=True, header_style="bold cyan")
    api_table.add_column("API", width=9)
    api_table.add_column("요청수", justify="right", width=7)
    api_table.add_column("가용성%", justify="right", width=8)
    api_table.add_column("성능%", justify="right", width=8)
    api_table.add_column("avg", justify="right", width=8)
    api_table.add_column("P50", justify="right", width=8)
    api_table.add_column("P95", justify="right", width=8)
    api_table.add_column("P99", justify="right", width=8)
    api_table.add_column("2xx", justify="right", width=6)
    api_table.add_column("4xx", justify="right", width=6)
    api_table.add_column("5xx", justify="right", width=6)
    api_table.add_column("err", justify="right", width=6)

    for api, slo in SLO.items():
        st = state.api_stats[api]
        if st.total == 0:
            api_table.add_row(api, "0", "-", "-", "-", "-", "-", "-", "-", "-", "-", "-")
            continue

        avail = st.availability
        perf  = st.performance(slo["perf"])
        cnt_2xx = sum(v for k, v in st.status_codes.items() if 200 <= k < 300)
        cnt_4xx = sum(v for k, v in st.status_codes.items() if 400 <= k < 500)
        cnt_5xx = sum(v for k, v in st.status_codes.items() if k >= 500)

        api_table.add_row(
            f"[bold]{api}[/bold]",
            str(st.total),
            f"[{pct_color(avail)}]{avail:.1f}%[/]",
            f"[{pct_color(perf)}]{perf:.1f}%[/]",
            fmt_ms(st.avg),
            fmt_ms(st.percentile(50)),
            fmt_ms(st.percentile(95)),
            fmt_ms(st.percentile(99)),
            f"[green]{cnt_2xx}[/]",
            f"[yellow]{cnt_4xx}[/]",
            f"[red]{cnt_5xx}[/]",
            f"[red]{st.errors}[/]" if st.errors else "0",
        )

    layout["api"].update(Panel(api_table, title="[bold]API 성능[/bold]"))

    # ── 트래픽 타임라인 ──
    total_duration = sum(d for d, _ in STAGES)
    elapsed_clamped = min(elapsed, total_duration)
    bar_width = 80
    filled = int(bar_width * elapsed_clamped / total_duration)
    bar = "[green]" + "█" * filled + "[/green][dim]" + "░" * (bar_width - filled) + "[/dim]"

    # 현재 단계 누적 시간 계산
    phase_labels = []
    acc = 0
    round_markers = []
    for i, (dur, vu) in enumerate(STAGES):
        pct = acc / total_duration
        marker_pos = int(bar_width * pct)
        label = f"{acc//60}m"
        if i == len(STAGES) // 2:
            round_markers.append((marker_pos, "[cyan]2회차[/cyan]"))
        phase_labels.append((marker_pos, f"{vu}VU"))
        acc += dur

    # 단계별 VU 표시 (5단계마다)
    label_line = [" "] * bar_width
    for pos, lbl in phase_labels[::2]:
        clean = lbl.replace("VU", "")
        for j, ch in enumerate(clean):
            if pos + j < bar_width:
                label_line[pos + j] = ch

    # 경과 시간 / 전체 시간
    total_mins = total_duration // 60
    stage_info = f"단계 {state.phase_num}/{len(STAGES)}  |  경과 {mins:02d}:{secs:02d} / {total_mins:02d}:00"

    timeline_text = (
        bar + "\n"
        + "[dim]" + "".join(label_line) + "[/dim]\n"
        + f"[yellow]{stage_info}[/yellow]  페이즈: [cyan]{state.phase}[/cyan]"
    )
    layout["timeline"].update(Panel(timeline_text, title="[bold]트래픽 타임라인[/bold]"))

    # ── 사이드: 인스턴스 / 비용 ──
    side_table = Table(box=box.SIMPLE, expand=True, show_header=False)
    side_table.add_column("항목", style="cyan")
    side_table.add_column("값", justify="right")

    avg_inst = statistics.mean(state.instance_counts) if state.instance_counts else 0
    cost_ratio = calc_cost_ratio(state, base_count)

    side_table.add_row("기준 인스턴스", str(base_count))
    side_table.add_row("현재 평균 인스턴스", f"{avg_inst:.1f}")
    if cost_ratio is not None:
        cr_color = "green" if 0.5 <= cost_ratio <= 1.5 else ("yellow" if cost_ratio <= 2.5 else "red")
        side_table.add_row("Cost Ratio", f"[{cr_color}]{cost_ratio:.2f}[/]")
    else:
        side_table.add_row("Cost Ratio", "[dim]-[/dim]")

    side_table.add_row("", "")
    side_table.add_row("[bold]응답코드 분포[/bold]", "")
    all_codes: dict = defaultdict(int)
    for st in state.api_stats.values():
        for k, v in st.status_codes.items():
            all_codes[k] += v
    for code in sorted(all_codes):
        color = "green" if 200 <= code < 300 else ("yellow" if code < 500 else "red")
        side_table.add_row(f"  HTTP {code}", f"[{color}]{all_codes[code]}[/]")

    layout["side"].update(Panel(side_table, title="[bold]인스턴스 / 비용[/bold]"))

    # ── 비정상 트래픽 테이블 ──
    atk_table = Table(box=box.ROUNDED, expand=True, show_header=True, header_style="bold red")
    atk_table.add_column("공격 유형", width=22)
    atk_table.add_column("총 요청", justify="right", width=8)
    atk_table.add_column("차단(403)", justify="right", width=10)
    atk_table.add_column("통과", justify="right", width=8)
    atk_table.add_column("차단율", justify="right", width=8)
    atk_table.add_column("응답코드 분포", width=35)

    total_atk = sum(a.total for a in state.attack_stats.values())
    total_blocked = sum(a.blocked for a in state.attack_stats.values())
    total_block_rate = 100.0 * total_blocked / total_atk if total_atk else 0

    for cat, ast in state.attack_stats.items():
        if ast.total == 0:
            atk_table.add_row(ast.category, "0", "-", "-", "-", "-")
            continue
        br = ast.block_rate
        dist = ", ".join(f"{k}:{v}" for k, v in sorted(ast.status_dist.items()))
        atk_table.add_row(
            ast.category,
            str(ast.total),
            f"[green]{ast.blocked}[/]",
            f"[red]{ast.passed}[/]" if ast.passed else "0",
            f"[{pct_color(br)}]{br:.1f}%[/]",
            dist,
        )

    atk_table.add_section()
    atk_table.add_row(
        "[bold]합계[/bold]",
        f"[bold]{total_atk}[/bold]",
        f"[bold green]{total_blocked}[/bold green]",
        f"[bold red]{total_atk - total_blocked}[/bold red]",
        f"[bold {pct_color(total_block_rate)}]{total_block_rate:.1f}%[/bold {pct_color(total_block_rate)}]",
        "",
    )
    layout["attack"].update(Panel(atk_table, title="[bold red]비정상 트래픽 처리[/bold red]"))

    # ── 푸터 ──
    slo_parts = []
    for api, slo in SLO.items():
        st = state.api_stats[api]
        perf = st.performance(slo["perf"])
        avail = st.availability
        slo_parts.append(f"{api}: avail=[{pct_color(avail)}]{avail:.0f}%[/] perf=[{pct_color(perf)}]{perf:.0f}%[/]")
    layout["footer"].update(Panel("  ".join(slo_parts), title="SLO 요약"))

    return layout


# 1x1 PNG (이미지 업로드용 최소 바이너리)
TINY_PNG = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
    b'\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00'
    b'\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00'
    b'\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82'
)


# ── 요청 워커 ─────────────────────────────────────────────────────────────
async def normal_worker(session: aiohttp.ClientSession, base: str, state: LoadTestState, stop_event: asyncio.Event):
    while not stop_event.is_set():
        r = random.random()
        t0 = time.time()
        try:
            if r < 0.30:
                # user POST + GET
                ru, uu = rid(), uid()
                uname = f"lt_{ru}"
                email = f"{uname}@example.org"
                async with session.post(f"{base}/v1/user",
                    json={"requestid": ru, "uuid": uu, "username": uname, "email": email},
                    headers=json_headers()) as resp:
                    await resp.read()
                    lat = (time.time() - t0) * 1000
                    state.api_stats["user"].record(resp.status, lat)

                await asyncio.sleep(0.05)
                t0 = time.time()
                async with session.get(f"{base}/v1/user",
                    params={"email": email, "requestid": rid(), "uuid": uid()},
                    headers=json_headers()) as resp:
                    await resp.read()
                    lat = (time.time() - t0) * 1000
                    state.api_stats["user"].record(resp.status, lat)

            elif r < 0.60:
                # product POST + GET
                ru, uu = rid(), uid()
                pid = f"lt_{ru}"
                async with session.post(f"{base}/v1/product",
                    json={"requestid": ru, "uuid": uu, "id": pid, "name": pid,
                          "price": random.randint(100, 99999)},
                    headers=json_headers()) as resp:
                    await resp.read()
                    lat = (time.time() - t0) * 1000
                    state.api_stats["product"].record(resp.status, lat)

                await asyncio.sleep(0.05)
                t0 = time.time()
                async with session.get(f"{base}/v1/product",
                    params={"id": pid, "requestid": rid(), "uuid": uid()},
                    headers=json_headers()) as resp:
                    await resp.read()
                    lat = (time.time() - t0) * 1000
                    state.api_stats["product"].record(resp.status, lat)

            elif r < 0.80:
                # stress
                async with session.post(f"{base}/v1/stress",
                    json={"requestid": rid(), "uuid": uid(), "length": 256},
                    headers=json_headers()) as resp:
                    await resp.read()
                    lat = (time.time() - t0) * 1000
                    state.api_stats["stress"].record(resp.status, lat)

            else:
                # product PUT (이미지 업로드) + GET /images/ (다운로드)
                ru, uu = rid(), uid()
                pid = f"lt_{ru}"

                # product 먼저 생성
                async with session.post(f"{base}/v1/product",
                    json={"requestid": rid(), "uuid": uid(), "id": pid, "name": pid, "price": 999},
                    headers=json_headers()) as resp:
                    await resp.read()

                await asyncio.sleep(0.1)

                # PUT 이미지 업로드 측정
                t0 = time.time()
                form = aiohttp.FormData()
                form.add_field("requestid", ru)
                form.add_field("uuid", uu)
                form.add_field("id", pid)
                form.add_field("image", TINY_PNG, filename=f"{pid}.png", content_type="image/png")
                async with session.put(f"{base}/v1/product", data=form) as resp:
                    await resp.read()
                    put_status = resp.status
                    lat = (time.time() - t0) * 1000
                    state.api_stats["product"].record(put_status, lat)

                # 업로드 성공 시 이미지 다운로드 측정
                if put_status in (200, 201):
                    await asyncio.sleep(1.0)  # CF 전파 대기
                    t0 = time.time()
                    async with session.get(f"{base}/images/{pid}.png") as resp:
                        await resp.read()
                        lat = (time.time() - t0) * 1000
                        state.api_stats["image"].record(resp.status, lat)

        except Exception:
            lat = (time.time() - t0) * 1000
            if r < 0.30:
                state.api_stats["user"].record(0, lat)
            elif r < 0.60:
                state.api_stats["product"].record(0, lat)
            elif r < 0.80:
                state.api_stats["stress"].record(0, lat)
            else:
                state.api_stats["image"].record(0, lat)

        state.update_rps()
        await asyncio.sleep(random.uniform(0.1, 0.3))


async def abnormal_worker(session: aiohttp.ClientSession, base: str, state: LoadTestState, stop_event: asyncio.Event):
    """비정상 트래픽 고정 3 req/s"""
    while not stop_event.is_set():
        cat, fn = random.choice(build_attacks(base))
        t0 = time.time()
        try:
            status, _ = await fn(session)
        except Exception:
            status = 0
        state.attack_stats[cat].record(status)
        await asyncio.sleep(random.uniform(0.2, 0.5))


def build_attacks(base: str):
    """(category, async_fn) 리스트"""
    H = json_headers

    async def req(session, method, url, **kwargs):
        async with session.request(method, url, **kwargs) as r:
            await r.read()
            return r.status, r

    attacks = []

    # 잘못된 메소드
    for method, path in [("DELETE", "/v1/user"), ("PATCH", "/v1/product"),
                          ("OPTIONS", "/v1/stress"), ("PUT", "/v1/user"), ("PUT", "/v1/stress")]:
        url = f"{base}{path}"
        attacks.append(("wrong_method",
            lambda s, m=method, u=url: req(s, m, u, headers=H(), json={"requestid": rid(), "uuid": uid()})))

    # 파라미터 누락/오류
    attacks += [
        ("missing_param", lambda s: req(s, "GET", f"{base}/v1/user", params={"requestid": rid(), "uuid": uid()})),
        ("missing_param", lambda s: req(s, "GET", f"{base}/v1/product", params={"requestid": rid(), "uuid": uid()})),
        ("missing_param", lambda s: req(s, "GET", f"{base}/v1/user", params={"email": "a@b.com", "requestid": "abc", "uuid": uid()})),
        ("missing_param", lambda s: req(s, "GET", f"{base}/v1/product", params={"id": "123", "requestid": "hack", "uuid": uid()})),
        ("missing_param", lambda s: req(s, "GET", f"{base}/v1/user", params={"email": "a@b.com", "requestid": rid(), "uuid": "not-a-uuid"})),
        ("missing_param", lambda s: req(s, "GET", f"{base}/v1/user", params={"email": "a@b.com", "requestid": rid(), "uuid": uid(), "extra": "bad"})),
    ]

    # SQLi
    attacks += [
        ("sqli", lambda s: req(s, "POST", f"{base}/v1/user", headers=H(),
            json={"requestid": "1 OR 1=1", "uuid": uid(), "username": f"bad_{rid()}", "email": f"bad_{rid()}@x.com"})),
        ("sqli", lambda s: req(s, "POST", f"{base}/v1/product", headers=H(),
            json={"requestid": "DROP TABLE", "uuid": "bad", "id": f"bad_{rid()}", "name": "x", "price": 1})),
        ("sqli", lambda s: req(s, "GET", f"{base}/v1/user",
            params={"email": "' OR '1'='1", "requestid": rid(), "uuid": uid()})),
    ]

    # 잘못된 body
    attacks += [
        ("bad_body", lambda s: req(s, "POST", f"{base}/v1/user", headers=H(), json={"foo": "bar"})),
        ("bad_body", lambda s: req(s, "POST", f"{base}/v1/stress", headers=H(), json={"wrong": "data"})),
        ("bad_body", lambda s: req(s, "POST", f"{base}/v1/user", headers=H(), data="not json")),
        ("bad_body", lambda s: req(s, "POST", f"{base}/v1/stress", headers=H(), data="")),
    ]

    # 비정상 헤더
    attacks += [
        ("bad_header", lambda s: req(s, "GET", f"{base}/v1/user",
            headers={**H(), "X-Attack": "payload"},
            params={"email": "a@b.com", "requestid": rid(), "uuid": uid()})),
        ("bad_header", lambda s: req(s, "POST", f"{base}/v1/user",
            headers={**H(), "X-Hacker": "true"},
            json={"requestid": rid(), "uuid": uid(), "username": f"bad_{rid()}", "email": f"bad_{rid()}@x.com"})),
        ("bad_header", lambda s: req(s, "POST", f"{base}/v1/stress",
            headers={**H(), "Authorization": "Bearer fake"},
            json={"requestid": rid(), "uuid": uid(), "length": 256})),
    ]

    # 잘못된 Content-Type
    attacks += [
        ("bad_ctype", lambda s: req(s, "POST", f"{base}/v1/user",
            headers={"Content-Type": "text/html"},
            json={"requestid": rid(), "uuid": uid(), "username": f"bad_{rid()}", "email": f"bad_{rid()}@x.com"})),
        ("bad_ctype", lambda s: req(s, "POST", f"{base}/v1/stress",
            headers={"Content-Type": "application/xml"},
            json={"requestid": rid(), "uuid": uid(), "length": 256})),
    ]

    return attacks


# ── VU 스케줄러 ────────────────────────────────────────────────────────────
async def vu_scheduler(base: str, state: LoadTestState, stop_event: asyncio.Event, all_done: asyncio.Event):
    """STAGES에 따라 VU 수 조정"""
    active_tasks: list[asyncio.Task] = []
    connector = aiohttp.TCPConnector(limit=500)
    session = aiohttp.ClientSession(connector=connector,
                                    timeout=aiohttp.ClientTimeout(total=10))

    # 비정상 트래픽 워커 (5분 후 시작, 고정 8개)
    stop_abnormal = asyncio.Event()
    abnormal_tasks = []

    async def delayed_abnormal():
        await asyncio.sleep(300)  # 5분 대기
        for _ in range(8):
            abnormal_tasks.append(
                asyncio.create_task(abnormal_worker(session, base, state, stop_abnormal))
            )

    asyncio.create_task(delayed_abnormal())

    phase_names_1 = ["한산", "한산", "증가", "증가", "증가", "증가"]
    phase_names_2 = ["스파이크", "고부하유지", "감소", "감소", "감소", "감소", "감소", "마무리"]
    all_phase_names = phase_names_1 + phase_names_2

    for idx, (duration, target_vu) in enumerate(STAGES):
        is_round2 = idx >= len(phase_names_1)
        round_str = "2회차" if is_round2 else "1회차"
        phase_name = all_phase_names[idx]
        state.phase = f"{round_str} {phase_name} → {target_vu}VU"
        state.phase_num = idx + 1
        state.target_vus = target_vu

        # VU 수 조정
        current = len(active_tasks)
        if target_vu > current:
            for _ in range(target_vu - current):
                t = asyncio.create_task(normal_worker(session, base, state, stop_event))
                active_tasks.append(t)
        elif target_vu < current:
            to_cancel = current - target_vu
            for t in active_tasks[:to_cancel]:
                t.cancel()
            active_tasks = active_tasks[to_cancel:]

        state.current_vus = len(active_tasks)

        # 단계 실행
        await asyncio.sleep(duration)

    # 종료
    stop_abnormal.set()
    stop_event.set()
    for t in active_tasks + abnormal_tasks:
        t.cancel()
    await session.close()
    all_done.set()


# ── EC2 샘플링 ────────────────────────────────────────────────────────────
async def ec2_sampler(state: LoadTestState, region: str, cluster: str, stop: asyncio.Event):
    while not stop.is_set():
        count = await asyncio.get_event_loop().run_in_executor(
            None, get_ec2_instance_count, region, cluster)
        if count > 0:
            state.instance_counts.append(count)
        await asyncio.sleep(10)


# ── 최종 리포트 ────────────────────────────────────────────────────────────
ATTACK_DETAILS = {
    "wrong_method":  [
        ("DELETE",  "/v1/user",    "-", "-"),
        ("PATCH",   "/v1/product", "-", "-"),
        ("OPTIONS", "/v1/stress",  "-", "-"),
        ("PUT",     "/v1/user",    "-", "-"),
        ("PUT",     "/v1/stress",  "-", "-"),
    ],
    "missing_param": [
        ("GET", "/v1/user",    "email 없음",          "requestid, uuid만 포함"),
        ("GET", "/v1/product", "id 없음",             "requestid, uuid만 포함"),
        ("GET", "/v1/user",    "requestid 형식 오류", "requestid=abc (숫자 아님)"),
        ("GET", "/v1/product", "requestid 형식 오류", "requestid=hack"),
        ("GET", "/v1/user",    "uuid 형식 오류",      "uuid=not-a-uuid"),
        ("GET", "/v1/user",    "불필요한 파라미터",   "extra=bad 추가"),
    ],
    "sqli": [
        ("POST", "/v1/user",    "requestid", "1 OR 1=1"),
        ("POST", "/v1/product", "requestid", "DROP TABLE  /  uuid=bad"),
        ("GET",  "/v1/user",    "email",     "' OR '1'='1"),
    ],
    "bad_body": [
        ("POST", "/v1/user",   "body", '{"foo": "bar"}'),
        ("POST", "/v1/stress", "body", '{"wrong": "data"}'),
        ("POST", "/v1/user",   "body", "not json (plain text)"),
        ("POST", "/v1/stress", "body", "(빈 body)"),
    ],
    "bad_header": [
        ("GET",  "/v1/user",   "X-Attack",      "payload"),
        ("POST", "/v1/user",   "X-Hacker",      "true"),
        ("POST", "/v1/stress", "Authorization", "Bearer fake"),
    ],
    "bad_ctype": [
        ("POST", "/v1/user",   "Content-Type", "text/html"),
        ("POST", "/v1/stress", "Content-Type", "application/xml"),
    ],
}


def _calc_scores(state, cost_ratio, total_block_rate):
    """채점기준표 그대로 점수 계산"""
    scores = {}

    # 1. 비정상 요청 처리 (4점)
    # image 처리율 >= 90/85/80/50% 각 0.5pt
    img = state.api_stats["image"]
    img_perf = img.performance(SLO["image"]["perf"])
    img_score = sum(0.5 for th in [90, 85, 80, 50] if img_perf >= th)
    # 비정상 요청 처리율 >= 90/85/80/50% 각 0.5pt
    atk_score = sum(0.5 for th in [90, 85, 80, 50] if total_block_rate >= th)
    scores["1_비정상요청처리"] = {"image": img_score, "waf": atk_score, "total": img_score + atk_score, "max": 4.0}

    # 2. 고가용성 및 안정성 (12점)
    # user/product/stress 가용성(5초 이내) >= 90/87.5/85/82.5/80/70/50/30% 각 0.5pt
    avail_scores = {}
    for api in ["user", "product", "stress"]:
        st = state.api_stats[api]
        avail = st.availability
        avail_scores[api] = sum(0.5 for th in [90, 87.5, 85, 82.5, 80, 70, 50, 30] if avail >= th)
    scores["2_고가용성"] = {**avail_scores, "total": sum(avail_scores.values()), "max": 12.0}

    # 3. 성능 효율성 (12점)
    # user(0.2s)/product(0.2s)/stress(1.0s) SLO >= 90/87.5/85/82.5/80/70/50/30% 각 0.5pt
    perf_scores = {}
    for api, slo in [("user", 0.2), ("product", 0.2), ("stress", 1.0)]:
        st = state.api_stats[api]
        perf = st.performance(slo)
        perf_scores[api] = sum(0.5 for th in [90, 87.5, 85, 82.5, 80, 70, 50, 30] if perf >= th)
    scores["3_성능효율성"] = {**perf_scores, "total": sum(perf_scores.values()), "max": 12.0}

    # 4. 비용 최적화 (12점)
    # 모든 api performance 30% 이상 전제조건
    perf_prereq = all(
        state.api_stats[api].performance(slo) >= 30
        for api, slo in [("user", 0.2), ("product", 0.2), ("stress", 1.0)]
    )
    if cost_ratio and perf_prereq:
        cost_score = sum(1.0 for upper in [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.25, 3.5, 3.75]
                         if 0.5 <= cost_ratio <= upper)
    else:
        cost_score = 0.0
    scores["4_비용최적화"] = {"score": cost_score, "prereq": perf_prereq, "total": cost_score, "max": 12.0}

    scores["grand_total"] = sum(s["total"] for s in scores.values())
    return scores


def print_final_report(state: LoadTestState, base_count: int):
    console.print(Rule("[bold yellow]최종 결과 리포트[/bold yellow]"))

    total_atk = sum(a.total for a in state.attack_stats.values())
    total_blocked = sum(a.blocked for a in state.attack_stats.values())
    total_block_rate = 100.0 * total_blocked / total_atk if total_atk else 0.0
    cost_ratio = calc_cost_ratio(state, base_count)

    scores = _calc_scores(state, cost_ratio, total_block_rate)

    # ── API 성능 테이블 ──
    t = Table(title="API 성능", box=box.DOUBLE_EDGE, header_style="bold cyan")
    t.add_column("API");      t.add_column("총 요청", justify="right")
    t.add_column("가용성",    justify="right"); t.add_column("성능(SLO)", justify="right")
    t.add_column("SLO기준",   justify="center")
    t.add_column("avg",       justify="right"); t.add_column("P50", justify="right")
    t.add_column("P95",       justify="right"); t.add_column("P99", justify="right")
    t.add_column("2xx",       justify="right"); t.add_column("5xx", justify="right")
    t.add_column("가용 점수", justify="right"); t.add_column("성능 점수", justify="right")

    for api, slo in SLO.items():
        st = state.api_stats[api]
        avail = st.availability
        perf  = st.performance(slo["perf"])
        cnt_2xx = sum(v for k,v in st.status_codes.items() if 200<=k<300)
        cnt_5xx = sum(v for k,v in st.status_codes.items() if k>=500)
        avail_sc = scores["2_고가용성"].get(api, "-")
        perf_sc  = scores["3_성능효율성"].get(api, "-")
        t.add_row(
            api, str(st.total),
            f"[{pct_color(avail)}]{avail:.2f}%[/]",
            f"[{pct_color(perf)}]{perf:.2f}%[/]",
            f"≤{slo['perf']}s",
            fmt_ms(st.avg), fmt_ms(st.percentile(50)),
            fmt_ms(st.percentile(95)), fmt_ms(st.percentile(99)),
            f"[green]{cnt_2xx:,}[/]",
            f"[red]{cnt_5xx:,}[/]" if cnt_5xx else "0",
            f"{avail_sc:.1f}pt / 4.0pt" if isinstance(avail_sc, float) else str(avail_sc),
            f"{perf_sc:.1f}pt / 4.0pt"  if isinstance(perf_sc,  float) else str(perf_sc),
        )
    console.print(t)

    # ── 비정상 트래픽 테이블 ──
    t2 = Table(title="비정상 요청 처리 (WAF)", box=box.DOUBLE_EDGE, header_style="bold red")
    t2.add_column("공격 유형"); t2.add_column("총 요청", justify="right")
    t2.add_column("차단(403)", justify="right"); t2.add_column("통과", justify="right")
    t2.add_column("차단율",    justify="right"); t2.add_column("응답코드 분포")
    for cat, ast in state.attack_stats.items():
        br   = ast.block_rate
        dist = ", ".join(f"HTTP {k}:{v}건" for k,v in sorted(ast.status_dist.items()))
        t2.add_row(ast.category, str(ast.total),
                   f"[green]{ast.blocked}[/]",
                   f"[red]{ast.passed}[/]" if ast.passed else "0",
                   f"[{pct_color(br)}]{br:.1f}%[/]", dist)
    t2.add_section()
    t2.add_row("[bold]합계[/bold]", str(total_atk),
               f"[bold green]{total_blocked}[/bold green]",
               f"[bold red]{total_atk-total_blocked}[/bold red]",
               f"[{pct_color(total_block_rate)}]{total_block_rate:.1f}%[/]", "")
    console.print(t2)

    # ── 비용 ──
    avg_inst_str  = f"{statistics.mean(state.instance_counts):.1f}" if state.instance_counts else "N/A"
    cost_ratio_str = f"{cost_ratio:.3f}" if cost_ratio else "N/A"
    prereq_str = "[green]충족[/green]" if scores["4_비용최적화"]["prereq"] else "[red]미충족[/red]"
    console.print(Panel(
        f"기준 인스턴스: [cyan]{base_count}[/cyan]대\n"
        f"평균 인스턴스: [cyan]{avg_inst_str}[/cyan]대  (샘플 {len(state.instance_counts)}회)\n"
        f"Cost Ratio: [cyan]{cost_ratio_str}[/cyan]\n"
        f"성능 전제조건(30%+): {prereq_str}\n"
        f"비용 점수: [bold]{scores['4_비용최적화']['total']:.0f}pt / 12pt[/bold]",
        title="[bold]비용 최적화[/bold]"
    ))

    # ── 채점 총합 (채점기준표 그대로) ──
    score_table = Table(title="채점 예상 (40점 만점)", box=box.DOUBLE_EDGE, header_style="bold yellow")
    score_table.add_column("항목"); score_table.add_column("배점", justify="right")
    score_table.add_column("획득", justify="right"); score_table.add_column("세부")
    s1 = scores["1_비정상요청처리"]
    s2 = scores["2_고가용성"]
    s3 = scores["3_성능효율성"]
    s4 = scores["4_비용최적화"]
    c1 = pct_color(s1["total"]/4*100)
    c2 = pct_color(s2["total"]/12*100)
    c3 = pct_color(s3["total"]/12*100)
    c4 = pct_color(s4["total"]/12*100)
    score_table.add_row("1. 비정상 요청 처리", "4pt",
        f"[{c1}]{s1['total']:.1f}pt[/{c1}]",
        f"image {s1['image']:.1f}pt + WAF {s1['waf']:.1f}pt")
    score_table.add_row("2. 고가용성 및 안정성", "12pt",
        f"[{c2}]{s2['total']:.1f}pt[/{c2}]",
        f"user {s2['user']:.1f} + product {s2['product']:.1f} + stress {s2['stress']:.1f}")
    score_table.add_row("3. 성능 효율성", "12pt",
        f"[{c3}]{s3['total']:.1f}pt[/{c3}]",
        f"user {s3['user']:.1f} + product {s3['product']:.1f} + stress {s3['stress']:.1f}")
    score_table.add_row("4. 비용 최적화", "12pt",
        f"[{c4}]{s4['total']:.1f}pt[/{c4}]",
        f"ratio={cost_ratio_str}  (기준 {base_count}대 / 평균 {avg_inst_str}대)")
    score_table.add_section()
    grand = scores["grand_total"]
    grand_color = "green" if grand >= 36 else "yellow" if grand >= 24 else "red"
    score_table.add_row("[bold]합계[/bold]", "[bold]40pt[/bold]",
        f"[{grand_color}][bold]{grand:.1f}pt[/bold][/{grand_color}]", "")
    console.print(score_table)

    # HTML 리포트
    fname_html = f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    _save_html_report(state, base_count, cost_ratio, scores,
                      total_atk, total_blocked, total_block_rate, fname_html)
    console.print(f"\nHTML 리포트: [cyan]{fname_html}[/cyan]")


def _save_html_report(state, base_count, cost_ratio, scores,
                      total_atk, total_blocked, total_block_rate, fname):
    now_str = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S KST")

    def pc(v): # pct class
        if v >= 90: return "good"
        if v >= 80: return "warn"
        if v >= 50: return "low"
        return "bad"

    # 채점표 행
    s1 = scores["1_비정상요청처리"]
    s2 = scores["2_고가용성"]
    s3 = scores["3_성능효율성"]
    s4 = scores["4_비용최적화"]
    grand = scores["grand_total"]
    score_rows = f"""
      <tr><td>1. 비정상 요청 처리</td><td>4pt</td>
          <td class="{pc(s1['total']/4*100)}">{s1['total']:.1f}pt</td>
          <td>image {s1['image']:.1f}pt + WAF 차단 {s1['waf']:.1f}pt</td></tr>
      <tr><td>2. 고가용성 및 안정성</td><td>12pt</td>
          <td class="{pc(s2['total']/12*100)}">{s2['total']:.1f}pt</td>
          <td>user {s2['user']:.1f} + product {s2['product']:.1f} + stress {s2['stress']:.1f}</td></tr>
      <tr><td>3. 성능 효율성</td><td>12pt</td>
          <td class="{pc(s3['total']/12*100)}">{s3['total']:.1f}pt</td>
          <td>user {s3['user']:.1f} + product {s3['product']:.1f} + stress {s3['stress']:.1f}</td></tr>
      <tr><td>4. 비용 최적화</td><td>12pt</td>
          <td class="{pc(s4['total']/12*100)}">{s4['total']:.1f}pt</td>
          <td>ratio={f"{cost_ratio:.3f}" if cost_ratio else "N/A"} / 전제조건 {"충족" if s4["prereq"] else "미충족"}</td></tr>
      <tr class="total-row"><td><b>합계</b></td><td><b>40pt</b></td>
          <td class="{pc(grand/40*100)}"><b>{grand:.1f}pt</b></td><td></td></tr>"""

    # API 행
    api_rows = ""
    for api, slo in SLO.items():
        st = state.api_stats[api]
        avail = st.availability
        perf  = st.performance(slo["perf"])
        cnt_2xx = sum(v for k,v in st.status_codes.items() if 200<=k<300)
        cnt_4xx = sum(v for k,v in st.status_codes.items() if 400<=k<500)
        cnt_5xx = sum(v for k,v in st.status_codes.items() if k>=500)
        avail_sc = scores["2_고가용성"].get(api, 0)
        perf_sc  = scores["3_성능효율성"].get(api, 0)
        code_dist = " / ".join(f"HTTP {k}: {v:,}건" for k,v in sorted(st.status_codes.items()))
        api_rows += f"""
        <tr>
          <td><b>{api}</b></td><td>{st.total:,}</td>
          <td class="{pc(avail)}">{avail:.2f}%</td>
          <td class="{pc(perf)}">{perf:.2f}%</td>
          <td>≤{slo['perf']}s</td>
          <td>{fmt_ms(st.avg)}</td><td>{fmt_ms(st.percentile(50))}</td>
          <td>{fmt_ms(st.percentile(95))}</td><td>{fmt_ms(st.percentile(99))}</td>
          <td class="good">{cnt_2xx:,}</td>
          <td class="warn">{cnt_4xx:,}</td>
          <td class="{'bad' if cnt_5xx else ''}">{cnt_5xx:,}</td>
          <td>{avail_sc:.1f}pt / 4pt</td><td>{perf_sc:.1f}pt / 4pt</td>
          <td style="font-size:11px;color:#8b949e">{code_dist}</td>
        </tr>"""

    # 비정상 트래픽 행
    atk_rows = ""
    for cat, ast in state.attack_stats.items():
        br = ast.block_rate
        dist = " / ".join(f"HTTP {k}: {v}건" for k,v in sorted(ast.status_dist.items()))
        details = ATTACK_DETAILS.get(cat, [])
        det_rows = ""
        for d in details:
            det_rows += f"<tr><td>{d[0]}</td><td>{d[1]}</td><td>{d[2]}</td><td>{d[3]}</td></tr>"
        det_html = f"""<details><summary>상세 페이로드 ({len(details)}건)</summary>
          <table class="detail-table">
            <tr><th>메소드</th><th>경로</th><th>필드/헤더</th><th>값</th></tr>
            {det_rows}
          </table></details>""" if details else ""
        atk_rows += f"""
        <tr>
          <td>{ast.category}</td><td>{ast.total:,}</td>
          <td class="good">{ast.blocked:,}</td>
          <td class="{'bad' if ast.passed else ''}">{ast.passed:,}</td>
          <td class="{pc(br)}">{br:.1f}%</td>
          <td>{dist}</td>
        </tr>
        <tr><td colspan="6" style="padding:4px 12px">{det_html}</td></tr>"""

    avg_inst = f"{statistics.mean(state.instance_counts):.1f}" if state.instance_counts else "N/A"
    cr_str   = f"{cost_ratio:.3f}" if cost_ratio else "N/A"
    atk_score_val = s1["waf"]
    img_score_val = s1["image"]
    waf_cls = pc(total_block_rate)
    img_perf = state.api_stats["image"].performance(SLO["image"]["perf"])

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LoadTest Report</title>
<style>
  :root{{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--sub:#8b949e;
         --good:#3fb950;--warn:#d29922;--low:#f0883e;--bad:#f85149}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:'Segoe UI',sans-serif;font-size:14px}}
  h1{{padding:24px 32px 4px;font-size:22px}}
  .meta{{padding:4px 32px 20px;color:var(--sub);font-size:12px}}
  .section{{margin:0 32px 28px}}
  .section h2{{font-size:15px;border-bottom:1px solid var(--border);padding-bottom:8px;margin-bottom:12px}}
  table{{width:100%;border-collapse:collapse;background:var(--card);border-radius:8px;overflow:hidden}}
  th{{background:#21262d;padding:10px 12px;text-align:left;font-size:12px;color:var(--sub)}}
  td{{padding:9px 12px;border-top:1px solid var(--border);font-size:13px;vertical-align:middle}}
  tr:hover td{{background:#1c2128}}
  .good{{color:var(--good);font-weight:600}}
  .warn{{color:var(--warn);font-weight:600}}
  .low{{color:var(--low);font-weight:600}}
  .bad{{color:var(--bad);font-weight:600}}
  .total-row td{{background:#21262d;font-size:15px}}
  .summary-grid{{display:grid;grid-template-columns:repeat(5,1fr);gap:16px;margin-bottom:8px}}
  .card{{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:16px 20px}}
  .card .label{{font-size:11px;color:var(--sub);margin-bottom:6px}}
  .card .value{{font-size:26px;font-weight:700}}
  .card .sub{{font-size:11px;color:var(--sub);margin-top:4px}}
  details summary{{cursor:pointer;color:var(--sub);font-size:12px;padding:4px 0;user-select:none}}
  .detail-table{{margin-top:6px;font-size:12px;background:#0d1117;border-radius:4px}}
  .detail-table th,.detail-table td{{padding:4px 10px}}
</style>
</head>
<body>
<h1>⚡ LoadTest Report</h1>
<p class="meta">생성: {now_str} &nbsp;|&nbsp; 총 소요: 30분</p>

<div class="section">
  <h2>🏆 채점 예상 (40점 만점)</h2>
  <table>
    <tr><th>항목</th><th>배점</th><th>획득 점수</th><th>세부 내역</th></tr>
    {score_rows}
  </table>
</div>

<div class="section">
  <h2>📊 요약</h2>
  <div class="summary-grid">
    <div class="card"><div class="label">총 정상 요청</div>
      <div class="value">{sum(s.total for s in state.api_stats.values()):,}</div></div>
    <div class="card"><div class="label">총 비정상 요청</div>
      <div class="value">{total_atk:,}</div></div>
    <div class="card"><div class="label">WAF 차단율</div>
      <div class="value {waf_cls}">{total_block_rate:.1f}%</div>
      <div class="sub">차단 {total_blocked:,} / 통과 {total_atk-total_blocked:,}</div></div>
    <div class="card"><div class="label">image 처리율</div>
      <div class="value {pc(img_perf)}">{img_perf:.1f}%</div>
      <div class="sub">5초 이내 기준</div></div>
    <div class="card"><div class="label">Cost Ratio</div>
      <div class="value">{cr_str}</div>
      <div class="sub">평균 {avg_inst}대 / 기준 {base_count}대</div></div>
  </div>
</div>

<div class="section">
  <h2>🚀 API 성능</h2>
  <table>
    <tr><th>API</th><th>총 요청</th><th>가용성</th><th>성능(SLO)</th><th>SLO기준</th>
        <th>avg</th><th>P50</th><th>P95</th><th>P99</th>
        <th>2xx</th><th>4xx</th><th>5xx</th>
        <th>가용 점수</th><th>성능 점수</th><th>응답코드 분포</th></tr>
    {api_rows}
  </table>
</div>

<div class="section">
  <h2>🛡️ 비정상 요청 처리 (WAF)</h2>
  <table>
    <tr><th>공격 유형</th><th>총 요청</th><th>차단(403)</th><th>통과</th><th>차단율</th><th>응답코드 분포</th></tr>
    {atk_rows}
    <tr style="border-top:2px solid var(--border)">
      <td><b>합계</b></td><td><b>{total_atk:,}</b></td>
      <td class="good"><b>{total_blocked:,}</b></td>
      <td class="{'bad' if total_atk-total_blocked else ''}">{total_atk-total_blocked:,}</td>
      <td class="{waf_cls}"><b>{total_block_rate:.1f}%</b></td>
      <td>채점 예상: <b>{atk_score_val:.1f}pt / 2.0pt</b> (image: <b>{img_score_val:.1f}pt / 2.0pt</b>)</td>
    </tr>
  </table>
</div>

<div class="section">
  <h2>💰 비용 최적화</h2>
  <table>
    <tr><th>항목</th><th>값</th><th>설명</th></tr>
    <tr><td>기준 인스턴스</td><td>{base_count}대</td><td>테스트 시작 시점 EC2 수</td></tr>
    <tr><td>평균 인스턴스</td><td>{avg_inst}대</td><td>10초 간격 샘플 {len(state.instance_counts)}회 평균</td></tr>
    <tr><td>Cost Ratio</td><td class="{pc(100 if cost_ratio and 0.5<=cost_ratio<=1.5 else 0)}">{cr_str}</td>
        <td>평균 인스턴스 / 기준 인스턴스 (0.5 이상 필요)</td></tr>
    <tr><td>성능 전제조건</td><td>{"충족" if s4["prereq"] else "미충족"}</td>
        <td>user/product/stress 성능 30% 이상이어야 비용 점수 인정</td></tr>
    <tr><td><b>채점 예상</b></td><td><b>{s4['total']:.0f}pt / 12pt</b></td>
        <td>ratio 0.5~1.0 이면 12pt 만점</td></tr>
  </table>
</div>

</body></html>"""

    with open(fname, "w", encoding="utf-8") as f:
        f.write(html)



# ── 메인 ─────────────────────────────────────────────────────────────────
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("endpoint", help="테스트 엔드포인트 (예: https://example.com)")
    parser.add_argument("--aws-region", default="ap-northeast-2")
    parser.add_argument("--cluster-name", default="apdev-cluster")
    parser.add_argument("--base-instances", type=int, default=0,
                        help="기준 인스턴스 수 (0이면 시작 시 자동 조회)")
    args = parser.parse_args()

    base = args.endpoint.rstrip("/")
    console.print(f"[bold green]LoadTest 시작[/bold green] → {base}")

    state = LoadTestState()

    # 기준 인스턴스 수
    base_count = args.base_instances
    if base_count == 0 and AWS_AVAILABLE:
        base_count = get_ec2_instance_count(args.aws_region, args.cluster_name)
        console.print(f"EC2 조회 기준 인스턴스: [cyan]{base_count}[/cyan]대")
    if base_count == 0:
        try:
            base_count = int(input("기준 인스턴스 수 입력 (트래픽 없을 때 최소 노드 수, 보통 1): ").strip() or "1")
        except Exception:
            base_count = 1
    console.print(f"기준 인스턴스: [cyan]{base_count}[/cyan]대 (cost ratio 계산 기준)")
    state.base_instance_count = base_count

    stop_event = asyncio.Event()
    all_done = asyncio.Event()
    ec2_stop = asyncio.Event()

    # EC2 샘플러
    ec2_task = asyncio.create_task(
        ec2_sampler(state, args.aws_region, args.cluster_name, ec2_stop))

    # 스케줄러
    sched_task = asyncio.create_task(
        vu_scheduler(base, state, stop_event, all_done))

    # 대시보드
    with Live(render_dashboard(state, base_count, args.aws_region),
              refresh_per_second=2, console=console) as live:
        while not all_done.is_set():
            live.update(render_dashboard(state, base_count, args.aws_region))
            await asyncio.sleep(0.5)

    ec2_stop.set()
    await ec2_task

    console.print("\n[bold yellow]테스트 완료! 최종 리포트 생성 중...[/bold yellow]\n")
    print_final_report(state, base_count)


if __name__ == "__main__":
    asyncio.run(main())