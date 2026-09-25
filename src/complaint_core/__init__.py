"""环境投诉资料基础服务与投诉关联模块。"""

from .correlation import ComplaintService
from .service import DomainService

__all__ = ["DomainService", "ComplaintService"]
