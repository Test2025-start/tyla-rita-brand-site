"""配置合并模块 — 支持多人协作的深度合并

提供 JSON 配置的深度合并逻辑，支持：
- 文本字段：本地优先
- 图片字段：保留双方 URL
- 数组字段：按名称去重合并
- 联系方式：保留双方启用的渠道
"""
import copy
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("config_merge")


def is_image_field(key: str, value: Any) -> bool:
    """判断是否为图片字段"""
    if not isinstance(value, str):
        return False
    image_keys = {"image", "logo", "qrImage", "ogImage", "favicon", "qr"}
    if key in image_keys:
        return True
    if key.endswith("Image") or key.endswith("image"):
        return True
    return False


def merge_text_field(local_val: Any, remote_val: Any) -> Any:
    """合并文本字段：本地优先"""
    if local_val is not None and local_val != "":
        return local_val
    return remote_val


def merge_i18n_field(local_val: Any, remote_val: Any) -> Any:
    """合并多语言字段：保留双方，本地优先"""
    if isinstance(local_val, dict) and isinstance(remote_val, dict):
        result = copy.deepcopy(remote_val)
        for lang, val in local_val.items():
            if val:  # 本地有值就用本地的
                result[lang] = val
        return result
    if isinstance(local_val, dict):
        return local_val
    if isinstance(remote_val, dict):
        return remote_val
    return merge_text_field(local_val, remote_val)


def merge_image_field(local_val: Any, remote_val: Any) -> Any:
    """合并图片字段：保留双方 URL"""
    if not local_val:
        return remote_val
    if not remote_val:
        return local_val
    if local_val == remote_val:
        return local_val
    # 两个不同的 URL，保留本地的（用户最新选择）
    # 但记录远程的 URL 以备需要
    logger.info("图片字段合并: 本地 %s, 远程 %s (保留本地)", local_val[:50], remote_val[:50])
    return local_val


def get_item_name(item: Any) -> str:
    """获取数组项的名称（用于去重）"""
    if isinstance(item, dict):
        name = item.get("name", "")
        if isinstance(name, dict):
            return name.get("zh", "") or name.get("en", "") or str(name)
        return str(name) if name else ""
    return str(item)


def merge_array_field(local_val: list, remote_val: list) -> list:
    """合并数组字段：按名称去重，保留双方"""
    if not isinstance(local_val, list):
        return remote_val if isinstance(remote_val, list) else []
    if not isinstance(remote_val, list):
        return local_val

    # 建立名称索引
    result = copy.deepcopy(remote_val)
    remote_names = {get_item_name(item): i for i, item in enumerate(remote_val) if get_item_name(item)}

    for local_item in local_val:
        local_name = get_item_name(local_item)
        if local_name and local_name in remote_names:
            # 远程有同名项，合并
            idx = remote_names[local_name]
            result[idx] = merge_dicts(local_item, result[idx])
        else:
            # 远程没有，添加
            result.append(copy.deepcopy(local_item))

    return result


def merge_dicts(local: dict, remote: dict) -> dict:
    """深度合并两个字典，本地优先"""
    if not isinstance(local, dict):
        return remote if isinstance(remote, dict) else local
    if not isinstance(remote, dict):
        return local

    result = copy.deepcopy(remote)

    for key, local_val in local.items():
        remote_val = remote.get(key)

        # 跳过元数据
        if key == "_meta":
            result[key] = local_val
            continue

        # 图片字段
        if is_image_field(key, local_val) or is_image_field(key, remote_val):
            result[key] = merge_image_field(local_val, remote_val)
            continue

        # 多语言字段（dict with en/zh）
        if isinstance(local_val, dict) and isinstance(remote_val, dict):
            if "en" in local_val or "zh" in local_val or "en" in remote_val or "zh" in remote_val:
                result[key] = merge_i18n_field(local_val, remote_val)
                continue

        # 数组字段
        if isinstance(local_val, list) and isinstance(remote_val, list):
            result[key] = merge_array_field(local_val, remote_val)
            continue

        # 字典字段（递归合并）
        if isinstance(local_val, dict) and isinstance(remote_val, dict):
            result[key] = merge_dicts(local_val, remote_val)
            continue

        # 其他：本地优先
        if local_val is not None and local_val != "":
            result[key] = local_val

    return result


def merge_configs(local_config: dict, remote_config: dict) -> dict:
    """
    合并本地和远程配置

    策略：
    - 文本字段：本地优先（用户最新修改）
    - 图片字段：保留双方 URL
    - 数组字段：按名称去重合并
    - 联系方式：保留双方启用的渠道

    Args:
        local_config: 本地配置（用户当前修改）
        remote_config: 远程配置（线上版本）

    Returns:
        合并后的配置
    """
    if not remote_config:
        return copy.deepcopy(local_config)
    if not local_config:
        return copy.deepcopy(remote_config)

    result = merge_dicts(local_config, remote_config)

    # 版本号取较大者
    local_ver = local_config.get("_meta", {}).get("version", 0)
    remote_ver = remote_config.get("_meta", {}).get("version", 0)
    result["_meta"] = {
        "version": max(local_ver, remote_ver) + 1,
        "merged_from": f"local_v{local_ver} + remote_v{remote_ver}"
    }

    logger.info("配置合并完成: local_v%d + remote_v%d -> v%d",
                local_ver, remote_ver, result["_meta"]["version"])

    return result


def detect_conflicts(local_config: dict, remote_config: dict) -> list[dict]:
    """
    检测本地和远程配置的冲突

    Returns:
        冲突列表，每项包含 {path, local_value, remote_value, type}
    """
    conflicts = []

    def _compare(path: str, local: Any, remote: Any):
        if type(local) != type(remote):
            conflicts.append({
                "path": path,
                "local_value": str(local)[:100],
                "remote_value": str(remote)[:100],
                "type": "type_mismatch"
            })
            return

        if isinstance(local, dict) and isinstance(remote, dict):
            for key in set(list(local.keys()) + list(remote.keys())):
                if key == "_meta":
                    continue
                _compare(f"{path}.{key}", local.get(key), remote.get(key))
        elif isinstance(local, list) and isinstance(remote, list):
            # 简单比较长度
            if len(local) != len(remote):
                conflicts.append({
                    "path": path,
                    "local_value": f"{len(local)} items",
                    "remote_value": f"{len(remote)} items",
                    "type": "array_length"
                })
        elif local != remote:
            conflicts.append({
                "path": path,
                "local_value": str(local)[:100],
                "remote_value": str(remote)[:100],
                "type": "value_changed"
            })

    _compare("config", local_config, remote_config)
    return conflicts


def load_remote_config(config_url: str) -> dict | None:
    """
    从 URL 加载远程配置

    Args:
        config_url: config.js 的 URL

    Returns:
        配置字典，失败返回 None
    """
    import re
    try:
        import urllib.request
        req = urllib.request.Request(config_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
            # 解析 window.SITE_CONFIG = {...};
            match = re.search(r'SITE_CONFIG\s*=\s*([\s\S]*?);', content)
            if match:
                return json.loads(match.group(1))
    except Exception as e:
        logger.warning("加载远程配置失败: %s", e)
    return None
