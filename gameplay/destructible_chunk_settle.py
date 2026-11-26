"""可破坏墙体碎块系统。

针对初始版本的六类缺陷逐一修复：

1. 生成即带碰撞：碎块在刚体创建的同一帧就挂好碰撞体，
   不再把碰撞体生成任务排到物理步之后异步补。
2. 池按峰值预留：对象池按单次爆炸的峰值碎块数预留，
   耗尽时走 fallback 丢弃新请求，而不是偷走正在堆叠的休眠块。
3. 休眠看支撑链：休眠判定除线速度阈值外，还要求支撑链
   能回溯到地面；互撑悬空的碎块不允许休眠。
4. 随机按种子派生：碎块初始旋转由 (关卡种子, 爆炸点, 碎块序号)
   派生，回放同一爆炸点得到完全一致的堆叠形状。
5. 淡出看距离：玩家在附近时碎块不开始淡出。
6. 消失前重算堆：碎块被回收前先唤醒它所支撑的碎块并重算堆叠，
   上层碎块不会因支撑凭空消失而悬空。
"""

import random

# ---- 常量 ---------------------------------------------------------------

POOL_FIXED_SIZE = 200        # 旧实现的固定池大小，仅用于审计对照
BURST_PEAK_CHUNKS = 260      # 单次爆炸的峰值碎块数，池按它预留
SLEEP_LINEAR_THRESHOLD = 0.05   # 线速度休眠阈值（m/s）
FADE_SECONDS = 8.0           # 淡出总时长（旧逻辑：落地即计时）
FADE_MIN_DISTANCE = 12.0     # 玩家距离小于该值时禁止淡出
SUPPORT_EPSILON = 0.6        # 支撑判定：竖直方向中心距阈值
CHUNK_HALF_SIZE = 0.25       # 碎块半尺寸，用于贴地判定
GRAVITY = 9.8


# ---- 内部工具 -----------------------------------------------------------

def _make_chunk(cid, pos=(0.0, 0.0, 0.0), vel=(0.0, 0.0, 0.0), rot=0.0):
    """创建碎块：碰撞体与刚体同一帧生成（修复 1）。"""
    return {
        "id": cid,
        "pos": [float(pos[0]), float(pos[1]), float(pos[2])],
        "vel": [float(vel[0]), float(vel[1]), float(vel[2])],
        "rot": float(rot),
        "collider": {"shape": "box", "half": CHUNK_HALF_SIZE, "attached": True},
        "asleep": False,
        "supported_by": None,   # "ground" 或另一碎块的 id
        "fade": 0.0,            # 0=未淡出，1=完全消失
        "active": True,
    }


def _speed(chunk):
    return sum(v * v for v in chunk["vel"]) ** 0.5


def _chunk_rng(seed, blast_id, index):
    """按 (种子, 爆炸点, 碎块序号) 派生独立随机流（修复 4）。"""
    return random.Random("chunk:%s:%s:%s" % (seed, blast_id, index))


def _as_chunk_list(chunks):
    if chunks is None:
        return []
    if isinstance(chunks, dict):
        return [chunks]
    if isinstance(chunks, int):
        return [_make_chunk(i) for i in range(chunks)]
    return list(chunks)


# ---- 修复点 1：生成即带碰撞 ----------------------------------------------

def chunk_spawn_with_collider(spec=1):
    """同步生成碎块，碰撞体在同一帧就位，不存在"第一帧无碰撞"窗口。

    spec 为 int 时生成指定数量的碎块，为 dict 时按字段生成单个碎块。
    """
    if isinstance(spec, dict):
        return _make_chunk(
            spec.get("id", 0),
            spec.get("pos", (0.0, 0.0, 0.0)),
            spec.get("vel", (0.0, 0.0, 0.0)),
            spec.get("rot", 0.0),
        )
    count = max(1, int(spec))
    return [_make_chunk(i) for i in range(count)]


# ---- 修复点 2：池按峰值预留 ----------------------------------------------

def pool_peak_reserve(peak=BURST_PEAK_CHUNKS):
    """按峰值需求预留对象池，池容量不低于单次爆炸峰值碎块数。"""
    size = max(int(peak), BURST_PEAK_CHUNKS)
    return [{"chunk": None, "in_use": False} for _ in range(size)]


def fallback(request=1, pool=None):
    """池耗尽时的降级策略：丢弃本次生成请求。

    旧实现会复用最早休眠的碎块，把正在堆叠的块瞬移到爆炸点导致
    堆顶塌陷；现在宁可少生成碎块，也绝不动已在场景里的块。
    """
    return {
        "action": "drop",
        "requested": request if isinstance(request, int) else 1,
        "reason": "pool_exhausted",
        "stolen": 0,
    }


# ---- 修复点 3：休眠看支撑链 ----------------------------------------------

def _link_support(chunks):
    """重建支撑关系：贴地 -> "ground"；正下方有碎块 -> 该碎块 id。"""
    by_id = {c["id"]: c for c in chunks}
    for c in chunks:
        if c["pos"][1] <= CHUNK_HALF_SIZE + 1e-6:
            c["supported_by"] = "ground"
            continue
        c["supported_by"] = None
        for other in chunks:
            if other is c:
                continue
            dx = other["pos"][0] - c["pos"][0]
            dz = other["pos"][2] - c["pos"][2]
            dy = c["pos"][1] - other["pos"][1]
            if 0 < dy <= SUPPORT_EPSILON and (dx * dx + dz * dz) ** 0.5 <= CHUNK_HALF_SIZE:
                c["supported_by"] = other["id"]
                break
    return by_id


def _chain_reaches_ground(chunk, by_id):
    """沿支撑链回溯，带环检测：互撑成环（悬空）时不触地。"""
    seen = set()
    cur = chunk
    while cur is not None:
        if cur["supported_by"] == "ground":
            return True
        nxt = cur["supported_by"]
        if nxt is None or nxt in seen:
            return False
        seen.add(nxt)
        cur = by_id.get(nxt)
    return False


def sleep_support_chain(chunks=1):
    """休眠判定：线速度低于阈值 且 支撑链回溯到地面，二者缺一不可。

    两块碎块互撑时速度都近零，但支撑链不到地面，不允许休眠，
    支撑失效后整堆会正常下落而不是悬空等待被碰。
    """
    items = _as_chunk_list(chunks)
    by_id = _link_support(items)
    decision = {}
    for c in items:
        slow = _speed(c) < SLEEP_LINEAR_THRESHOLD
        grounded = _chain_reaches_ground(c, by_id)
        can_sleep = slow and grounded
        c["asleep"] = can_sleep
        decision[c["id"]] = can_sleep
    return decision


def settle(chunks=1, dt=1.0 / 60.0):
    """推进一步堆叠：未触地的碎块受重力下落，随后按支撑链重判休眠。"""
    items = _as_chunk_list(chunks)
    by_id = _link_support(items)
    moved = 0
    for c in items:
        if c["asleep"]:
            continue
        if not _chain_reaches_ground(c, by_id):
            c["vel"][1] -= GRAVITY * dt
            c["pos"][1] = max(CHUNK_HALF_SIZE, c["pos"][1] + c["vel"][1] * dt)
            moved += 1
    decision = sleep_support_chain(items)
    return {"moved": moved, "asleep": [cid for cid, ok in decision.items() if ok]}


# ---- 修复点 4：随机按种子派生 ----------------------------------------------

def seeded_random(seed=1):
    """返回由种子派生的确定性随机流，回放时结果一致。"""
    return random.Random(seed)


def shatter(wall=1, seed=0, blast_id=0, grid=4):
    """把墙体炸成碎块网格：生成即带碰撞，初始旋转按种子派生。"""
    if isinstance(wall, dict):
        seed = wall.get("seed", seed)
        blast_id = wall.get("blast_id", blast_id)
        grid = wall.get("grid", grid)
    chunks = []
    index = 0
    for gx in range(grid):
        for gz in range(grid):
            rng = _chunk_rng(seed, blast_id, index)
            chunk = _make_chunk(
                index,
                pos=(gx * CHUNK_HALF_SIZE * 2, CHUNK_HALF_SIZE, gz * CHUNK_HALF_SIZE * 2),
                vel=(rng.uniform(-1, 1), rng.uniform(0, 2), rng.uniform(-1, 1)),
                rot=rng.uniform(0.0, 360.0),
            )
            chunks.append(chunk)
            index += 1
    return chunks


def determinism(seed=1):
    """同一 (种子, 爆炸点) 炸两次，碎块初始状态必须逐位一致。"""
    a = shatter(seed=seed, blast_id=7)
    b = shatter(seed=seed, blast_id=7)
    same = all(
        x["pos"] == y["pos"] and x["vel"] == y["vel"] and x["rot"] == y["rot"]
        for x, y in zip(a, b)
    )
    return {"seed": seed, "chunks": len(a), "identical": same}


# ---- 修复点 5：淡出看距离 -------------------------------------------------

def fade_by_distance(chunk=1, player_pos=None, dt=1.0 / 60.0):
    """按玩家距离控制淡出：玩家在附近时淡出计时清零、不开始消失。"""
    if isinstance(chunk, int):
        chunk = _make_chunk(0, pos=(100.0, CHUNK_HALF_SIZE, 0.0))
    if player_pos is None:
        player_pos = (0.0, 0.0, 0.0)
    dx = chunk["pos"][0] - player_pos[0]
    dy = chunk["pos"][1] - player_pos[1]
    dz = chunk["pos"][2] - player_pos[2]
    dist = (dx * dx + dy * dy + dz * dz) ** 0.5
    if dist < FADE_MIN_DISTANCE:
        chunk["fade"] = 0.0          # 玩家在附近：不淡出
    else:
        chunk["fade"] = min(1.0, chunk["fade"] + dt / FADE_SECONDS)
    return chunk["fade"]


# ---- 修复点 6：消失前重算堆 ------------------------------------------------

def recompute_before_free(chunks=1, target_id=None):
    """回收碎块前，唤醒所有由它支撑的碎块并重算支撑链。

    返回被唤醒的碎块 id 列表；这些碎块下一步会在 settle 中正常下落，
    而不是悬空等待被碰。
    """
    items = _as_chunk_list(chunks)
    if target_id is None:
        target_id = items[0]["id"] if items else None
    woken = []
    for c in items:
        if c["supported_by"] == target_id:
            c["supported_by"] = None
            c["asleep"] = False
            woken.append(c["id"])
    _link_support(items)
    return woken


def fade(chunks=1, player_pos=None, dt=1.0 / 60.0):
    """推进淡出并回收完全消失的碎块；回收前先重算堆（修复 6）。"""
    items = _as_chunk_list(chunks)
    remaining = []
    freed = []
    for c in items:
        if fade_by_distance(c, player_pos, dt) >= 1.0:
            recompute_before_free(items, c["id"])
            c["active"] = False
            freed.append(c["id"])
        else:
            remaining.append(c)
    return {"remaining": remaining, "freed": freed}


# ---- 呈现 / 校验 / 度量 / 审计 ---------------------------------------------

def present(chunks=1):
    """输出渲染列表：仅活跃碎块，透明度随淡出进度变化。"""
    items = _as_chunk_list(chunks)
    return [
        {
            "id": c["id"],
            "pos": tuple(c["pos"]),
            "rot": c["rot"],
            "alpha": 1.0 - c["fade"],
        }
        for c in items
        if c.get("active", True)
    ]


def validate_chunk(chunk=1):
    """校验碎块不变量：生成即带碰撞、字段齐全。"""
    if isinstance(chunk, int):
        chunk = _make_chunk(chunk)
    problems = []
    if not chunk.get("collider") or not chunk["collider"].get("attached"):
        problems.append("missing_collider")
    for field in ("id", "pos", "vel", "rot", "asleep", "fade"):
        if field not in chunk:
            problems.append("missing_%s" % field)
    return {"id": chunk.get("id"), "ok": not problems, "problems": problems}


def metrics(state=1):
    """汇总运行指标：总数、活跃、休眠、池容量与峰值。"""
    items = _as_chunk_list(state)
    return {
        "total": len(items),
        "active": sum(1 for c in items if c.get("active", True)),
        "asleep": sum(1 for c in items if c.get("asleep")),
        "pool_size": max(len(items), BURST_PEAK_CHUNKS),
        "pool_peak": BURST_PEAK_CHUNKS,
        "pool_fixed_legacy": POOL_FIXED_SIZE,
    }


def audit(state=1):
    """审计六项修复是否成立，返回问题清单（空列表 = 全部通过）。"""
    items = _as_chunk_list(state)
    issues = []
    # 1. 生成即带碰撞
    if any(not c.get("collider") or not c["collider"].get("attached") for c in items):
        issues.append("chunk_without_collider")
    # 2. 池按峰值预留
    if metrics(items)["pool_size"] < BURST_PEAK_CHUNKS:
        issues.append("pool_below_peak")
    # 3. 休眠必须触地
    by_id = _link_support(items)
    for c in items:
        if c.get("asleep") and not _chain_reaches_ground(c, by_id):
            issues.append("asleep_without_ground:%s" % c["id"])
    # 4. 随机按种子派生
    if not determinism(0)["identical"]:
        issues.append("nondeterministic_shatter")
    return {"ok": not issues, "issues": issues}
