"""小红书千帆客服 Agent —— 配置。

所有敏感项（LLM key 等）从环境变量或 .env 文件注入，仓库只提交 .env.example。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _load_dotenv() -> None:
    """从项目根目录 .env 加载环境变量（若存在且未显式设置）。

    不覆盖已存在的环境变量，以便部署环境可覆盖本地 .env。
    """
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Settings:
    base_dir: Path
    data_dir: Path
    policy_path: Path
    database_path: str
    llm_mode: str            # llm | rules
    llm_base_url: str
    llm_model: str
    llm_api_key: str | None
    store_id: str
    tenant_id: str
    api_key: str | None       # 决策 API 的鉴权 key（可选）

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        data_dir = Path(os.environ.get("XHS_DATA_DIR", BASE_DIR / "src" / "xhs_kefu" / "data"))
        return cls(
            base_dir=BASE_DIR,
            data_dir=data_dir,
            policy_path=Path(os.environ.get("XHS_POLICY_PATH", BASE_DIR / "config" / "policy.toml")),
            database_path=os.environ.get("XHS_DB_PATH", str(BASE_DIR / "data" / "xhs_kefu.db")),
            llm_mode=os.environ.get("XHS_LLM_MODE", "rules").lower(),
            llm_base_url=os.environ.get("XHS_LLM_BASE_URL", "https://api.deepseek.com"),
            llm_model=os.environ.get("XHS_LLM_MODEL", "deepseek-chat"),
            llm_api_key=os.environ.get("DEEPSEEK_API_KEY") or None,
            store_id=os.environ.get("XHS_STORE_ID", "STORE-001"),
            tenant_id=os.environ.get("XHS_TENANT_ID", "demo"),
            api_key=os.environ.get("XHS_API_KEY") or None,
        )
