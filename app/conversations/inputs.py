"""轮次输入的规范化表示。

同一 request_id 的重复提交必须比较完整输入：操作类型、题干、附件及角色、
学生解法、来源和题目引用。规范化 JSON 由服务层生成、仓库原样保存；迁移旧
F004 轮次时也使用这里的函数，保证回放时能命中同一份输入。
"""

import json
from typing import Any


def canonical_json(payload: dict[str, Any]) -> str:
    """稳定、紧凑、保留中文的 JSON；键排序，避免字典顺序影响比较。"""

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def query_input(text: str) -> str:
    """旧 F004 查询轮次的规范化输入；迁移回填必须与运行时完全一致。"""

    return canonical_json({"kind": "query", "text": text})
