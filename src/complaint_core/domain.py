"""保存本项目允许登记的领域资料类别。"""

ALLOWED_CATEGORIES = frozenset([
    "complaint_zone",
    "operation_window",
    "duty_roster",
    "weather_source",
    "treatment_status",
    "weather_snapshot"
])

# 区域级资料可以不挂靠具体场所（site_id 为空）。
SITELESS_CATEGORIES = frozenset(["weather_snapshot"])


def is_allowed_category(value: str) -> bool:
    """判断资料类别是否属于当前项目。"""

    return value in ALLOWED_CATEGORIES
