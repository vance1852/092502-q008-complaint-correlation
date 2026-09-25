"""投诉关联的确定性规则：文本指纹、重复合并、候选评分与置信区间。

规则只产出带版本号的候选研判，不直接形成执法结论；任何候选都可以被
值班人员排除、追加或确认。规则内容随规则版本（rule_version）冻结，
规则更新只会影响之后生成的版本，不会改写历史版本。
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

from .audit import digest

# ---------------------------------------------------------------------------
# 规则族与版本
# ---------------------------------------------------------------------------

RULE_FAMILY = "complaint-correlation"
RULE_SPEC_VERSION = 1

RULE_SPEC: dict[str, Any] = {
    "spec_version": RULE_SPEC_VERSION,
    "family": RULE_FAMILY,
    # 合并参数
    "merge_window_minutes": 30,
    "fingerprint_ngram": 2,
    "fingerprint_threshold": 0.6,
    # 评分参数
    "weights": {
        "distance": 0.30,
        "downwind": 0.25,
        "time_match": 0.20,
        "text_match": 0.15,
        "weather": 0.10,
    },
    "distance_meters_strong": 500.0,
    "distance_meters_max": 3000.0,
    "downwind_half_angle_deg": 45.0,
    "time_window_minutes": 60,
    # 置信区间（与评分同尺度的半宽，对称、裁剪到 [0,1]）
    "confidence_base": 0.18,
    "confidence_per_factor": 0.03,
    "confidence_floor": 0.05,
    # 现场核查建议
    "inspection_high_score": 0.70,
    "inspection_low_ci": 0.55,
}


def rule_spec() -> dict[str, Any]:
    """返回当前打包的规则规格（深拷贝语义，调用方不得依赖引用可变）。"""

    return {**RULE_SPEC, "weights": {**RULE_SPEC["weights"]}}


# ---------------------------------------------------------------------------
# 文本归一化与指纹
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[\s　，。！？、；：「」『』（）【】《》〈〉“”‘’\-—…·.,!?;:()\[\]{}\"'/\\|<>~`@#$%^&*_+=]+")
_STOPWORDS = frozenset([
    "我们", "附近", "一直", "已经", "什么", "怎么", "这样", "那样", "这里", "那里",
    "的话", "觉得", "感觉", "应该", "可能", "一个", "有些", "有点",
])


def normalize_text(text: str) -> str:
    """全角转半角、小写、去标点与空白，得到稳定的比对文本。"""

    if text is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(text)).lower()
    normalized = _PUNCT_RE.sub("", normalized)
    for word in _STOPWORDS:
        normalized = normalized.replace(word, "")
    return normalized


def _shingles(normalized: str, n: int) -> list[str]:
    if len(normalized) <= n:
        return [normalized] if normalized else []
    return [normalized[i:i + n] for i in range(len(normalized) - n + 1)]


def text_fingerprint(description: str) -> dict[str, Any]:
    """生成文本指纹：归一化文本的字符 n-gram 集合。

    返回 {"norm", "grams": [...]}。两文本的相似度用重叠系数衡量
    （按较短文本归一化），对居民来电常见的改写、增删虚词更稳健。
    """

    spec = RULE_SPEC
    norm = normalize_text(description)
    grams = sorted(set(_shingles(norm, spec["fingerprint_ngram"])))
    return {"norm": norm, "grams": grams}


def fingerprint_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    """两个指纹的重叠系数 |A∩B| / min(|A|,|B|)，空集返回 0。"""

    a = set(left.get("grams", ()))
    b = set(right.get("grams", ()))
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


# ---------------------------------------------------------------------------
# 联系方式保护
# ---------------------------------------------------------------------------

_DIGIT_RE = re.compile(r"\d")


def mask_contact(value: str | None) -> str:
    """对手机号/电话做不可逆掩码：保留前 3 位与后 2 位。"""

    if not value:
        return ""
    digits = _DIGIT_RE.sub("", str(value))
    raw = re.sub(r"\s+", "", str(value))
    if len(raw) < 5:
        return "*" * len(raw)
    return f"{raw[:3]}{'*' * (len(raw) - 5)}{raw[-2:]}"


def contact_token(value: str | None) -> str | None:
    """由联系方式生成不可逆比对令牌（仅保留数字后散列）。"""

    if not value:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits:
        return None
    return digest({"contact": digits})


# ---------------------------------------------------------------------------
# 时间、几何
# ---------------------------------------------------------------------------

def parse_instant(value: str) -> float:
    """解析 ISO-8601 时间为 epoch 秒，支持结尾 Z。"""

    from datetime import datetime

    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text).timestamp()


def time_gap_minutes(left: str, right: str) -> float:
    return abs(parse_instant(left) - parse_instant(right)) / 60.0


def haversine_meters(a: tuple[float, float], b: tuple[float, float]) -> float:
    """两点经纬度之间的球面距离（米）。"""

    radius = 6_371_000.0
    lat1, lon1 = (math.radians(v) for v in a)
    lat2, lon2 = (math.radians(v) for v in b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def bearing_degrees(a: tuple[float, float], b: tuple[float, float]) -> float:
    """从 a 指向 b 的初始方位角（度，0=北，顺时针）。"""

    lat1, lon1 = (math.radians(v) for v in a)
    lat2, lon2 = (math.radians(v) for v in b)
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _angular_gap(a: float, b: float) -> float:
    gap = abs(a - b) % 360.0
    return min(gap, 360.0 - gap)


def downwind_score(complaint_xy: tuple[float, float], site_xy: tuple[float, float],
                   wind_direction_deg: float | None, spec: dict[str, Any] | None = None) -> tuple[float, bool]:
    """判断投诉点是否位于企业下风向。

    气象上的风向指风从哪个方向吹来，下风向方位 = 风向 + 180。
    返回 (子分数, 是否在下风向扇形内)。
    """

    spec = spec or RULE_SPEC
    if wind_direction_deg is None:
        return 0.0, False
    toward = bearing_degrees(site_xy, complaint_xy)
    downwind = (float(wind_direction_deg) + 180.0) % 360.0
    gap = _angular_gap(toward, downwind)
    half = spec["downwind_half_angle_deg"]
    if gap > half:
        return 0.0, False
    return max(0.0, 1.0 - gap / half), True


# ---------------------------------------------------------------------------
# 重复来电判定
# ---------------------------------------------------------------------------

def should_merge(new: dict[str, Any], existing: dict[str, Any],
                 spec: dict[str, Any] | None = None) -> tuple[bool, list[str]]:
    """按区域、时间窗口和文本指纹判断两条投诉是否应合并。

    existing/new 需包含 region_code、occurred_at、fingerprint，可选
    contact_token、location。同联系方式是加强信号但非必要条件。
    返回 (是否合并, 命中依据)。
    """

    spec = spec or RULE_SPEC
    reasons: list[str] = []
    if new.get("region_code") != existing.get("region_code"):
        return False, []
    if time_gap_minutes(new["occurred_at"], existing["occurred_at"]) > spec["merge_window_minutes"]:
        return False, []
    similarity = fingerprint_similarity(new.get("fingerprint", {}), existing.get("fingerprint", {}))
    same_contact = bool(new.get("contact_token") and new.get("contact_token") == existing.get("contact_token"))
    close_points = False
    new_xy, old_xy = new.get("location"), existing.get("location")
    if new_xy and old_xy:
        close_points = haversine_meters(tuple(new_xy), tuple(old_xy)) <= spec["distance_meters_strong"]
    if similarity >= spec["fingerprint_threshold"]:
        reasons.append(f"text_fingerprint:{similarity:.2f}")
    if same_contact:
        reasons.append("same_contact")
    if close_points:
        reasons.append("same_spot")
    if not reasons:
        return False, []
    return True, reasons


# ---------------------------------------------------------------------------
# 事实快照收集与候选评分
# ---------------------------------------------------------------------------

def gather_facts(*, case: dict[str, Any], events: list[dict[str, Any]],
                 sites: list[dict[str, Any]], domain_records: list[dict[str, Any]],
                 weather: dict[str, Any] | None) -> dict[str, Any]:
    """把案件研判时依赖的输入冻结成一份可散列事实。

    sites: [{site_id, name, region_code, location: [lat, lon], organization_id}]
    domain_records: 平台已接收的资料（生产时段、治污设施状态、气象快照等），
        每条含 site_id、category、external_key、payload、as_of。
    weather: 该时间窗的气象快照 {wind_direction_deg, wind_speed_mps, as_of, source}。
    """

    return {
        "case": case,
        "events": sorted(events, key=lambda item: (item["occurred_at"], item["event_id"])),
        "sites": sorted(sites, key=lambda item: item["site_id"]),
        "domain_records": sorted(domain_records,
                                 key=lambda item: (item.get("site_id", ""), item.get("category", ""),
                                                   item.get("external_key", ""))),
        "weather": weather,
    }


def _records_for(records: list[dict[str, Any]], site_id: str) -> list[dict[str, Any]]:
    return [r for r in records if r.get("site_id") == site_id]


def _time_match(event_window: tuple[str, str], records: list[dict[str, Any]],
                spec: dict[str, Any]) -> tuple[float, list[str]]:
    """生产时段与投诉时间窗是否重叠，治污设施是否异常。"""

    factors: list[str] = []
    best = 0.0
    start = parse_instant(event_window[0])
    end = parse_instant(event_window[1])
    for record in records:
        if record.get("category") != "operation_window":
            continue
        payload = record.get("payload", {})
        windows = payload.get("windows", [])
        for window in windows:
            try:
                w_start = parse_instant(window["start"])
                w_end = parse_instant(window["end"])
            except (KeyError, TypeError, ValueError):
                continue
            overlap = max(0.0, min(end, w_end) - max(start, w_start))
            span = max(end - start, 1.0)
            best = max(best, min(1.0, overlap / span))
        if best > 0:
            factors.append("operation_window_overlap")
    for record in records:
        if record.get("category") != "treatment_status":
            continue
        payload = record.get("payload", {})
        if payload.get("status") in {"abnormal", "offline", "bypassed"}:
            best = max(best, 0.8)
            factors.append(f"treatment_{payload.get('status')}")
    return best, factors


def score_candidate(*, event_window: tuple[str, str], text_terms: list[str],
                    anchor: tuple[float, float] | None, weather: dict[str, Any] | None,
                    site: dict[str, Any], site_records: list[dict[str, Any]],
                    spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """对单个企业计算各贡献因素、总分和置信区间。"""

    spec = spec or RULE_SPEC
    weights = spec["weights"]
    contributions: dict[str, float] = {}
    factors: list[str] = []

    # 距离因素
    site_xy = site.get("location")
    if anchor and site_xy:
        distance = haversine_meters(tuple(anchor), tuple(site_xy))
        if distance <= spec["distance_meters_max"]:
            if distance <= spec["distance_meters_strong"]:
                contributions["distance"] = weights["distance"]
            else:
                span = spec["distance_meters_max"] - spec["distance_meters_strong"]
                contributions["distance"] = round(
                    weights["distance"] * max(0.0, 1.0 - (distance - spec["distance_meters_strong"]) / span), 6)
            factors.append(f"distance_{int(distance)}m")
    else:
        distance = None

    # 下风向因素
    if anchor and site_xy and weather and weather.get("wind_direction_deg") is not None:
        wind_value, downwind = downwind_score(tuple(anchor), tuple(site_xy),
                                              weather["wind_direction_deg"], spec)
        if downwind:
            contributions["downwind"] = round(weights["downwind"] * wind_value, 6)
            factors.append("downwind")

    # 时间/治污因素
    time_value, time_factors = _time_match(event_window, site_records, spec)
    if time_value > 0:
        contributions["time_match"] = round(weights["time_match"] * time_value, 6)
        factors.extend(time_factors)

    # 文本命中因素（企业资料中声明的特征词，如产品、气味类型）
    hit_terms: list[str] = []
    vocab: set[str] = set()
    for record in site_records:
        vocab.update(record.get("payload", {}).get("odor_terms", []))
    for term in text_terms:
        if term and term in vocab:
            hit_terms.append(term)
    if hit_terms:
        contributions["text_match"] = weights["text_match"]
        factors.append("odor_terms:" + ",".join(sorted(hit_terms)))

    # 气象证据因素：有与投诉时间匹配的气象快照才给基础分，风速过低不利扩散再加分
    if weather and weather.get("as_of"):
        try:
            if time_gap_minutes(weather["as_of"], event_window[0]) <= spec["time_window_minutes"]:
                weather_value = 0.5
                speed = weather.get("wind_speed_mps")
                if isinstance(speed, (int, float)) and speed <= 2.0:
                    weather_value = 1.0
                    factors.append("low_wind_speed")
                contributions["weather"] = round(weights["weather"] * weather_value, 6)
                factors.append("weather_snapshot")
        except (TypeError, ValueError):
            pass

    score = round(min(1.0, sum(contributions.values())), 6)
    half_width = max(spec["confidence_floor"],
                     spec["confidence_base"] - spec["confidence_per_factor"] * len(factors))
    lower = round(max(0.0, score - half_width), 6)
    upper = round(min(1.0, score + half_width), 6)
    return {
        "site_id": site["site_id"],
        "site_name": site.get("name", site["site_id"]),
        "score": score,
        "contributions": contributions,
        "factors": factors,
        "confidence_interval": {"level": 0.90, "lower": lower, "upper": upper},
        "distance_meters": None if distance is None else round(distance, 1),
    }


def _anchor(events: list[dict[str, Any]]) -> tuple[float, float] | None:
    for event in sorted(events, key=lambda item: item["occurred_at"]):
        location = event.get("location")
        if location:
            return tuple(location)  # type: ignore[return-value]
    return None


def _text_terms(events: list[dict[str, Any]]) -> list[str]:
    terms: set[str] = set()
    for event in events:
        for term in event.get("odor_terms", []):
            terms.add(term)
        norm = event.get("fingerprint", {}).get("norm")
        if norm:
            terms.add(norm[:8])
    return sorted(terms)


def generate_candidates(facts: dict[str, Any], spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """依据冻结事实生成候选企业、贡献因素与置信区间。

    输出是确定性的：相同规则规格 + 相同事实必然得到相同结果。
    """

    spec = spec or RULE_SPEC
    case = facts["case"]
    events = facts["events"]
    sites = facts["sites"]
    records = facts["domain_records"]
    weather = facts.get("weather")

    event_window = (
        min(event["occurred_at"] for event in events),
        max(event["occurred_at"] for event in events),
    )
    anchor = _anchor(events)
    text_terms = _text_terms(events)
    region = case.get("region_code")

    candidates = []
    for site in sites:
        if region and site.get("region_code") and site["region_code"] != region:
            continue
        candidate = score_candidate(
            event_window=event_window,
            text_terms=text_terms,
            anchor=anchor,
            weather=weather,
            site=site,
            site_records=_records_for(records, site["site_id"]),
            spec=spec,
        )
        candidates.append(candidate)

    candidates.sort(key=lambda item: (-item["score"], item["site_id"]))
    ranked = [dict(item, rank=index + 1) for index, item in enumerate(candidates)]
    needs_field = any(
        item["score"] >= spec["inspection_high_score"]
        or item["confidence_interval"]["lower"] >= spec["inspection_low_ci"]
        for item in ranked
    )
    return {
        "rule_family": RULE_FAMILY,
        "rule_spec_version": spec.get("spec_version", RULE_SPEC_VERSION),
        "candidates": ranked,
        "needs_field_inspection": bool(needs_field),
    }
