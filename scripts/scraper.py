#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
崩坏星穹铁道 · 终局玩法血量膨胀数据抓取器
数据源: hsr.nanoka.cc (SvelteKit 静态 JSON)

抓取四种终局玩法各【正式服】版本的最高难度血量：
  - 混沌回忆   (moc,  /maze)
  - 虚构叙事   (pf,   /story)
  - 末日幻影   (as,   /boss)
  - 异相仲裁   (peak, /peak)

血量公式（与站点源码一致）：
  怪物本体HP = HPBase × HPModifyRatio × 精英组HPRatio × 等级HPRatio × Σ(阶段倍率)
  - 普通战斗用 EliteGroup.json 的 HPRatio
  - 无限刷怪战斗用 InfiniteEliteGroup.json 的 HPRatio
  - Boss 多阶段乘以 PhaseList 各 phase_max_hp_ratio 之和

贪饕侵蚀（invasion）：
  怪物本体被打死后会额外出现一条「贪饕血条」，按等级取不同的初始上限，
  且玩家对它造成伤害时，当前血量按上限的 2 倍速度下降
  （1 点伤害 = 当前血量 -2、最大生命上限 -1）。
  因此站点原来那种 `hp × (1 + maze_buff_param[1])` 的写法是错的，见下方
  INVASION_* 常量与 invasion_extra_damage() 的完整说明。
"""

import json
import os
import re
import subprocess
import sys
import time
import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
SITE = "https://hsr.nanoka.cc"
STATIC = "https://static.nanoka.cc/hsr"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA})

# 每个玩法抓取用到的参考数据（懒加载缓存）
_cache = {}


# ---------------------------------------------------------------------------
# 贪饕侵蚀（invasion）模型
# ---------------------------------------------------------------------------
# 机制（依据游戏内实测，2026-10 校准）：
#   1. 怪物本体被打死后并不会消失，而是进入「贪饕血条」状态，获得一条额外的血条。
#   2. 贪饕血条的初始最大生命：
#        - 单阶段一条命的怪：本体总血量 × k
#        - 多阶段多条命的怪：最后一阶段血量 × k
#   3. 玩家对贪饕血条造成伤害时，会同时扣除「当前血量」与「最大生命上限」，
#      且当前血量的下降速度是上限的 2 倍：
#        1 点伤害 = 扣 2 点当前血量 + 扣 1 点最大生命上限。
#      例：贪饕条 100/100，打 5 点伤害 → 上限 95、当前 90。
#      因此打空当前血量需要 M/2 点伤害，打完后上限恰好剩 M/2。
#   4. 只有当「贪饕血条归零」且「此刻的最大生命上限已低于初始上限的 θ」时，
#      怪物才会被彻底消灭；否则贪饕条会直接回满到当前的最大生命上限，循环下去。
#
# 由 2/3/4 可以推出「打空一条 M 的贪饕条」需要 M/2 点伤害，
# 且打完后最大生命也恰好降到 M/2。于是轮次完全由 θ 决定：
#   θ = 0.60 → 0.5 < 0.6，一轮打死           → 消耗 0.500 × M0
#   θ = 0.40 → 0.5 未达标，二轮 0.25 < 0.4    → 消耗 0.750 × M0
#   θ = 0.23 → 0.5、0.25 连续未达标，三轮 0.125 → 消耗 0.875 × M0
#
# 站点要展示的是「通关所需的总伤害」，因此：
#   最终血量 = 本体血量（各阶段之和） + 贪饕条等效伤害（消耗比例 × M0）
# ---------------------------------------------------------------------------

# 贪饕等级（invasion.level，直接取上游数字，不做任何偏移）→ 贪饕血条初始上限系数
# 注：终局玩法实际只会出现 2/3/4 级（1 级为理论预留，保留以便将来上游启用）。
INVASION_K_SINGLE = {1: 0.20, 2: 0.40, 3: 0.80, 4: 1.20}   # 单阶段：本体血量 × k
INVASION_K_MULTI = {1: 0.40, 2: 0.72, 3: 1.44, 4: 1.80}    # 多阶段：最后一阶段血量 × k

# 贪饕等级 → 彻底消灭所需的「最大生命 / 初始上限」阈值
INVASION_THRESHOLD = {1: 0.60, 2: 0.60, 3: 0.40, 4: 0.23}

# 「哪些怪带贪饕」的唯一判据：上游 event.invasion.monster_list 里标记的怪物 ID。
# 不在名单里的怪，即使同场、即使长得像，也一律视为无贪饕（不做任何推断兜底）。
# 因此末日幻影那种「名单列配套精英、stage 只出场 BOSS」的结构会自然落到「无贪饕」，
# 这是符合预期的结果，不是 bug。


def invasion_spent(level):
    """打空一条贪饕血条需要消耗的等效伤害（以初始上限 M0 为单位）。

    返回 (消耗比例, 轮数)。例如 3 级 → (0.75, 2)，表示要打穿两条。
    """
    th = INVASION_THRESHOLD.get(level)
    if th is None:
        return 0.0, 0
    m = 1.0       # 当前最大生命 / M0
    spent = 0.0   # 已消耗伤害 / M0
    rounds = 0
    while True:
        d = m / 2.0      # 打空当前血量所需伤害：每点伤害扣 2 点进度
        spent += d
        m -= d           # 最大生命同步流失，打完后恰好减半
        rounds += 1
        if m < th:       # 归零时上限已低于阈值 → 彻底消灭
            break
        # 未达标：贪饕条回满到当前上限，继续下一轮
        if rounds >= 10:   # 防御性上限，正常永不到达
            break
    return spent, rounds


# ---------------------------------------------------------------------------
# BOSS 阶段比例修正（上游 monstervalue 的 PhaseList 与游戏内实测不符时用）
# ---------------------------------------------------------------------------
# key = monstervalue 键（怪物 ID 前 7 位）
#   ratios       : 各阶段血条比例，取代上游 PhaseList
#   phase1_scale : 实测第一阶段 / 上游算出第一阶段 的修正系数（上游基础值偏低时用）
PHASE_OVERRIDE = {
    # 亚婆离：上游 MaxMonsterPhase / PhaseList 均为空（按单阶段处理），
    # 但游戏内实测是两条血，第二条 = 第一条 × 2。
    "5034010": {
        "ratios": [1.0, 2.0],
        "phase1_scale": 1.0,
        "note": "亚婆离 2 阶段，实测比例 1:2",
    },
    # 归寂：上游 PhaseList = [1.0, 1.0, 1.9]（和 3.9），与实测不符。
    # 实测三阶段 16,768,365 / 20,122,038 / 27,667,802，比例精确为 1 : 1.2 : 1.65。
    # phase1_scale 保持 1.0：上游 child 801503006 的 HPModifyRatio=0.375 已经能精确
    # 还原实测第一阶段（14880 × 0.375 × 6.2 × 484.690862 = 16,768,365），
    # 再加任何缩放都会重复放大（曾误乘 8/7，导致三阶段总值虚高 12.5%）。
    "8015030": {
        "ratios": [1.0, 1.2, 1.65],
        "phase1_scale": 1.0,
        "note": "归寂 3 阶段，实测比例 1:1.2:1.65",
    },
}


def resolve_phases(mv_key, mv):
    """返回 (各阶段比例列表, 第一阶段修正系数)。

    优先使用 PHASE_OVERRIDE 的实测比例，否则用上游 PhaseList。
    """
    ov = PHASE_OVERRIDE.get(str(mv_key))
    if ov:
        return [float(x) for x in ov["ratios"]], float(ov.get("phase1_scale", 1.0))
    phase = (mv or {}).get("PhaseList") or []
    ratios = [float(p.get("phase_max_hp_ratio", 1) or 1) for p in phase]
    return (ratios if ratios else [1.0]), 1.0



def fetch_json(url, retries=3, timeout=60):
    """抓取 JSON，带重试"""
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(1 + attempt)
    return None


def fetch_text(url, retries=3, timeout=60):
    for attempt in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout)
            r.raise_for_status()
            return r.text
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(1 + attempt)
    return None


def detect_data_version():
    """解析当前数据包版本号（如 4.5.54）

    首页 data-url 指向的就是官方最新数据包（含正式服已开放的赛季）。
    正式服 / 未上线的区分交给 detect_live_versions() 依据 live_begin 判定，
    这里不再对包号做向下收敛（那样会丢掉最新一期正式服数据）。
    """
    html = fetch_text(f"{SITE}/maze/")
    m = re.search(r"/hsr/([\d.]+)/", html)
    if not m:
        raise RuntimeError("无法从首页解析数据版本号")
    return m.group(1)


def load_reference(ver, name):
    """加载并缓存参考数据文件

    注意：缓存键必须带版本号前缀。早期实现只用文件名作键，
    一旦跨版本调用就会命中上一版本的数据，造成血量为旧版本的值。
    """
    key = f"{ver}/{name}"
    if key not in _cache:
        url = f"{STATIC}/{ver}/{name}"
        _cache[key] = fetch_json(url)
    return _cache[key]


def build_lookup_tables(ver):
    """构建血量计算所需的查找表"""
    monstervalue = load_reference(ver, "monstervalue.json")
    monster = load_reference(ver, "monster.json")
    hard_level = load_reference(ver, "HardLevelGroup.json")
    elite_group = load_reference(ver, "EliteGroup.json")
    infinite_elite = load_reference(ver, "InfiniteEliteGroup.json")

    hard_map = {}
    for r in hard_level:
        hard_map[(r["HardLevelGroup"], r["Level"])] = r.get("HPRatio", 1)

    elite_map = {r["EliteGroup"]: r.get("HPRatio", 1) for r in elite_group}
    infinite_elite_map = {r["EliteGroup"]: r.get("HPRatio", 1) for r in infinite_elite}

    return {
        "monstervalue": monstervalue,
        "monster": monster,
        "hard_map": hard_map,
        "elite_map": elite_map,
        "infinite_elite_map": infinite_elite_map,
    }


def calc_monster_hp(mid, elite_gid, hard_g, level, tables, is_infinite=False, infinite_bonus=0.0, invasion_level=0):
    """计算单个怪物的「通关所需总血量」= 本体各阶段之和 + 贪饕血条等效伤害

    - infinite_bonus: 无限刷怪波次 param_list[1] 血量加成
    - invasion_level: 贪饕侵蚀等级（1~4），0 表示未被侵蚀
    """
    key = str(mid)[:7]  # 怪物 ID 取前 7 位作为 monstervalue 键
    mv = tables["monstervalue"].get(key)
    if not mv:
        return 0.0

    hp_base = float(mv.get("HPBase", 0) or 0)

    # child 匹配（精确 Id > 数字相等 > child[0]）
    child = mv.get("child") or []
    m = None
    for c in child:
        if int(c.get("Id", 0)) == int(mid):
            m = c
            break
    if m is None:
        for c in child:
            if str(c.get("Id", "")) == key:
                m = c
                break
    if m is None and child:
        m = child[0]

    hp_modify = float(m.get("HPModifyRatio", 1) or 1) if m else 1.0

    # 精英组倍率：先查普通 EliteGroup，找不到且是无限战斗才回退 InfiniteEliteGroup
    # （站点源码逻辑：eliteGroupMap.get(id) ?? infiniteEliteGroupMap.get(id)）
    elite_ratio = 1.0
    if elite_gid in tables["elite_map"]:
        elite_ratio = float(tables["elite_map"][elite_gid] or 1)
    elif is_infinite and elite_gid in tables["infinite_elite_map"]:
        elite_ratio = float(tables["infinite_elite_map"][elite_gid] or 1)

    # 等级倍率
    hard_ratio = float(tables["hard_map"].get((hard_g, level), 1) or 1)

    hp = hp_base * hp_modify * elite_ratio * hard_ratio

    # 无限刷怪波次的血量加成：×(1 + param_list[1])（虚构叙事等玩法）
    hp *= 1.0 + float(infinite_bonus or 0)

    # Boss 多阶段：各阶段血条比例之和（PHASE_OVERRIDE 可覆盖上游配错的情况）
    ratios, phase1_scale = resolve_phases(key, mv)
    ratios = [r * phase1_scale for r in ratios]
    hp_last_phase = hp * ratios[-1]          # 最后一阶段血量（多阶段贪饕血条的基准）
    hp *= sum(ratios)                        # 本体总血量 = 各阶段之和

    # 贪饕侵蚀：本体打死后多一条贪饕血条，按等级追加「等效消耗伤害」
    if invasion_level:
        hp += invasion_extra_damage(hp, hp_last_phase, len(ratios), invasion_level)

    return hp


def invasion_extra_damage(hp_body, hp_last_phase, phase_count, level):
    """贪饕侵蚀额外需要造成的伤害（等效血量）。

    - 单阶段怪：贪饕条初始上限 M0 = 本体血量 × INVASION_K_SINGLE[level]
    - 多阶段怪：贪饕条初始上限 M0 = 最后一阶段血量 × INVASION_K_MULTI[level]
    再乘以打空所需的消耗比例（见 invasion_spent）。
    """
    multi = phase_count > 1
    k = (INVASION_K_MULTI if multi else INVASION_K_SINGLE).get(level)
    if not k:
        return 0.0
    m0 = (hp_last_phase if multi else hp_body) * k
    spent, _ = invasion_spent(level)
    return m0 * spent



def clean_text(s):
    """清理 HTML 标签（如 <unbreak>01</unbreak>、<color=#xx>）"""
    if not isinstance(s, str):
        return s
    return re.sub(r"<[^>]+>", "", s)


def monster_rank(mid, tables):
    """获取怪物 rank（用于区分 Elite/BOSS 与小怪）"""
    key = str(mid)[:7]
    m = tables["monster"].get(str(mid)) or tables["monster"].get(key) or {}
    return m.get("rank") or ""


def monster_name(mid, tables):
    key = str(mid)[:7]
    m = tables["monster"].get(str(mid)) or tables["monster"].get(key) or {}
    return m.get("zh") or m.get("en") or ""


def monster_entry(mid, hp, invasion_level, tables, contribution=None, count=1):
    """构造一只怪物的明细条目（供前端核对「总血量是怎么组成的」）

    - hp     单只怪物血量（含贪饕血条折算的等效值）
    - contrib 这一行实际计入本波总量的血量。多数情况等于 hp × count，
              但虚构叙事第二/三波只把 BOSS 的 40% 计入，小怪按 10 只计入。
    - count  本波内该怪出现的只数（同一 ID 多次出现合并成一行）
    """
    key = str(mid)[:7]
    mv = tables["monstervalue"].get(key) or {}
    try:
        ratios, _ = resolve_phases(key, mv)
        phases = len(ratios)
    except Exception:
        phases = 1
    return {
        "id": str(mid),
        "name": monster_name(mid, tables) or "",
        "hp": round(hp),
        "contrib": round(contribution if contribution is not None else hp * count),
        "count": count,
        "phases": phases,
        "inv": int(invasion_level or 0),
    }


def sum_contrib(waves):
    """把明细里的波次贡献加起来（用于一致性断言）"""
    return sum(m.get("contrib", 0) for w in waves for m in w.get("monsters", []))


def event_monster_ids(event):
    """提取 event 的 monster_list 里所有怪物 ID"""
    ids = []
    for wave in event.get("monster_list") or []:
        for k, v in wave.items():
            if k.startswith("monster"):
                ids.append(v)
    return ids


# ---------------------------------------------------------------------------
# 混沌回忆 (moc)
# ---------------------------------------------------------------------------
def fetch_chaos_memory(ver, tables, live_versions, details=None):
    """混沌回忆：每版本最高层，星启三路(或两路)怪物血量总和"""
    maze = load_reference(ver, "maze.json")
    version_map = fetch_json(f"{STATIC}/{ver}/zh/maze/version.json")

    data = []
    for version, ids in version_map.items():
        if version == "static":
            continue
        if version not in live_versions:
            continue
        season_id = str(ids[0])
        detail = fetch_json(f"{STATIC}/{ver}/zh/maze/{season_id}.json")
        if not isinstance(detail, list) or not detail:
            continue

        # 普通层（有 name）与星启元素（有 pre_id）
        normal = [e for e in detail if e.get("name")]
        star = [e for e in detail if "pre_id" in e]

        if not normal:
            continue

        # 星启元素挂载到 pre_id 指向的最高层
        star_by_pre = {e["pre_id"]: e for e in star}

        # 最高层 = 最后一个普通层
        top = normal[-1]

        lanes_hp = []
        lanes_detail = [] if details is not None else None
        for key in ("event_id_list1", "event_id_list2"):
            for event in top.get(key, []):
                wd = [] if details is not None else None
                hp = event_total_hp(event, tables, is_infinite=False, detail=wd)
                lanes_hp.append(hp)
                if lanes_detail is not None:
                    lanes_detail.append({"label": f"第 {len(lanes_hp)} 路", "hp": round(hp), "waves": wd})

        # 星启第三路：仅当本版本详情里确实带有星启副本时才计入。
        # maze/version.json 并不随星启开放在本期同步更新，尚未开星启的版本
        # 容易误命中上一期遗留的 star 元素，从而把上一期星启血量错记到本期。
        star_elem = star_by_pre.get(top["id"])
        if star_elem:
            for event in star_elem.get("event_id_list", []):
                wd = [] if details is not None else None
                hp = event_total_hp(event, tables, is_infinite=False, detail=wd)
                lanes_hp.append(hp)
                if lanes_detail is not None:
                    lanes_detail.append({"label": f"第 {len(lanes_hp)} 路（星启）", "hp": round(hp), "waves": wd})

        total_hp = sum(lanes_hp)
        name = maze.get(season_id, {}).get("zh") or top.get("name") or ""
        data.append({
            "version": version,
            "seasonId": season_id,
            "name": clean_text(name),
            "hp": round(total_hp),
            "lanes": len(lanes_hp),
            "lanesHp": [round(x) for x in lanes_hp],
        })
        if details is not None:
            details[season_id] = {"name": clean_text(name), "total": round(total_hp), "lanes": lanes_detail}

    data.sort(key=lambda x: version_sort_key(x["version"]))
    return data


# ---------------------------------------------------------------------------
# 版本号对齐：把末日/虚构的赛季号映射为与混沌回忆一致的游戏版本号
# ---------------------------------------------------------------------------
# 锚点依据（三重印证）：
#   1) 4.3 官方更新说明列出当期三玩法为「末日·遗忘冽风 / 虚构·借虚成真 / 混沌·学院怪谈」，
#      与本项目 s3018 / s2024 / 1033 的名称逐一对应；
#   2) 星启模式自 4.3 起上线，三者恰在同期首次出现星启关卡；
#   3) 各玩法在 4.x 期间均为一版本一期。
# 由此：末日 s3018=4.3、虚构 s2024=4.3，最新一期（s3020 / s2026）即 4.5。
ALIGN_ANCHORS = {
    "apocalypticShadow": ("3018", "4.3"),
    "pureFiction": ("2024", "4.3"),
}


def align_versions(result):
    """把末日幻影 / 虚构叙事的赛季按锚点回填为游戏版本号（与混沌回忆一致）"""
    chaos = result.get("chaosMemory", {}).get("data") or []
    chaos_versions = [p["version"] for p in chaos]
    if not chaos_versions:
        return result

    for mode_key, (anchor_sid, anchor_ver) in ALIGN_ANCHORS.items():
        mode = result.get(mode_key)
        if not mode:
            continue
        items = mode.get("data") or []
        if not items:
            continue

        # 锚点所在索引
        aidx = next((i for i, p in enumerate(items) if str(p.get("seasonId")) == anchor_sid), None)
        if aidx is None:
            continue
        # 锚点版本在混沌序列中的索引（以混沌版本序列为统一标尺）
        if anchor_ver not in chaos_versions:
            continue
        cidx = chaos_versions.index(anchor_ver)

        for i, p in enumerate(items):
            v_idx = cidx + (i - aidx)
            # 超出混沌已知范围时，向前延拓（1.x 早期版本按步长递减）
            if 0 <= v_idx < len(chaos_versions):
                p["version"] = chaos_versions[v_idx]
            else:
                p["version"] = extend_version(chaos_versions, v_idx)
            p["seasonId"] = p.get("seasonId")

        # 用版本号排序（升序），保证与混沌同一标尺
        items.sort(key=lambda x: version_sort_key(x["version"]))

    return result


def extend_version(chaos_versions, idx):
    """索引超出混沌序列时，按相邻步长向前/向后延拓版本号"""
    if idx < 0:
        first = chaos_versions[0]
        major, minor = (int(x) for x in first.split("."))
        # 向前按 0.1 步长递减（1.0 之前）
        minor = minor + idx  # idx 为负
        while minor < 0:
            major -= 1
            minor += 10
        return f"{major}.{minor}"
    last = chaos_versions[-1]
    major, minor = (int(x) for x in last.split("."))
    overflow = idx - (len(chaos_versions) - 1)
    minor += overflow
    while minor >= 10:
        major += 1
        minor -= 10
    return f"{major}.{minor}"

def fetch_apocalyptic_shadow(ver, tables, details=None):
    """末日幻影：每赛季最高难度(难度04)，星启三路(或两路)血量总和"""
    boss_map = load_reference(ver, "maze_boss.json")
    data = []
    for season_id, info in boss_map.items():
        if not info.get("zh"):
            continue
        # live 判断：live_begin 字段存在 = 正式服
        if "live_begin" not in info:
            continue
        detail = fetch_json(f"{STATIC}/{ver}/zh/boss/{season_id}.json")
        if not isinstance(detail, dict):
            continue
        levels = detail.get("level") or []
        if not levels:
            continue

        # 难度01-04（有 name），星启（pre_id）
        normal = [e for e in levels if e.get("name")]
        star = [e for e in levels if "pre_id" in e]
        if not normal:
            continue
        top = normal[-1]  # 难度04

        lanes_hp = []
        lanes_detail = [] if details is not None else None
        for key in ("event_id_list1", "event_id_list2"):
            for event in top.get(key, []):
                wd = [] if details is not None else None
                hp = event_total_hp(event, tables, is_infinite=False, detail=wd)
                lanes_hp.append(hp)
                if lanes_detail is not None:
                    lanes_detail.append({"label": f"第 {len(lanes_hp)} 路", "hp": round(hp), "waves": wd})

        star_elem = star[0] if star else None
        if star_elem:
            for event in star_elem.get("event_id_list", []):
                wd = [] if details is not None else None
                hp = event_total_hp(event, tables, is_infinite=False, detail=wd)
                lanes_hp.append(hp)
                if lanes_detail is not None:
                    lanes_detail.append({"label": f"第 {len(lanes_hp)} 路（星启）", "hp": round(hp), "waves": wd})

        total_hp = sum(lanes_hp)
        data.append({
            "version": "",  # 由 align_versions() 统一回填游戏版本号
            "seasonId": season_id,
            "name": clean_text(info.get("zh") or ""),
            "hp": round(total_hp),
            "lanes": len(lanes_hp),
            "lanesHp": [round(x) for x in lanes_hp],
        })
        if details is not None:
            details[season_id] = {
                "name": clean_text(info.get("zh") or ""),
                "total": round(total_hp),
                "lanes": lanes_detail,
            }

    data.sort(key=lambda x: int(x["seasonId"]))
    return data


# ---------------------------------------------------------------------------
# 虚构叙事 (pf)
# ---------------------------------------------------------------------------
def fetch_pure_fiction(ver, tables, details=None):
    """
    虚构叙事：每赛季最高难度(其四)，星启三路(或两路)。
    每路 3 波：
      第一波 = Σ(非集合体小怪血量)   [虚构集合体血量不计入]
      第二/三波 = Σ(小怪血量) + BOSS血量 × max(0, 1 - 3% × 小怪数)
    """
    story_map = load_reference(ver, "maze_extra.json")
    data = []
    for season_id, info in story_map.items():
        if not info.get("zh"):
            continue
        if "live_begin" not in info:
            continue
        detail = fetch_json(f"{STATIC}/{ver}/zh/story/{season_id}.json")
        if not isinstance(detail, dict):
            continue
        levels = detail.get("level") or []
        if not levels:
            continue

        normal = [e for e in levels if e.get("name")]
        star = [e for e in levels if "pre_id" in e]
        if not normal:
            continue
        top = normal[-1]  # 其四

        # 两路：event_id_list1/2 + infinite_list1/2
        lane_waves = []
        for key in ("1", "2"):
            ev_list = top.get(f"event_id_list{key}", [])
            inf_list = top.get(f"infinite_list{key}", {})
            lane_waves.append((ev_list, inf_list, top))

        # 星启第三路
        star_elem = star[0] if star else None
        if star_elem:
            lane_waves.append((star_elem.get("event_id_list", []), star_elem.get("infinite_list", {}), star_elem))

        lanes_hp = []
        lanes_detail = [] if details is not None else None
        for li, (ev_list, inf_list, source) in enumerate(lane_waves, 1):
            wd = [] if details is not None else None
            lane_hp = calc_pf_lane_hp(ev_list, inf_list, source, tables, detail=wd)
            lanes_hp.append(lane_hp)
            if lanes_detail is not None:
                label = f"第 {li} 路（星启）" if li == 3 else f"第 {li} 路"
                lanes_detail.append({"label": label, "hp": round(lane_hp), "waves": wd})

        total_hp = sum(lanes_hp)
        data.append({
            "version": "",  # 由 align_versions() 统一回填游戏版本号
            "seasonId": season_id,
            "name": clean_text(info.get("zh") or ""),
            "hp": round(total_hp),
            "lanes": len(lanes_hp),
            "lanesHp": [round(x) for x in lanes_hp],
        })
        if details is not None:
            details[season_id] = {
                "name": clean_text(info.get("zh") or ""),
                "total": round(total_hp),
                "lanes": lanes_detail,
            }

    data.sort(key=lambda x: int(x["seasonId"]))
    return data


def wave_hp_bonus(wave):
    """无限刷怪波次的血量加成系数 param_list[1]"""
    pl = wave.get("param_list") or []
    return pl[1] if len(pl) > 1 and pl[1] is not None else 0.0


def parse_invasion(event):
    """解析贪饕侵蚀：返回 (被侵蚀怪物ID集合, 侵蚀等级 1~4)。

    等级直接取 invasion.level。上游每个 event 的 invasion 对象自成一体
    （自带 maze_buff_param 与 monster_list），因此这里天然是「该 event 的等级」。
    注意：旧版曾把 level 忽略、改用 maze_buff_param[1] 当加成系数，那是错的——
    maze_buff_param[1] 只是旧口径的百分比，与贪饕血条机制无关。
    """
    inv = event.get("invasion")
    if not inv:
        return set(), 0

    lv = inv.get("level")
    if isinstance(lv, (list, tuple)):
        raise RuntimeError(f"invasion.level 变成多值 {lv}，贪饕分级逻辑需重新实现")
    try:
        level = int(lv) if lv is not None else 0
    except (TypeError, ValueError):
        level = 0

    polluted = {m.get("monster_id") for m in inv.get("monster_list", []) if m.get("monster_id")}

    # 等级缺失但确实配了侵蚀：不能静默按 0 处理（会悄悄少算一大截血量），
    # 宁可让抓取失败、保留旧数据。
    if level <= 0 and inv.get("monster_list"):
        raise RuntimeError(
            f"event(stage_id={event.get('stage_id')}) 有 invasion.monster_list 但 level={lv!r}，"
            f"无法确定贪饕等级"
        )
    if level > 4:
        raise RuntimeError(f"贪饕等级 {level} 超出已知范围(1~4)，系数表需补充")

    return polluted, level


def event_total_hp(event, tables, is_infinite=False, detail=None):
    """计算一个 event 的怪物总血量（含贪饕侵蚀）

    detail 传入列表时，按波次收集每只怪物明细（同一波内同 ID 合并成一行）。
    明细里的 contrib 之和恒等于返回值，供写盘前做一致性断言。
    """
    polluted, inv_level = parse_invasion(event)
    eg = event.get("elite_group", 0)
    hg = event.get("hard_level_group", 0)
    lv = event.get("level", 0)
    total = 0.0

    waves = event.get("monster_list") or []
    for wi, wave in enumerate(waves, 1):
        wave_total = 0.0
        agg = {}  # mid -> {"hp": 单只血量, "count": 只数, "inv": 该怪的侵蚀等级}
        for k, mid in wave.items():
            if not k.startswith("monster"):
                continue
            lvl = inv_level if mid in polluted else 0
            mhp = calc_monster_hp(mid, eg, hg, lv, tables, is_infinite=is_infinite, invasion_level=lvl)
            wave_total += mhp
            a = agg.setdefault(mid, {"hp": mhp, "count": 0, "inv": lvl})
            a["count"] += 1
        total += wave_total
        if detail is not None and agg:
            detail.append({
                "label": f"波次 {wi}",
                "hp": round(wave_total),
                "monsters": [
                    monster_entry(mid, a["hp"], a["inv"], tables, count=a["count"])
                    for mid, a in agg.items()
                ],
            })
    return total


def calc_pf_lane_hp(ev_list, inf_list, source, tables, detail=None):
    """计算虚构叙事单路血量（3波，含集合体剔除与BOSS削减）

    detail 传入列表时按波次收集明细；contrib 已折算实际计入口径：
    第一波按只数直加，第二/三波 BOSS 只计 40%、小怪每种按 10 只计。
    """
    # 从 infinite_list 取所有 wave（按 infinite_wave_id 排序）
    waves = []
    if isinstance(inf_list, dict):
        for wid, wave in inf_list.items():
            if isinstance(wave, dict) and wave.get("monster_group_id_list"):
                waves.append(wave)
    waves.sort(key=lambda w: w.get("infinite_wave_id", 0))

    if not waves:
        # 无 infinite 数据时回退到 event 的 monster_list
        return calc_pf_lane_hp_fallback(ev_list, source, tables, detail=detail)

    # 等级信息整路统一，从 event 取（event_id_list1/2 的 event 含 hard_level_group + level）
    ev = (ev_list or [{}])[0] if ev_list else {}
    hg = ev.get("hard_level_group", 0) or 0
    lv = ev.get("level", 0) or 0

    # 贪饕侵蚀（invasion 在 event 层级，作用于整路所有波次）
    polluted, inv_level = parse_invasion(ev)

    total = 0.0
    for i, wave in enumerate(waves):
        eg = wave.get("elite_group", 0)
        ids = wave["monster_group_id_list"]
        bonus = wave_hp_bonus(wave)

        # 计算每个怪物的血量，剔除虚构集合体
        entries = []  # (mid, hp)
        for mid in ids:
            if monster_name(mid, tables) == "虚构集合体":
                continue
            r = inv_level if mid in polluted else 0
            hp = calc_monster_hp(mid, eg, hg, lv, tables, is_infinite=True, infinite_bonus=bonus, invasion_level=r)
            entries.append((mid, hp))

        if not entries:
            continue

        if i == 0:
            # 第一波：小怪均匀刷新，直接求和（集合体已剔除）
            wave_total = sum(hp for _, hp in entries)
            total += wave_total
            agg = {}
            for mid, hp in entries:
                a = agg.setdefault(mid, {"hp": hp, "count": 0})
                a["count"] += 1
            rows = [
                monster_entry(mid, a["hp"], inv_level if mid in polluted else 0, tables, count=a["count"])
                for mid, a in agg.items()
            ]
        else:
            # 第二/三波：先判断该波次是否存在「真 BOSS」（血量明显高于小怪）
            # 统计每种怪物的单只血量与数量
            mid_hp = {}
            mid_count = {}
            for mid, hp in entries:
                mid_hp[mid] = hp
                mid_count[mid] = mid_count.get(mid, 0) + 1

            boss_mid = max(mid_hp, key=lambda m: mid_hp[m])
            boss_hp = mid_hp[boss_mid]
            others = [hp for m, hp in mid_hp.items() if m != boss_mid]
            second_max = max(others) if others else 0

            if boss_hp > second_max * 5:
                # 有真 BOSS（2011 赛季起）：40%×BOSS + 每种小怪×10
                #   —— 每种小怪击杀 10 只（共 20 只），各 -3% 共削减 60% BOSS，剩 40% 补刀
                minion_total = 10.0 * sum(hp for m, hp in mid_hp.items() if m != boss_mid)
                wave_total = 0.4 * boss_hp + minion_total
                total += wave_total
                rows = []
                for mid, hp in mid_hp.items():
                    inv = inv_level if mid in polluted else 0
                    if mid == boss_mid:
                        rows.append(monster_entry(mid, hp, inv, tables, contribution=0.4 * hp, count=1))
                    else:
                        rows.append(monster_entry(mid, hp, inv, tables, contribution=10.0 * hp, count=10))
            else:
                # 无真 BOSS（早期赛季）：直接 Σ(单只血量 × 数量)
                wave_total = sum(hp * mid_count[m] for m, hp in mid_hp.items())
                total += wave_total
                rows = [
                    monster_entry(mid, hp, inv_level if mid in polluted else 0, tables, count=mid_count[mid])
                    for mid, hp in mid_hp.items()
                ]

        if detail is not None:
            detail.append({"label": f"波次 {i + 1}", "hp": round(wave_total), "monsters": rows})

    return total


def calc_pf_lane_hp_fallback(ev_list, source, tables, detail=None):
    """无 infinite 数据时的回退：直接对 event monster_list 求和"""
    total = 0.0
    for li, event in enumerate(ev_list, 1):
        wd = [] if detail is not None else None
        total += event_total_hp(event, tables, is_infinite=False, detail=wd)
        if detail is not None:
            detail.extend(wd)
    return total


# ---------------------------------------------------------------------------
# 异相仲裁 (peak)
# ---------------------------------------------------------------------------
def fetch_divergent_arbitration(ver, tables, live_versions, details=None):
    """异相仲裁：每版本 3骑士总和 + 王棋 + 绝境王棋"""
    peak_map = load_reference(ver, "maze_peak.json")
    version_map = fetch_json(f"{STATIC}/{ver}/zh/peak/version.json")

    data = []
    for version, ids in version_map.items():
        if version == "unknown":
            continue
        if version not in live_versions:
            continue
        season_id = str(ids[0])
        detail = fetch_json(f"{STATIC}/{ver}/zh/peak/{season_id}.json")
        if not isinstance(detail, dict):
            continue

        # 3 骑士
        knights = 0.0
        knight_details = []
        knight_units = [] if details is not None else None
        for ki, knight in enumerate(detail.get("pre_level") or [], 1):
            khp = 0.0
            kw = [] if details is not None else None
            for event in knight.get("event_id_list", []):
                khp += peak_event_hp(event, knight.get("infinite_list", {}), tables, detail=kw)
            knights += khp
            knight_details.append(round(khp))
            if knight_units is not None:
                knight_units.append({"label": f"骑士 {ki}", "hp": round(khp), "waves": kw})

        # 王棋
        king = 0.0
        king_waves = [] if details is not None else None
        for event in detail.get("boss_level", {}).get("event_id_list", []):
            king += peak_event_hp(event, detail["boss_level"].get("infinite_list", {}), tables, detail=king_waves)

        # 绝境王棋
        king_abyss = 0.0
        abyss_waves = [] if details is not None else None
        for event in detail.get("boss_config", {}).get("event_id_list", []):
            king_abyss += peak_event_hp(event, detail["boss_config"].get("infinite_list", {}), tables, detail=abyss_waves)

        data.append({
            "version": version,
            "seasonId": season_id,
            "name": clean_text(peak_map.get(season_id, {}).get("zh") or detail.get("name") or ""),
            "knights": round(knights),
            "knightDetails": knight_details,
            "king": round(king),
            "kingAbyss": round(king_abyss),
        })
        if details is not None:
            details[season_id] = {
                "name": clean_text(peak_map.get(season_id, {}).get("zh") or detail.get("name") or ""),
                "total": round(knights + king + king_abyss),
                "groups": [
                    {"key": "knights", "label": "三骑士", "hp": round(knights), "units": knight_units},
                    {"key": "king", "label": "王棋", "hp": round(king),
                     "units": [{"label": "王棋", "hp": round(king), "waves": king_waves}]},
                    {"key": "kingAbyss", "label": "绝境王棋", "hp": round(king_abyss),
                     "units": [{"label": "绝境王棋", "hp": round(king_abyss), "waves": abyss_waves}]},
                ],
            }

    data.sort(key=lambda x: version_sort_key(x["version"]))
    return data


def peak_event_hp(event, inf_list, tables, detail=None):
    """异相仲裁单个 event 的血量（无限刷怪用 infinite_list，含贪饕侵蚀）

    detail 传入列表时按波次收集明细（保持原有遍历顺序，不改变数值）。
    """
    polluted, inv_level = parse_invasion(event)
    if isinstance(inf_list, dict) and inf_list:
        # 用 infinite_list 的 monster_group_id_list（完整刷怪序列）
        total = 0.0
        hg = event.get("hard_level_group", 0)
        lv = event.get("level", 0)
        for wi, (wid, wave) in enumerate(inf_list.items(), 1):
            if not isinstance(wave, dict):
                continue
            eg = wave.get("elite_group", 0)
            bonus = wave_hp_bonus(wave)
            wave_total = 0.0
            agg = {}
            for mid in wave.get("monster_group_id_list", []):
                r = inv_level if mid in polluted else 0
                mhp = calc_monster_hp(mid, eg, hg, lv, tables, is_infinite=True, infinite_bonus=bonus, invasion_level=r)
                wave_total += mhp
                a = agg.setdefault(mid, {"hp": mhp, "count": 0, "inv": r})
                a["count"] += 1
            total += wave_total
            if detail is not None and agg:
                label = wave.get("infinite_wave_id", wi)
                detail.append({
                    "label": f"波次 {label}",
                    "hp": round(wave_total),
                    "monsters": [
                        monster_entry(mid, a["hp"], a["inv"], tables, count=a["count"])
                        for mid, a in agg.items()
                    ],
                })
        return total
    else:
        # 普通 event
        return event_total_hp(event, tables, is_infinite=False, detail=detail)


# ---------------------------------------------------------------------------
# 版本过滤
# ---------------------------------------------------------------------------
def version_sort_key(v):
    """版本号排序键（如 4.5 > 3.8 > 1.0）"""
    parts = v.split(".")
    return [int(p) if p.isdigit() else 0 for p in parts]


def detect_live_versions(ver):
    """
    确定正式服（live）版本集合。
    规则：混沌回忆 version.json 中，对应关卡在 maze.json 里 live_begin 字段存在的版本 = live。
    """
    maze = load_reference(ver, "maze.json")
    version_map = fetch_json(f"{STATIC}/{ver}/zh/maze/version.json")
    live = set()
    for version, ids in version_map.items():
        if version == "static":
            continue
        sid = str(ids[0])
        info = maze.get(sid, {})
        # live_begin 字段存在（值可为空字符串）= 正式服
        if "live_begin" in info and info.get("live_begin") is not None:
            live.add(version)
    return live


def apply_overrides(result):
    """应用手动修正覆盖层 data/overrides.json（与自动抓取数据分离，互不污染）"""
    import os
    candidates = [
        "data/overrides.json",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "overrides.json"),
    ]
    ov_path = next((c for c in candidates if os.path.exists(c)), None)
    if not ov_path:
        return result
    try:
        with open(ov_path, encoding="utf-8") as f:
            ov = json.load(f)
    except Exception as e:
        print(f"警告: overrides.json 解析失败，忽略覆盖层: {e}")
        return result

    applied = 0
    for mode_key, entries in (ov or {}).items():
        if mode_key.startswith("_") or mode_key == "meta" or not isinstance(entries, dict):
            continue
        mode = result.get(mode_key)
        if not mode:
            continue
        for ver_id, patch in entries.items():
            if not isinstance(patch, dict):
                continue
            note = patch.pop("_note", None) or patch.pop("note", None)
            for item in mode.get("data", []):
                if str(item.get("version")) == str(ver_id) or str(item.get("seasonId")) == str(ver_id):
                    item.update(patch)
                    if note:
                        item["overrideNote"] = note
                    applied += 1
    if applied:
        print(f"已应用 {applied} 条手动修正（overrides.json）")
    return result


# ---------------------------------------------------------------------------
# 怪物明细：一致性断言 + 写盘
# ---------------------------------------------------------------------------
def verify_details(result, details_by_mode):
    """明细与主数据的一致性断言。

    这是「核对总血量组成」的技术保证，也是算法偏颇的自动报警器：
    任何一层对不上就直接抛错，宁可这次不更新，也不写一份自相矛盾的数据出去。

    比对层级：怪物贡献 → 波次 → 路/组 → 主数据总量。
    带 overrides 修正的条目只做内部自洽检查（因为总量已被人工改写）。
    """
    def close(a, b, tol=2.0):
        return abs(float(a) - float(b)) <= tol

    for mode_key, det in details_by_mode.items():
        mode = result.get(mode_key) or {}
        items = {str(x.get("seasonId")): x for x in (mode.get("data") or [])}
        for sid, d in det.items():
            item = items.get(sid)
            if item is None:
                continue
            units = d.get("lanes") or d.get("groups") or []

            # 1) 波次内部：怪物贡献之和 == 波次血量
            for unit in units:
                for w in unit.get("waves") or []:
                    ms = w.get("monsters") or []
                    s = sum(x.get("contrib", 0) for x in ms)
                    if not close(s, w.get("hp", 0), max(2.0, len(ms) * 1.0)):
                        raise RuntimeError(
                            f"[明细不一致] {mode_key} 赛季 {sid} · {unit.get('label')} · {w.get('label')}："
                            f"怪物贡献合计 {s:,} ≠ 波次血量 {w.get('hp', 0):,}"
                        )

            # 2) 组/路 → 主数据
            patched = bool(item.get("overrideNote"))
            if mode_key == "divergentArbitration":
                for g in units:
                    us = sum(u.get("hp", 0) for u in (g.get("units") or []))
                    if not close(us, g.get("hp", 0), max(2.0, len(g.get("units") or []))):
                        raise RuntimeError(
                            f"[明细不一致] {mode_key} 赛季 {sid} · {g.get('label')}："
                            f"子项合计 {us:,} ≠ 组血量 {g.get('hp', 0):,}"
                        )
                    if not patched and not close(g.get("hp", 0), item.get(g.get("key"), 0)):
                        raise RuntimeError(
                            f"[明细不一致] {mode_key} 赛季 {sid} · {g.get('label')}："
                            f"明细 {g.get('hp', 0):,} ≠ 主数据 {item.get(g.get('key'), 0):,}"
                        )
            else:
                lhp = item.get("lanesHp") or []
                if len(units) != len(lhp):
                    raise RuntimeError(
                        f"[明细不一致] {mode_key} 赛季 {sid}：明细 {len(units)} 路 ≠ 主数据 {len(lhp)} 路"
                    )
                if not patched:
                    for lane, v in zip(units, lhp):
                        if not close(lane.get("hp", 0), v):
                            raise RuntimeError(
                                f"[明细不一致] {mode_key} 赛季 {sid} · {lane.get('label')}："
                                f"明细 {lane.get('hp', 0):,} ≠ 主数据 {v:,}"
                            )
    return True


def write_details(details_by_mode, out_path):
    """把 4 个玩法的明细各写一份 JSON（紧凑格式，减小体积）"""
    base = os.path.dirname(os.path.abspath(out_path))
    ddir = os.path.join(base, "details")
    os.makedirs(ddir, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    out = []
    for mode_key, det in details_by_mode.items():
        payload = {"mode": mode_key, "generatedAt": stamp, "versions": det}
        path = os.path.join(ddir, f"{mode_key}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
        out.append((mode_key, len(det), os.path.getsize(path)))
    for mode_key, n, size in out:
        print(f"  明细 {mode_key:<22} {n:>3} 期  {size / 1024:>7.1f} KB")
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    global out_path_arg
    out_path_arg = sys.argv[1] if len(sys.argv) > 1 else "data/health_data.json"
    print("=" * 60)
    print("崩坏星穹铁道 · 终局玩法血量膨胀数据抓取")
    print("=" * 60)

    ver = detect_data_version()
    print(f"数据包版本: {ver}")

    tables = build_lookup_tables(ver)
    print("参考数据已加载: monstervalue / HardLevelGroup / EliteGroup / InfiniteEliteGroup")

    live_versions = detect_live_versions(ver)
    print(f"正式服版本(live): {sorted(live_versions, key=version_sort_key)}")

    details_by_mode = {
        "chaosMemory": {},
        "apocalypticShadow": {},
        "pureFiction": {},
        "divergentArbitration": {},
    }

    print("\n[1/4] 抓取混沌回忆...")
    chaos = fetch_chaos_memory(ver, tables, live_versions, details=details_by_mode["chaosMemory"])
    print(f"  -> {len(chaos)} 个版本")

    print("[2/4] 抓取末日幻影...")
    shadow = fetch_apocalyptic_shadow(ver, tables, details=details_by_mode["apocalypticShadow"])
    print(f"  -> {len(shadow)} 个赛季")

    print("[3/4] 抓取虚构叙事...")
    fiction = fetch_pure_fiction(ver, tables, details=details_by_mode["pureFiction"])
    print(f"  -> {len(fiction)} 个赛季")

    print("[4/4] 抓取异相仲裁...")
    peak = fetch_divergent_arbitration(ver, tables, live_versions, details=details_by_mode["divergentArbitration"])
    print(f"  -> {len(peak)} 个版本")

    result = {
        "meta": {
            "dataVersion": ver,
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": SITE,
            "liveVersion": max(live_versions, key=version_sort_key) if live_versions else None,
        },
        "chaosMemory": {"name": "混沌回忆", "data": chaos},
        "apocalypticShadow": {"name": "末日幻影", "data": shadow},
        "pureFiction": {"name": "虚构叙事", "data": fiction},
        "divergentArbitration": {"name": "异相仲裁", "data": peak},
    }

    result = align_versions(result)
    result = apply_overrides(result)

    # ---- 明细一致性断言：任何一层对不上就一个文件都不写 ----
    # 放在写主数据之前，保证「有问题的数据不落地」。
    try:
        verify_details(result, details_by_mode)
        print("\n✓ 明细与主数据一致（怪物 → 波次 → 路/组 → 总量）")
    except RuntimeError as e:
        print(f"\n✗ {e}", file=sys.stderr)
        print("✗ 已中止：明细与主数据对不上，现有文件未被修改。", file=sys.stderr)
        sys.exit(1)

    out_path = out_path_arg

    # ---- 安全写入：先落临时文件 → 校验 → 通过后才替换正式文件 ----
    # 直接覆盖正式文件是有风险的：一旦上游改版导致某个玩法抓成空列表，
    # 写出去的就是一份「全空」的数据，网站曲线会静默消失。所以这里拆成两步。
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 60)
    print("校验抓取结果")
    print("=" * 60)
    if not run_validation(tmp_path, out_path):
        # 校验失败：删掉临时文件，正式数据分毫未动，以非 0 退出码让 CI 标红
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        print("\n✗ 已中止：现有数据文件未被修改。", file=sys.stderr)
        sys.exit(1)

    # 校验通过：原子替换（同目录内 rename 是原子操作，不会出现半个文件）
    os.replace(tmp_path, out_path)

    # ---- 怪物明细：按玩法拆 4 个文件，前端点开某一期时才加载 ----
    print("\n明细文件:")
    write_details(details_by_mode, out_path)

    # 同步输出 JS 版本（供页面 <script> 直接加载，规避 file:// CORS）
    # 用紧凑格式 + 末尾换行，避免把整个文件挤成一行导致 diff 无意义
    js_path = out_path.rsplit(".", 1)[0] + ".js"
    js_tmp = js_path + ".tmp"
    with open(js_tmp, "w", encoding="utf-8") as f:
        f.write("window.HP_DATA = " + json.dumps(result, ensure_ascii=False, separators=(",", ":")) + ";\n")
    os.replace(js_tmp, js_path)
    print(f"\n完成！输出: {out_path} + {js_path}")

    # ---- 自动更新 index.html 的缓存破除串 ----
    # 数据文件通过 <script src="data/health_data.js?v=YYYYMMDD"> 加载，
    # 查询串不变的话，浏览器与 CDN 会一直命中旧缓存，访问者看不到新数据。
    # 以前这一步需要人工记得改，实际上总会忘，所以交给脚本自动完成。
    stamp = time.strftime("%Y%m%d")
    if update_cache_buster(stamp):
        print(f"已同步 index.html 缓存串为 v={stamp}")
    else:
        print("提示: index.html 中未找到缓存串，或已是当天版本，无需更新")

    print_summary(result)


def run_validation(new_path, prev_path):
    """调用 validate.py 校验抓取结果。返回 True 表示通过。

    校验脚本与本文件同目录，用 sys.executable 调用以复用同一个解释器。
    """
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "validate.py")
    cmd = [sys.executable, script, new_path]
    if os.path.exists(prev_path):
        cmd.append(prev_path)
    try:
        p = subprocess.run(cmd, text=True)
        return p.returncode == 0
    except FileNotFoundError:
        # 校验脚本缺失时选择「继续」还是「中止」？
        # 这里选中止 —— 校验是防止坏数据上线的唯一闸门，不能因为文件丢了就放行。
        print("✗ 找不到 scripts/validate.py，为保证数据安全已中止写入", file=sys.stderr)
        return False


def update_cache_buster(stamp):
    """把 index.html 里 health_data.js?v=... 的查询串更新为 stamp。"""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "index.html")
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        html = f.read()
    new_html, n = re.subn(
        r'(src="data/health_data\.js\?v=)(\d+)(")',
        lambda m: m.group(1) + stamp + m.group(3),
        html,
    )
    if n == 0 or new_html == html:
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_html)
    return True


def print_summary(result):
    print("\n" + "=" * 60)
    print("数据摘要")
    print("=" * 60)
    for key in ("chaosMemory", "apocalypticShadow", "pureFiction", "divergentArbitration"):
        mode = result[key]
        d = mode["data"]
        if not d:
            print(f"{mode['name']}: 无数据")
            continue
        if key == "divergentArbitration":
            first = d[0]
            last = d[-1]
            print(f"{mode['name']}: {len(d)}版本  首[{first['version']}] 骑士={first['knights']:,} 王棋={first['king']:,} 绝境={first['kingAbyss']:,}")
            print(f"            末[{last['version']}] 骑士={last['knights']:,} 王棋={last['king']:,} 绝境={last['kingAbyss']:,}")
        else:
            first = d[0]
            last = d[-1]
            print(f"{mode['name']}: {len(d)}版本  首[{first['version']}] HP={first['hp']:,}")
            print(f"            末[{last['version']}] HP={last['hp']:,}  (膨胀 {last['hp']/first['hp']:.2f}x)")


if __name__ == "__main__":
    main()
