"""Platform Adapter 层：统一三个平台的接入。

- 千帆（qianfan）：真实接入（复用现有 CDP Worker 的逻辑）；
- 抖音（douyin）、千牛（qianniu）：占位实现，只定义接口，返回统一 Message。

所有平台都产出统一的 IncomingMessage，供下游 Router Agent 使用。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PlatformMessage:
    """统一的平台消息（跨平台归一化）。"""

    platform: str              # qianfan | douyin | qianniu
    store_id: str
    customer_id: str
    message_id: str
    text: str
    attachments: tuple[str, ...] = field(default_factory=tuple)

    @property
    def session_key(self) -> str:
        return f"{self.platform}|{self.store_id}|{self.customer_id}"


class PlatformAdapter:
    """平台适配器接口。"""

    platform: str = "unknown"

    def receive(self) -> list[PlatformMessage]:
        """拉取平台的新消息。"""
        raise NotImplementedError

    def send(self, message: PlatformMessage, reply: str) -> bool:
        """发送回复到平台。"""
        raise NotImplementedError


class QianfanAdapter(PlatformAdapter):
    """千帆（真实接入占位：实际收发由现有 CDP Worker 完成，这里只是接口对齐）。

    说明：千帆的真实收发依赖 Electron CDP，本 MVP 中保留接口，
    实际由 workers/qianfan_cdp_worker.py 通过 /v1/decide 走完整链路。
    """

    platform = "qianfan"

    def __init__(self) -> None:
        self._inbox: list[PlatformMessage] = []

    def receive(self) -> list[PlatformMessage]:
        msgs = list(self._inbox)
        self._inbox.clear()
        return msgs

    def send(self, message: PlatformMessage, reply: str) -> bool:
        # 真实发送由 CDP Worker 完成；这里仅记录
        return True


class DouyinAdapter(PlatformAdapter):
    """抖音（占位）：接口已定义，真实接入需飞鸽后台 Playwright/API，后续实现。"""

    platform = "douyin"

    def receive(self) -> list[PlatformMessage]:
        return []

    def send(self, message: PlatformMessage, reply: str) -> bool:
        return False


class QianniuAdapter(PlatformAdapter):
    """千牛（占位）：接口已定义，真实接入需千牛 App/API，后续实现。"""

    platform = "qianniu"

    def receive(self) -> list[PlatformMessage]:
        return []

    def send(self, message: PlatformMessage, reply: str) -> bool:
        return False


ADAPTERS: dict[str, PlatformAdapter] = {
    "qianfan": QianfanAdapter(),
    "douyin": DouyinAdapter(),
    "qianniu": QianniuAdapter(),
}


def get_adapter(platform: str) -> PlatformAdapter:
    return ADAPTERS.get(platform, QianfanAdapter())
