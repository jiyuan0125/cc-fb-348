#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recover_merge.py
================
把故障前设备读数备份 snapshot/ 与恢复窗口内在线侧读数 live/ 拼成统一时间轴
unified.csv（列: dev,ts,val,src），并把重叠段不一致点的处置写入 overlap_report.txt。

一、两段数据的形成过程与不一致点的取法
--------------------------------------
1. snapshot/ 是故障设备本地【旧采集固件】持续写入的读数缓存，维修时才整体转储。
   故障窗口内旧固件/本地存储链路异常，会把单个采样点写坏，形态为孤立尖峰：
   仅该点突变，前后相邻采样立即回到原趋势（正向、负向尖峰都有）。
2. live/ 是维修【更换采集固件】后在线侧继续采到的读数，走新固件采集链路，
   时间戳取自在线侧设备钟（与 snapshot 旧钟的偏差不同且每段各自恒定）。
3. 重叠段内同一次物理采样在两边各有一行。对本批 8 台设备的核验结论：
   - 等值行占绝大多数（每台 360 个重叠点中 356~357 个相等）；
   - 全部 29 个不一致点中，snapshot 值均为偏离前后邻居的孤立尖峰，live 值均
     平滑落在两侧相邻采样的值域内；
   - live 全段（含 live 独有后段）不存在任何此类跳变；部分 snapshot 尖峰的
     异常值还与别的时刻的 live 正常值偶然相等，反证尖峰是 snapshot 侧写坏。
   => 不一致点一律取 live（新固件在线链路的可信记录），丢弃 snapshot 坏点，
      该点采用来源记为 live。

二、钟偏衔接
------------
统一时间线沿用 snapshot 段的时钟读数（快照钟）。
由重叠段两边【值相等】的行配对，统计差值 off = live_ts - snap_ts 的众数，
得到每台设备的恒定钟偏；在线行对齐到快照钟的时间戳为 live_ts - off，
snapshot 行时间戳保持原值。本批数据 8 台设备均为 off = -5s（在线钟慢 5s）。

三、src 取值
------------
- 仅 snapshot 有的行（重叠区之前）        -> src=snapshot
- 仅 live 有的行（重叠区之后，已对齐钟偏） -> src=live
- 两边都有的行：值相等 -> src=snapshot+live；值不等 -> 取 live，src=live
"""

import argparse
import csv
import os
from collections import Counter, defaultdict

HEADER = ("dev", "ts", "val", "src")
SNAP = "snapshot"
LIVE = "live"
BOTH = "snapshot+live"
# 钟偏候选窗口：设备钟偏差为秒级小量；窗口也排除周期数据跨周期的同值远距配对
OFF_WINDOW = 300
CADENCE = 60


def load(path):
    """读取单台设备 csv -> [(ts:int, val:int), ...]，按 ts 排序并查重。"""
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append((int(r["ts"]), int(r["val"])))
    rows.sort(key=lambda x: x[0])
    if len({t for t, _ in rows}) != len(rows):
        raise ValueError(f"{path}: 存在重复时间戳")
    return rows


def estimate_offset(snap, live):
    """
    用重叠段两边值相等的行推断恒定钟偏 off = live_ts - snap_ts。
    仅在秒级窗口内配对，取众数。返回 (off, 配对票数, 次高候选)。
    """
    live_by_val = defaultdict(list)
    for t, v in live:
        live_by_val[v].append(t)
    votes = Counter()
    for ts, v in snap:
        for tl in live_by_val.get(v, ()):
            d = tl - ts
            if abs(d) <= OFF_WINDOW:
                votes[d] += 1
    if not votes:
        raise RuntimeError("重叠段没有找到任何等值配对，无法推断钟偏")
    (off, n), runner = votes.most_common(2)[0], (
        votes.most_common(2)[1] if len(votes) > 1 else None)
    if runner is not None and n < 10 * runner[1]:
        raise RuntimeError(f"钟偏众数不突出: {votes.most_common(5)}")
    return off, n, runner


def merge_device(dev, snap_rows, live_rows, off):
    """
    统一到快照钟合并一台设备。snapshot 时间戳不变；live 时间戳对齐为 live_ts-off。
    返回 (merged_rows, conflicts, n_overlap)。
    conflicts 元素: dict(dev, ts, snap_ts, live_ts, snap_val, live_val)
    """
    snap_m = dict(snap_rows)                       # 快照钟原生
    live_m = {t - off: v for t, v in live_rows}   # 在线行对齐到快照钟
    merged, conflicts = [], []
    for ts in sorted(set(snap_m) | set(live_m)):
        in_s, in_l = ts in snap_m, ts in live_m
        if in_s and in_l:
            vs, vl = snap_m[ts], live_m[ts]
            if vs == vl:
                merged.append((dev, ts, vs, BOTH))
            else:
                # snapshot 为故障期旧固件写坏的孤立尖峰 -> 取 live
                conflicts.append({
                    "dev": dev, "ts": ts,
                    "snap_ts": ts, "live_ts": ts + off,
                    "snap_val": vs, "live_val": vl,
                    "adopt_val": vl, "adopt_src": LIVE,
                })
                merged.append((dev, ts, vl, LIVE))
        elif in_l:
            merged.append((dev, ts, live_m[ts], LIVE))
        else:
            merged.append((dev, ts, snap_m[ts], SNAP))
    return merged, conflicts, len(set(snap_m) & set(live_m))


def write_report(path, totals, conflicts):
    """
    报告前 3 行固定为计数（计数名=整数），随后每个不一致点一行：
    设备 时刻 两边取值 采用值 采用来源。
    """
    lines = [
        f"重叠区行数={totals['overlap']}",
        f"一致行数={totals['agree']}",
        f"不一致点数={totals['conflict']}",
    ]
    for c in conflicts:
        lines.append(
            f"设备={c['dev']} 时刻={c['ts']} "
            f"snapshot取值={c['snap_val']} live取值={c['live_val']} "
            f"采用值={c['adopt_val']} 采用来源={c['adopt_src']}"
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description="合并 snapshot/live 两段设备读数")
    ap.add_argument("--snapshot-dir", default="snapshot")
    ap.add_argument("--live-dir", default="live")
    ap.add_argument("--out", default="unified.csv")
    ap.add_argument("--report", default="overlap_report.txt")
    args = ap.parse_args()

    devs = sorted(fn[:-4] for fn in os.listdir(args.snapshot_dir)
                  if fn.endswith(".csv"))
    if not devs:
        raise SystemExit("snapshot 目录下没有 csv 文件")

    all_rows, all_conflicts = [], []
    total_overlap = total_agree = 0
    per_dev = {}

    for dev in devs:
        snap = load(os.path.join(args.snapshot_dir, dev + ".csv"))
        live = load(os.path.join(args.live_dir, dev + ".csv"))
        off, pairs, runner = estimate_offset(snap, live)
        merged, conflicts, n_overlap = merge_device(dev, snap, live, off)

        # 合并后时间轴连续性 / 节拍校验
        ts_seq = [t for _, t, _, _ in merged]
        if len(ts_seq) != len(set(ts_seq)):
            raise AssertionError(f"{dev}: 合并后存在重复时间戳")
        gaps = {b - a for a, b in zip(ts_seq, ts_seq[1:])}
        if gaps != {CADENCE}:
            raise AssertionError(f"{dev}: 合并后节拍异常 {gaps}")

        all_rows.extend(merged)
        all_conflicts.extend(conflicts)
        total_overlap += n_overlap
        total_agree += n_overlap - len(conflicts)
        per_dev[dev] = {"off": off, "pairs": pairs,
                        "n_overlap": n_overlap, "n_conflict": len(conflicts)}

    all_rows.sort(key=lambda r: (r[0], r[1]))
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(all_rows)

    totals = {"overlap": total_overlap, "agree": total_agree,
              "conflict": len(all_conflicts)}
    all_conflicts.sort(key=lambda c: (c["dev"], c["ts"]))
    write_report(args.report, totals, all_conflicts)

    src_count = Counter(r[3] for r in all_rows)
    print(f"设备 {len(devs)} 台，输出 {len(all_rows)} 行 -> {args.out}")
    print(f"重叠区行数={total_overlap} 一致行数={total_agree} "
          f"不一致点数={len(all_conflicts)}（均取 live）")
    print(f"src 计数: snapshot={src_count[SNAP]} "
          f"snapshot+live={src_count[BOTH]} live={src_count[LIVE]}")
    for dev, info in sorted(per_dev.items()):
        print(f"  {dev}: off(live-snap)={info['off']:+d}s "
              f"等值配对={info['pairs']} 重叠={info['n_overlap']} "
              f"冲突={info['n_conflict']}")
    print(f"报告 -> {args.report}")


if __name__ == "__main__":
    main()
