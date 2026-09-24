#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
filter_sales_ljc.py —— 经理号「全公司」销售分析 xlsx → 提取李家村渠道数据 → 直接写 sa_aug_cache.json。
（storyos B 方案组件，2026-09-24 建）

为什么不过渡成 xlsx 再喂 update_sa_cache.py：
  openpyxl 写出的单元格是 inlineStr，而 update_sa_cache.py 的 XML 解析只认
  sharedStrings（t="s"）→ 实测读出 0 条（2026-09-24 踩坑）。
  故直接按 update_sa_cache 的 EXTRACT_MAP 口径提取 records、过滤门店、写同格式 JSON，
  管线侧 run_pipeline.py 用 --no-sa 跳过 Step1，直接消费本脚本产出的缓存。

源结构（店长号版/经理号版一致，实测 2026-09-24）：
    row1 = 标题「销售分析」；row2 = 字段名；row3 起 = 数据；E 列 = 门店名称。
读法：zipfile+XML（openpyxl 读不了用友导出的坏样式，同 update_sa_cache）。

用法：
    python filter_sales_ljc.py <源销售分析.xlsx> [-o sa_aug_cache.json 输出路径]
"""
import sys
import os
import zipfile
import xml.etree.ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
STORE_KEY = "李家村"
DEFAULT_OUT = "/Users/mac/.local/share/TeleAgent/TeleAgent的工作空间/shop/sa_aug_cache.json"

# 与 shop/update_sa_cache.py 完全一致的列字母 → 字段映射
EXTRACT_MAP = {
    "G":  "iEmployeeid_name",
    "H":  "iBusinesstypeid_name",
    "P":  "retailVouchHeaderDefineCharacter__HWHKQD_name",
    "AM": "fNetMoney",
    "AG": "fQuantity",
    "B":  "dDate",
    "C":  "code",
    "N":  "product_cName",
    "O":  "productsku_cCode",
    "I":  "iMemberid_name",
    "J":  "iMemberid_cphone",
}


def col_letter(ref):
    return "".join(ch for ch in ref if ch.isalpha())


def read_records(path):
    """zipfile+XML 全量解析 → [(门店名, record_dict)]，数字列转 float。"""
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.parse(z.open("xl/sharedStrings.xml")).getroot():
            shared.append("".join(n.text or "" for n in si.iter(f"{NS}t")))
    root = ET.parse(z.open("xl/worksheets/sheet1.xml")).getroot()
    rows = root.findall(f".//{NS}row")
    if len(rows) < 3:
        sys.exit(f"❌ 源行数过少: {len(rows)}")

    # 字段行 = row2（rows[1]），确认「门店名称」列 + 校验与 EXTRACT_MAP 的字段名对应
    hdr = {}
    for c in rows[1].findall(f"{NS}c"):
        col = col_letter(c.get("r"))
        t = c.get("t")
        v = c.find(f"{NS}v")
        val = v.text if v is not None else ""
        if t == "s" and val:
            val = shared[int(val)]
        hdr[col] = str(val)
    store_col = None
    for col, val in hdr.items():
        if "门店名称" in val:
            store_col = col
            break
    if not store_col:
        sys.exit(f"❌ 字段行未找到「门店名称」列: {dict(list(hdr.items())[:10])}")

    out = []
    for row in rows[2:]:
        store = ""
        rec = {}
        for c in row.findall(f"{NS}c"):
            col = col_letter(c.get("r"))
            t = c.get("t")
            v = c.find(f"{NS}v")
            val = v.text if v is not None else ""
            if t == "s" and val:
                val = shared[int(val)]
            if col == store_col:
                store = str(val)
            if col in EXTRACT_MAP:
                import datetime
                field = EXTRACT_MAP[col]
                if field in ("fNetMoney", "fQuantity"):
                    try:
                        val = float(val) if val else 0.0
                    except ValueError:
                        val = 0.0
                elif field == "dDate":
                    # Excel 日期序列号 → ISO 日期（与 update_sa_cache 同口径）
                    try:
                        serial = float(val)
                        val = (datetime.date(1900, 1, 1) + datetime.timedelta(days=serial - 2)).isoformat()
                    except (ValueError, OverflowError):
                        pass
                rec[field] = val
        if store:
            out.append((store, rec))
    return out


def main():
    if len(sys.argv) < 2:
        sys.exit("用法: filter_sales_ljc.py <源销售分析.xlsx> [-o 输出json]")
    src = sys.argv[1]
    out = sys.argv[sys.argv.index("-o") + 1] if "-o" in sys.argv else DEFAULT_OUT
    if not os.path.exists(src):
        sys.exit(f"❌ 找不到源文件: {src}")

    pairs = read_records(src)
    total = len(pairs)
    keep = [rec for store, rec in pairs if STORE_KEY in store]
    if not keep:
        sys.exit(f"❌ 过滤后 0 条（源 {total} 条中无「{STORE_KEY}」）—— 检查门店名")

    net = sum(r.get("fNetMoney", 0) for r in keep)
    qty = sum(r.get("fQuantity", 0) for r in keep)
    with open(out, "w", encoding="utf-8") as f:
        import json
        json.dump({"records": keep}, f, ensure_ascii=False, indent=1)

    print(f"→ 源 {total} 条 → 保留「{STORE_KEY}」{len(keep)} 条")
    print(f"  销售净额 ¥{net:,.2f} | 数量 {qty:.0f}")
    print(f"✅ sa_cache 已写 → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
