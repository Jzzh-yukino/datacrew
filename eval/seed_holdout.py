"""把留出题写入 eval.queries（split='holdout' / 'holdout_v2'）并算金标哈希。

先提交题目文件，再运行本脚本；跑完评测后不得针对留出失败题改代码。

用法：
    python -m eval.seed_holdout              # v1，30 题，split='holdout'
    python -m eval.seed_holdout --version v2 # R2 留出集 v2，30 题，split='holdout_v2'
"""
from __future__ import annotations

import argparse
import asyncio

from app.infra.db import admin_pool, init_pools
from eval.holdout_set import seed_holdout, seed_holdout_v2


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", choices=("v1", "v2"), default="v1")
    args = parser.parse_args()
    await init_pools()
    async with admin_pool().acquire() as conn:
        n = await (seed_holdout_v2(conn) if args.version == "v2" else seed_holdout(conn))
    split = "holdout_v2" if args.version == "v2" else "holdout"
    print(f"holdout {args.version} 已入库：{n} 题（split={split}）")


if __name__ == "__main__":
    asyncio.run(main())
