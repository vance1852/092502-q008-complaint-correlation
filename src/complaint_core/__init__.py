"""环境投诉资料基础服务的服务端基础包。"""

from .cases import CaseService
from .service import DomainService

__all__ = ["DomainService", "CaseService"]
