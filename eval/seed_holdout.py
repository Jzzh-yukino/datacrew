"""把 RW-2 留出题写入 eval.queries（split='holdout'）并算金标哈希。

先提交 `eval/holdout_set.py`，再运行本脚本；跑完评测后不得针对留出失败题改代码。
"""
from __future__ import annotations

import asyncio

from app.infra.db import admin_pool, init_pools
from eval.holdout_set import seed_holdout


async def main() -> None:
    await init_pools()
    async with admin_pool().acquire() as conn:
        n = await seed_holdout(conn)
    print(f"holdout 已入库：{n} 题（split=holdout）")


if __name__ == "__main__":
    asyncio.run(main())
