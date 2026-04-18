"""加载/保存 agent specs (YAML).

文件: webapp/agents.yaml (与 webapp 同级)
首次启动时, 如不存在, 用 DEFAULT_SPECS 写一份.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List

import yaml

from .specs import DEFAULT_SPECS, BRAND_PREFIX
from .types import AgentSpec

logger = logging.getLogger(__name__)


# webapp/core/agents/config.py → webapp/agents.yaml (3 levels up: agents → core → webapp)
DEFAULT_SPECS_PATH = Path(__file__).resolve().parent.parent.parent / "agents.yaml"


def _serialize(spec: AgentSpec) -> Dict:
    return spec.model_dump(exclude_none=False)


def _deserialize(d: Dict) -> AgentSpec:
    return AgentSpec(**d)


def save_agent_specs(specs: List[AgentSpec], path: Path = DEFAULT_SPECS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "brand_prefix": BRAND_PREFIX,
        "agents": [_serialize(s) for s in specs],
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False, width=120)
    logger.info(f"💾 agent specs 已保存到 {path}")


def load_agent_specs(path: Path = DEFAULT_SPECS_PATH) -> Dict:
    """返回 {brand_prefix: str, specs: List[AgentSpec]}.

    若文件不存在, 自动用 DEFAULT_SPECS 写一份再读.
    """
    if not path.exists():
        logger.info(f"agents.yaml 不存在, 用默认 specs 创建: {path}")
        save_agent_specs(DEFAULT_SPECS, path)

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    brand_prefix = data.get("brand_prefix", BRAND_PREFIX)
    raw_agents = data.get("agents", [])
    specs: List[AgentSpec] = []
    for d in raw_agents:
        try:
            specs.append(_deserialize(d))
        except Exception as e:
            logger.warning(f"跳过畸形 agent spec: {d.get('id')} ({e})")
    if not specs:
        logger.warning("agents.yaml 没有有效 specs, 回退到默认")
        specs = list(DEFAULT_SPECS)

    # 自动合并: DEFAULT_SPECS 里有但 yaml 没有的 id (如新增了 cover_designer), 自动 append
    existing_ids = {s.id for s in specs}
    added_any = False
    for default_spec in DEFAULT_SPECS:
        if default_spec.id not in existing_ids:
            specs.append(default_spec)
            existing_ids.add(default_spec.id)
            added_any = True
            logger.info(f"agents.yaml 缺少 {default_spec.id}, 自动补入默认 spec")
    if added_any:
        # 持久化, 让用户在 UI 里能看到
        try:
            save_agent_specs(specs, path)
        except Exception as e:
            logger.warning(f"自动补入新 spec 后写盘失败: {e}")

    return {"brand_prefix": brand_prefix, "specs": specs}
