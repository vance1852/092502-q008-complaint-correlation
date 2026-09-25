"""投诉关联引擎：纯函数，不触碰数据库与时钟。

引擎把"居民描述 + 候选场所状态 + 气象快照"按照某个规则版本的参数
换算成候选企业、贡献因素和置信区间。所有输出只取决于输入，
因此同一规则版本对同一冻结快照必然得到同一结果（便于复核与审计）。

置信区间是面向值班研判的启发式区间，不是统计推断，更不是执法结论。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .audit import canonical_json, digest

SHINGLE_SIZE = 3
EARTH_RADIUS_KM = 6371.0088

DEFAULT_PARAMS: dict[str, Any] = {
    "time_window_minutes": 180,
    "min_text_jaccard": 0.30,
    "distance_limit_km": 5.0,
    "min_score": 0.35,
    # 各贡献因素的权重，合计后裁剪到 [0, 1]
    "weights": {
        "time_overlap": 0.24,
        "text_match": 0.22,
        "spatial_proximity": 0.18,
        "downwind_transport": 0.18,
        "production_running": 0.10,
        "treatment_offline": 0.08,
    },
    # 触发现场核查建议的阈值
    "inspection_score_threshold": 0.55,
    # 区间半径随证据缺口收缩/扩张的系数
    "interval_base": 0.12,
    "interval_per_missing_factor": 0.05,
}


def normalize_text(text: str) -> str:
    """归一化居民描述：保留中日韩文字与字母数字，统一小写与空白。"""

    kept: list[str] = []
    for char in str(text or "").lower():
        code = ord(char)
        if char.isalnum() or 0x4E00 <= code <= 0x9FFF:
            kept.append(char)
        else:
            kept.append(" ")
    normalized = " ".join("".join(kept).split())
    return normalized


def shingles(text: str, size: int = SHINGLE_SIZE) -> list[str]:
    """生成字符级 shingle 集合的排序列表（集合语义，稳定输出）。"""

    normalized = normalize_text(text)
    padded = f"^{normalized}$"
    if len(padded) < size:
        return sorted({padded})
    return sorted({padded[index:index + size] for index in range(len(padded) - size + 1)})


def text_fingerprint(text: str) -> tuple[str, list[str]]:
    """返回文本指纹（shingle 集合摘要）与 shingle 列表。"""

    pieces = shingles(text)
    return digest(pieces), pieces


def jaccard(left: list[str] | set[str], right: list[str] | set[str]) -> float:
    """计算两个 shingle 集合的 Jaccard 相似度。"""

    left_set, right_set = set(left), set(right)
    if not left_set and not right_set:
        return 0.0
    union = left_set | right_set
    return len(left_set & right_set) / len(union) if union else 0.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """计算两个经纬度坐标之间的球面距离（公里）。"""

    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (math.sin(d_phi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2)
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _bearing_delta(deg_a: float, deg_b: float) -> float:
    """返回两个方位角之间 0..180 度的最小夹角。"""

    delta = abs((float(deg_a) - float(deg_b) + 180.0) % 360.0 - 180.0)
    return delta


@dataclass(frozen=True)
class SiteEvidence:
    """场所侧在研判时点的事实输入（来自冻结的平台快照）。"""

    site_id: str
    lat: float | None = None
    lon: float | None = None
    production_running: bool | None = None
    treatment_online: bool | None = None
    odor_keywords: tuple[str, ...] = ()
    site_text: str = ""
    time_overlap: bool = False


@dataclass(frozen=True)
class WeatherSnapshot:
    """投诉时点的气象快照。"""

    wind_direction_deg: float | None = None  # 风吹来的方向，气象惯例
    wind_speed_ms: float | None = None
    stability_class: str | None = None
    observed_at: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class Factor:
    """单个贡献因素及其 0..1 的贡献度。"""

    code: str
    contribution: float
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateResult:
    """引擎为一个场所产出的候选结果。"""

    site_id: str
    score: float
    confidence_low: float
    confidence_high: float
    factors: tuple[Factor, ...]
    eligible: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "score": round(self.score, 4),
            "confidence_low": round(self.confidence_low, 4),
            "confidence_high": round(self.confidence_high, 4),
            "eligible": self.eligible,
            "factors": [
                {"code": factor.code, "contribution": round(factor.contribution, 4),
                 "detail": factor.detail}
                for factor in self.factors
            ],
        }


@dataclass(frozen=True)
class EngineResult:
    """一次关联计算的完整输出。"""

    candidates: tuple[CandidateResult, ...]
    requires_site_inspection: bool
    params_hash: str

    def to_dict(self) -> dict[str, Any]:
        ordered = sorted(self.candidates, key=lambda item: (-item.score, item.site_id))
        return {
            "candidates": [candidate.to_dict() for candidate in ordered],
            "requires_site_inspection": self.requires_site_inspection,
            "params_hash": self.params_hash,
        }


def merge_params(params: dict[str, Any] | None) -> dict[str, Any]:
    """以默认参数为底叠加规则版本自带参数（深一层 weights 合并）。"""

    merged = {key: value for key, value in DEFAULT_PARAMS.items()}
    if params:
        for key, value in params.items():
            if key == "weights" and isinstance(value, dict):
                weights = dict(DEFAULT_PARAMS["weights"])
                weights.update(value)
                merged["weights"] = weights
            else:
                merged[key] = value
    return merged


def _downwind_score(weather: WeatherSnapshot | None, evidence: SiteEvidence,
                    complaint_lat: float | None, complaint_lon: float | None) -> Factor | None:
    if weather is None or weather.wind_direction_deg is None or weather.wind_speed_ms is None:
        return None
    if None in (evidence.lat, evidence.lon, complaint_lat, complaint_lon):
        return None
    if weather.wind_speed_ms <= 0:
        return Factor("downwind_transport", 0.0, {"reason": "wind_calm"})
    # 场所指向投诉点的方位角；气象风向是"来向"，吹送方向需 +180
    phi1, phi2 = math.radians(evidence.lat), math.radians(complaint_lat)
    d_lambda = math.radians(complaint_lon - evidence.lon)
    bearing = math.degrees(math.atan2(
        math.sin(d_lambda) * math.cos(phi2),
        math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(d_lambda),
    )) % 360.0
    transport_direction = (weather.wind_direction_deg + 180.0) % 360.0
    delta = _bearing_delta(bearing, transport_direction)
    alignment = max(0.0, math.cos(math.radians(delta)))
    return Factor("downwind_transport", round(alignment, 4),
                  {"bearing_site_to_complaint_deg": round(bearing, 1),
                   "wind_transport_direction_deg": round(transport_direction, 1),
                   "delta_deg": round(delta, 1),
                   "wind_speed_ms": weather.wind_speed_ms})


def correlate(*, description: str, complaint_lat: float | None, complaint_lon: float | None,
              evidences: list[SiteEvidence], weather: WeatherSnapshot | None,
              params: dict[str, Any] | None = None) -> EngineResult:
    """对一批场所执行关联计算，返回候选与置信区间。"""

    merged = merge_params(params)
    weights = merged["weights"]
    text_pieces = shingles(description)
    normalized = normalize_text(description)
    results: list[CandidateResult] = []

    for evidence in evidences:
        factors: list[Factor] = []

        # 文本指纹相似度
        similarity = jaccard(text_pieces, shingles(evidence.site_text))
        for keyword in evidence.odor_keywords:
            if keyword and keyword in normalized:
                similarity = max(similarity, 1.0)
        text_eligible = similarity >= merged["min_text_jaccard"]
        factors.append(Factor("text_match", round(similarity, 4),
                              {"jaccard": round(similarity, 4),
                               "passed_threshold": text_eligible}))

        # 空间邻近度
        if None not in (evidence.lat, evidence.lon, complaint_lat, complaint_lon):
            distance = haversine_km(evidence.lat, evidence.lon, complaint_lat, complaint_lon)
            limit = float(merged["distance_limit_km"])
            proximity = max(0.0, 1.0 - distance / limit) if limit > 0 else 0.0
            spatial_eligible = distance <= limit
            factors.append(Factor("spatial_proximity", round(proximity, 4),
                                  {"distance_km": round(distance, 3),
                                   "limit_km": limit, "passed_threshold": spatial_eligible}))
        else:
            distance = None
            spatial_eligible = True  # 无坐标时不因空间直接淘汰，只给零贡献
            factors.append(Factor("spatial_proximity", 0.0, {"reason": "coordinates_missing"}))

        # 风向输送
        downwind = _downwind_score(weather, evidence, complaint_lat, complaint_lon)
        if downwind is None:
            factors.append(Factor("downwind_transport", 0.0, {"reason": "weather_or_position_missing"}))
        else:
            factors.append(Factor("downwind_transport", downwind.contribution, downwind.detail))

        # 生产与治污设施状态
        if evidence.production_running is True:
            factors.append(Factor("production_running", 1.0, {"state": "running"}))
        elif evidence.production_running is False:
            factors.append(Factor("production_running", 0.0, {"state": "stopped"}))
        else:
            factors.append(Factor("production_running", 0.0, {"state": "unknown"}))
        if evidence.treatment_online is False and evidence.production_running is not False:
            factors.append(Factor("treatment_offline", 1.0, {"state": "offline"}))
        elif evidence.treatment_online is True:
            factors.append(Factor("treatment_offline", 0.0, {"state": "online"}))
        else:
            factors.append(Factor("treatment_offline", 0.0, {"state": "unknown"}))

        # 时间重叠由调用方按规则窗口预先判定（生产时段命中投诉时间窗）
        factors.append(Factor("time_overlap", 1.0 if evidence.time_overlap else 0.0,
                              {"within_window": evidence.time_overlap}))

        score = 0.0
        missing = 0
        for factor in factors:
            weight = float(weights.get(factor.code, 0.0))
            score += weight * factor.contribution
            if factor.detail.get("state") == "unknown" or factor.detail.get("reason"):
                missing += 1
        score = round(min(1.0, max(0.0, score)), 4)

        margin = float(merged["interval_base"]) + float(merged["interval_per_missing_factor"]) * missing
        low = round(max(0.0, score - margin), 4)
        high = round(min(1.0, score + margin), 4)

        eligible = (text_eligible and spatial_eligible
                    and score >= float(merged["min_score"]))
        results.append(CandidateResult(evidence.site_id, score, low, high,
                                       tuple(factors), eligible))

    candidates = tuple(result for result in results if result.eligible)
    top_score = max((candidate.score for candidate in candidates), default=0.0)
    requires = any(candidate.score >= float(merged["inspection_score_threshold"])
                   for candidate in candidates)
    return EngineResult(
        candidates=tuple(sorted(candidates, key=lambda item: (-item.score, item.site_id))),
        requires_site_inspection=requires,
        params_hash=digest({"params": merged, "engine": "complaint_core.engine.v1"}),
    )


def anonymized_case_code(case_id: str) -> str:
    """从案件内部编号派生不含投诉人信息的对外编码。"""

    return "CASE-" + quote(digest({"case_id": case_id})[:12].upper(), safe="")
