# pairwise-destructible-chunk-settle

## 领域

游戏、图形与多媒体

## 主题

破坏碎块生成与堆叠

## 说明

生成即碰撞、支撑链休眠与种子随机。

初始状态下 `gameplay/destructible_chunk_settle.py` 中所有函数抛出 `NotImplementedError`，测试全部失败。
修复实现后运行：

```bash
python -m pytest -q
```

期望全部通过。
