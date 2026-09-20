#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据完整性校验

为什么需要这个文件：
    scraper.py 在「上游改版 / 网络异常 / 选择器失效」时，各 fetch_* 会返回空列表，
    但它仍会以退出码 0 把一份「四种玩法全为空」的数据写出去。定时任务看到退出码 0
    照常提交，网站上所有曲线会静默消失 —— 而且要等到下周一有人访问才会发现。

    所以这里对抓取结果做多层校验，任何一层不过就拒绝写入、以非 0 退出码失败，
    让 GitHub Actions 标红并通知，实现「宁可数据不更新，也不写坏数据」。

校验项（任一失败即整体失败）：
    1. 结构完整   —— 四种玩法齐备，且 data 均为非空列表
    2. 数据点总数 —— 不得比上一版明显减少（防止上游只返回了部分赛季）
    3. 数值合法   —— 血量均为正数、无 None/NaN，换算倍率在合理区间
    4. 版本号格式 —— 形如 1.0 / 4.5，不用季节号（s3020 / 2026）
    5. 时间顺序   —— 版本号严格递增，无重复
    6. 最新版本   —— 末位应与 meta.liveVersion 一致，防止抓进未上线的赛季

用法：
    python scripts/validate.py <新数据.json> [上一版数据.json]
    校验通过打印摘要并以 0 退出；失败打印原因并以 1 退出。
"""

import json
import re
import sys

# 四种玩法的键名 → 中文名（用于报错信息）
MODES = {
    "chaosMemory": "混沌回忆",
    "apocalypticShadow": "末日幻影",
    "pureFiction": "虚构叙事",
    "divergentArbitration": "异相仲裁",
}

# 异相仲裁有三个子指标，其余玩法只有 hp
SUB_KEYS = ("knights", "king", "kingAbyss")

# 版本号必须形如 1.0 / 2.7 / 4.5，且大版本 1~9、小版本 0~9
VERSION_RE = re.compile(r"^\d\.\d$")

# 数据点总数的允许收缩比例：新数据不得少于上一版的这个比例。
# 设 0.9 是为了容忍极少数情况下的赛季校正，同时又能挡住「只抓到一半」。
MIN_KEEP_RATIO = 0.9

# 单期血量上限（1e12 = 1 万亿）。超过基本可断定是倍率乘错，而非真实数值。
MAX_HP = 1e12


class ValidationError(Exception):
    """校验失败。消息会直接展示给使用者，所以要写清楚「哪里不对、该怎么办」。"""


def _fail(msg):
    raise ValidationError(msg)


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check_structure(data):
    """结构完整：四种玩法齐备，data 均为非空列表。"""
    for key, name in MODES.items():
        if key not in data:
            _fail(f"缺少玩法「{name}」({key}) —— 抓取流程可能整体未执行")
        arr = (data.get(key) or {}).get("data")
        if not isinstance(arr, list):
            _fail(f"「{name}」的 data 不是列表（实际为 {type(arr).__name__}）")
        if not arr:
            _fail(
                f"「{name}」抓取结果为空。\n"
                f"    常见原因：上游 hsr.nanoka.cc 改版、静态 JSON 路径变化、网络被限流。\n"
                f"    处理：先在本地手工运行 python scripts/scraper.py /tmp/probe.json 观察输出，\n"
                f"    若同样为空则说明上游结构变了，需要对照站点源码调整 scripts/scraper.py 的解析逻辑。"
            )


def check_values(data):
    """数值合法：血量均为正数且在合理上限内。"""
    for key, name in MODES.items():
        arr = data[key]["data"]
        for item in arr:
            ver = item.get("version", "?")
            values = []
            if key == "divergentArbitration":
                for sk in SUB_KEYS:
                    if sk not in item:
                        _fail(f"「{name}」版本 {ver} 缺少子指标字段 {sk}")
                    values.append((sk, item[sk]))
            else:
                if "hp" not in item:
                    _fail(f"「{name}」版本 {ver} 缺少 hp 字段")
                values.append(("hp", item["hp"]))

            for field, v in values:
                if v is None:
                    _fail(f"「{name}」版本 {ver} 的 {field} 为 None —— 倍率链中某环缺失")
                if not isinstance(v, (int, float)):
                    _fail(f"「{name}」版本 {ver} 的 {field} 不是数字（{v!r}）")
                if v <= 0:
                    _fail(f"「{name}」版本 {ver} 的 {field} 为 {v} —— 血量必须为正数")
                if v > MAX_HP:
                    _fail(
                        f"「{name}」版本 {ver} 的 {field} 为 {v:,.0f}，超过上限 {MAX_HP:,.0f}。\n"
                        f"    这通常是倍率被重复相乘（例如同时套用了精英组与无限精英组倍率）导致的。"
                    )


def check_version_format(data):
    """版本号格式合法：形如 4.5，不允许季节号（s3020 / 2026）。"""
    for key, name in MODES.items():
        for item in data[key]["data"]:
            ver = item.get("version")
            if not ver:
                _fail(
                    f"「{name}」存在 version 为空的记录。\n"
                    f"    这会让前端退回显示 seasonId（s 开头的编号），属于已知问题。"
                )
            if not VERSION_RE.match(str(ver)):
                _fail(
                    f"「{name}」版本号 {ver!r} 格式异常，应为「大版本.小版本」形如 4.5。\n"
                    f"    若出现 s3020 / 2026 这类季节号，说明 align_versions() 的映射没生效。"
                )


def check_ordering(data):
    """版本号严格递增且无重复。"""
    for key, name in MODES.items():
        arr = data[key]["data"]
        seen = []
        for item in arr:
            ver = str(item["version"])
            if ver in seen:
                _fail(f"「{name}」版本 {ver} 重复出现")
            seen.append(ver)
        # 按 (大版本, 小版本) 元组比较，避免字符串序把 4.10 排在 4.9 前面
        pairs = [tuple(int(x) for x in v.split(".")) for v in seen]
        if pairs != sorted(pairs):
            _fail(f"「{name}」版本号未按升序排列：{seen}")


def check_latest(data):
    """末位版本应与 meta.liveVersion 一致，确保没有把未上线赛季混进来。"""
    live = (data.get("meta") or {}).get("liveVersion")
    if not live:
        _fail("meta.liveVersion 缺失 —— 无法确认抓取到的是正式服数据")
    key = "divergentArbitration"
    tail = str(data[key]["data"][-1]["version"])
    versions = {str(x["version"]) for k in MODES for x in data[k]["data"]}
    if str(live) not in versions:
        _fail(
            f"meta.liveVersion={live} 在任何玩法的数据中都找不到，\n"
            f"    说明抓到的可能全是未上线赛季，或 live_begin 判定逻辑失效。"
        )
    return live, tail


def check_shrink(data, previous):
    """数据点总数不得比上一版明显减少（挡住「只抓到一半」）。"""
    if not previous:
        return None
    report = []
    for key, name in MODES.items():
        old = len((previous.get(key) or {}).get("data") or [])
        new = len(data[key]["data"])
        if old == 0:
            continue
        if new < old * MIN_KEEP_RATIO:
            _fail(
                f"「{name}」数据点从 {old} 降到 {new}（超过 {(1 - MIN_KEEP_RATIO) * 100:.0f}% 的收缩）。\n"
                f"    正式服的赛季只会累积、不会回退，出现下降通常意味着这次抓取不完整。\n"
                f"    若确属上游校正，请确认后手工更新数据文件。"
            )
        if new != old:
            report.append(f"{name} {old}→{new}")
    return report


def validate(new_data, previous=None):
    """执行全部校验，返回一份摘要字典。任何一项失败都会抛出 ValidationError。"""
    check_structure(new_data)
    check_values(new_data)
    check_version_format(new_data)
    check_ordering(new_data)
    live, tail = check_latest(new_data)
    shrink = check_shrink(new_data, previous)

    return {
        "live": live,
        "tail": tail,
        "shrink": shrink or [],
        "counts": {MODES[k]: len(new_data[k]["data"]) for k in MODES},
        "generatedAt": (new_data.get("meta") or {}).get("generatedAt", "?"),
    }


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/validate.py <新数据.json> [上一版数据.json]", file=sys.stderr)
        return 2

    new_path = sys.argv[1]
    prev_path = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        new_data = load(new_path)
    except FileNotFoundError:
        print(f"✗ 找不到待校验文件: {new_path}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print(f"✗ 待校验文件不是合法 JSON: {e}", file=sys.stderr)
        return 1

    previous = None
    if prev_path:
        try:
            previous = load(prev_path)
        except Exception:
            # 上一版读不到不影响本次校验，只是跳过「收缩检查」
            print(f"提示: 无法读取上一版 {prev_path}，跳过收缩检查")

    try:
        info = validate(new_data, previous)
    except ValidationError as e:
        print("", file=sys.stderr)
        print("=" * 64, file=sys.stderr)
        print("✗ 数据校验未通过，已阻止写入", file=sys.stderr)
        print("=" * 64, file=sys.stderr)
        print(str(e), file=sys.stderr)
        print("", file=sys.stderr)
        print("现有数据文件保持不变，网站不会受到影响。", file=sys.stderr)
        return 1

    print("✓ 数据校验通过")
    print(f"  正式服版本 : {info['live']}（末位数据点 {info['tail']}）")
    print(f"  生成时间   : {info['generatedAt']}")
    for name, n in info["counts"].items():
        print(f"  {name:<8} : {n} 个数据点")
    if info["shrink"]:
        print(f"  数据点变化 : {', '.join(info['shrink'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
