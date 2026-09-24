#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_sales_analysis_hyc.py —— 拉「华为李家村万达授权体验店」的销售分析明细（经理账号）。

为什么单独写：
  1) 李家村用店长账号(18161914293)，返回天然只含李家村，无需门店过滤；
     李家村必须用**经理账号**(18591910491)，该账号返回**全公司全部门店**，
     必须在本地按 `store_name == "华为李家村万达授权体验店"` 过滤。
     ⚠️ 历史血泪：曾误判"经理号只看得到大唐不夜城"——真相是 report/list
     结果按门店排序，第 1 页 5000 行恰好全是第一家店。别只看第一页就下结论。
  2) rm_saleanalysis 的日期/门店/org 等 queryParams **服务端全部无效**，
     必须全量分页拉取（约 46 页 × 5000）后本地过滤。
  3) 全公司数据量大（22.8 万行），逐页过滤而非全量驻留内存，降低占用。

产出：
  sa_hyc_month.json   —— 本月（当月1日~今天）李家村记录（全 124 字段，已去重）
  sa_aug_cache.json   —— 同上的 merge_qudao.py 消费格式（{records:[...]}），
                         供渠道明细/员工×渠道/渠道达成使用
  sa_warehouse_hyc.json —— 李家村全历史（当月切片的上游，供复用/审计）
  sa_raw.tsv          —— 本月宽表（人工核对用）

用法：
  python fetch_sales_analysis_hyc.py [--use-cache] [--month 2026-09]
"""
import json, os, sys, ssl, time, datetime, urllib.request, urllib.error, re
import collections
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

YY_BASE = "https://c3.yonyoucloud.com"
SA_URL = YY_BASE + "/yonbip-mkt-retailweb/report/list"
BILLNUM = "rm_saleanalysis"

STORE_NAME = "华为李家村万达授权体验店"
STORE_KEY = "李家村"          # 宽松匹配键（防门店名写法微调）
SN_FIELD = "oid_userDefine_2419863036093267976"  # 序列号
# 【2026-09-20】门店 ID —— condition.commonVOs 精准锁单店用（实测 1 页 37s 拿本店全量）
#   取自接口返回行的 store 字段；换店改这里或设 SA_STORE_ID 环境变量
STORE_ID = os.environ.get("SA_STORE_ID", "2387138245738627078")

BASE = os.path.dirname(os.path.abspath(__file__))
# 经理账号登录态（店长账号看不到李家村）
DEFAULT_STATE = os.path.expanduser("~/.agent-browser/sessions/yonyou-mgr-default.json")
STATE = os.environ.get("YONYOU_STATE", DEFAULT_STATE)

OUT_MONTH = os.path.join(BASE, "sa_ljc_month.json")
OUT_CACHE = "/Users/mac/.local/share/TeleAgent/TeleAgent的工作空间/shop/sa_aug_cache.json"
OUT_WAREHOUSE = os.path.join(BASE, "sa_ljc_warehouse.json")
OUT_TSV = os.path.join(BASE, "sa_ljc_raw.tsv")

# ---------------------------------------------------------------
# 分页参数（2026-09-20 实测调优）
#
# 血泪事实：该接口服务端每页有 ~50s 固定开销，返回行数几乎不影响耗时。
#   旧参数 pageSize=5000 × 46 页 并发 5  → 实测 612s
#   新参数 pageSize=20000 × 12 页 并发 8 → 实测 206s（省 66%）
#
# 另：接口按【门店】分组排序，同一门店的行**连续**，全公司 22.9 万行里
# 李家村只落在其中 2 页（pageSize=20000 时为 11、12 页）。
# 故再有「页定位」缓存在 `sa_page_map.json`：日常只拉命中页 ± 1 页兜底，
# 约 130s（省 79%）；行数校验不过自动降级全量重扫。
# ---------------------------------------------------------------
PAGE_SIZE = int(os.environ.get("SA_PAGE_SIZE", "20000"))
MAX_WORKERS = int(os.environ.get("SA_WORKERS", "8"))
CACHE_MAX_AGE = 6 * 3600
PAGE_MAP = os.path.join(BASE, "sa_page_map_ljc.json")   # 李家村所在页映射（定位缓存）
MAP_TOLERANCE = 0.35                                # 定位命中行数允许 ±35% 漂移

USE_CACHE = "--use-cache" in sys.argv
NO_LOCATE = "--no-locate" in sys.argv        # 强制全量扫描（不用页定位缓存）
NO_CONDITION = "--no-condition" in sys.argv  # 禁用 condition 精准模式（退回页定位/全量）
MONTH = None
if "--month" in sys.argv:
    MONTH = sys.argv[sys.argv.index("--month") + 1]

TODAY = datetime.date.today()
if MONTH:
    y, m = MONTH.split("-")
    MONTH_FIRST = datetime.date(int(y), int(m), 1)
else:
    MONTH_FIRST = TODAY.replace(day=1)
    MONTH = MONTH_FIRST.strftime("%Y-%m")


def load_cookies(path):
    d = json.load(open(path))
    return {c["name"]: c["value"] for c in d.get("cookies", [])
            if "yonyou" in c.get("domain", "") and c.get("name") and c.get("value") is not None}


def _clean(v):
    """HTTP 头只能是 latin-1 安全字符，cookie 里的非 ASCII 要剥掉。"""
    return re.sub(r"[^\x20-\x7e]", "", str(v))


def build_headers(ck):
    return {
        "User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": YY_BASE,
        "Referer": YY_BASE + "/",
        "Cookie": "; ".join("%s=%s" % (k, _clean(v)) for k, v in ck.items()),
        "XSRF-TOKEN": _clean(ck.get("XSRF-TOKEN", "")),
        "yht_access_token": _clean(ck.get("yht_access_token", "")),
    }


def fetch_page(hdr, page_index, retries=3, timeout=240, use_condition=False):
    payload = {
        "billnum": BILLNUM,
        "page": {"pageIndex": page_index, "pageSize": PAGE_SIZE},
    }
    # 【2026-09-20 提速】condition.commonVOs 可精准锁单店：
    #   实测 recordCount 229603(全公司/3页/101s) → 14483(单店/1页/37s)
    if use_condition and STORE_ID:
        payload["condition"] = {"commonVOs": [{"itemName": "store", "value1": STORE_ID}]}
    body = json.dumps(payload).encode("utf-8")
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(SA_URL, data=body, headers=hdr, method="POST")
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=ssl.create_default_context()) as resp:
                j = json.loads(resp.read())
            if j.get("code") != 200:
                raise RuntimeError("接口 code=%s msg=%s" % (j.get("code"), str(j.get("message"))[:120]))
            dd = j.get("data") or {}
            return dd.get("recordCount"), (dd.get("recordList") or [])
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise SystemExit("✗ HTTP_%d 登录态失效（运行中段），请跑 relogin_mgr_py.py 重登经理账号" % e.code)
            last = e
        except SystemExit:
            raise
        except Exception as e:
            last = e
        if attempt < retries:
            time.sleep(2 * attempt)
    raise RuntimeError("页 %d 拉取失败: %s" % (page_index, last))


def is_hyc(rec):
    s = str(rec.get("store_name") or "")
    return STORE_KEY in s


def fetch_pages(hdr, pages, use_condition=False):
    """并发拉取指定页集合，返回 {page: [records]}。

    任一路失败即抛异常 —— 由上层决定「降级全量」而非静默漏页
    （漏页 = 渠道数字偏低，是比报错严重得多的事故）。
    """
    pages = [p for p in pages if p >= 1]
    if not pages:
        return {}
    out = {}
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(pages))) as pool:
        futs = {pool.submit(fetch_page, hdr, p, 3, 240, use_condition): p for p in pages}
        for fut in as_completed(futs):
            p = futs[fut]
            _, rl = fut.result()
            out[p] = rl
    return out


def write_page_map(pages, n_pages, total, hyc_raw):
    """记录李家村命中页，供下次「页定位」直接复用。"""
    try:
        atomic_write_json(PAGE_MAP, {
            "page_size": PAGE_SIZE, "total_pages": n_pages, "record_count": total,
            "pages": sorted(pages), "hyc_raw_rows": len(hyc_raw),
            "updated_at": time.time(),
        })
        print("  [定位] 页映射已更新: 命中页 %s / 共 %d 页（%d 行）"
              % (sorted(pages), n_pages, len(hyc_raw)))
    except Exception as e:
        print("  [定位] 写页映射失败(不影响本次): %s" % e)


def dedup(records):
    """剔除 _YD 预订单 + 按 (单号, SKU, 序列号) 去重（与李家村管线同口径）。"""
    seen, out = set(), []
    for r in records:
        code = str(r.get("code") or "")
        if code.endswith("_YD"):
            continue
        k = (code, str(r.get("productsku_cCode") or ""), str(r.get(SN_FIELD) or ""))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def auth_probe_mgr(hdr):
    """【2026-09-21 自愈】拉数前探活：401/失效时自动无头重登（playwright 版，钥匙串取密不落日志）再试。
    移植自李家村 mem17 auth_probe（wecom_api.py），根治 15 小时空窗后首档 401 静默断更。"""
    payload = {"billnum": BILLNUM, "page": {"pageIndex": 1, "pageSize": 1}}
    if STORE_ID:
        payload["condition"] = {"commonVOs": [{"itemName": "store", "value1": STORE_ID}]}
    body = json.dumps(payload).encode("utf-8")
    try:
        req = urllib.request.Request(SA_URL, data=body, headers=hdr, method="POST")
        with urllib.request.urlopen(req, timeout=60,
                                    context=ssl.create_default_context()) as resp:
            j = json.loads(resp.read())
        if j.get("code") == 200:
            rc = (j.get("data") or {}).get("recordCount")
            print("  [auth] 探针 OK recordCount=%s" % rc)
            return hdr
        raise RuntimeError("探针 code=%s" % j.get("code"))
    except SystemExit:
        raise
    except Exception as e:
        print("  [auth] 探针失败（%s），自动无头重登经理号..." % str(e)[:90], flush=True)
    # relogin 需 playwright：固定用 TeleAgent python（有 playwright，与李家村 mem17 同款）；
    # 本脚本自身可能由 WorkBuddy python 运行（无 playwright），不可用 sys.executable
    RELOGIN_INTERP = "/Users/mac/.local/share/TeleAgent/runtimes/python/bin/python3"
    if not os.path.exists(RELOGIN_INTERP):
        RELOGIN_INTERP = sys.executable
    r = subprocess.run([RELOGIN_INTERP, "/Users/mac/.local/share/TeleAgent/TeleAgent的工作空间/shophyc/relogin_mgr_py.py"],
                       capture_output=True, text=True, timeout=180)
    print("  [auth] relogin:", (r.stdout or r.stderr).strip()[:120], flush=True)
    if "LOGGED_IN" not in (r.stdout or ""):
        raise SystemExit("✗ 自动重登失败，登录态不可用（详见上方 relogin 输出）")
    if not os.path.exists(STATE):
        raise SystemExit("✗ 重登后仍找不到登录态文件")
    ck2 = load_cookies(STATE)
    if "yht_access_token" not in ck2:
        raise SystemExit("✗ 重登后登录态仍缺少 yht_access_token")
    hdr2 = build_headers(ck2)
    req = urllib.request.Request(SA_URL, data=body, headers=hdr2, method="POST")
    with urllib.request.urlopen(req, timeout=60,
                                context=ssl.create_default_context()) as resp:
        j = json.loads(resp.read())
    rc = (j.get("data") or {}).get("recordCount")
    if j.get("code") != 200 or rc is None:
        raise SystemExit("✗ 重登后探针仍失败 code=%s" % j.get("code"))
    print("  [auth] 探针恢复 OK recordCount=%s" % rc)
    return hdr2


def main():
    if not os.path.exists(STATE):
        sys.exit("✗ 找不到经理账号登录态: %s（先跑 relogin_mgr.sh）" % STATE)
    ck = load_cookies(STATE)
    if "yht_access_token" not in ck:
        sys.exit("✗ 登录态缺少 yht_access_token，请重登")
    hdr = auth_probe_mgr(build_headers(ck))

    print("▶ 拉取销售分析（经理号 %s）→ 本地过滤 store_name 含 '%s'"
          % (os.path.basename(STATE), STORE_KEY))
    print("  目标月份: %s（%s ~ %s）" % (MONTH, MONTH_FIRST, TODAY))

    # ---- 仓模式 ----
    hyc_all = []
    if USE_CACHE and os.path.exists(OUT_WAREHOUSE):
        try:
            wh = json.load(open(OUT_WAREHOUSE, encoding="utf-8"))
            age = time.time() - wh.get("fetched_at", 0)
            if age < CACHE_MAX_AGE:
                hyc_all = wh["records"]
                print("  [仓] 读本地仓（%.0f 分钟前，%d 行），跳过联网" % (age / 60, len(hyc_all)))
            else:
                print("  [仓] 已过期(%.1f h)，转联网" % (age / 3600))
        except Exception as e:
            print("  [仓] 读取失败，转联网: %s" % e)

    # ---- 【新】condition 精准模式：直锁本店（1 页 ~37s）----
    #   2026-09-20 实测：condition.commonVOs 传 store ID 可把 recordCount 从
    #   229603(全公司/3页) 缩到 14483(单店/1页)；无需页定位/全量扫描。
    #   校验不过（本店占比 <90%）自动退回常规流程，绝不静默出错。
    if not hyc_all and not NO_CONDITION and STORE_ID:
        t0 = time.time()
        print("  [精准] condition[store=%s] 直锁本店…" % STORE_ID)
        try:
            total, rows = fetch_page(hdr, 1, use_condition=True)
            n_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE if total else 1
            if n_pages > 1:
                extra = fetch_pages(hdr, range(2, n_pages + 1), use_condition=True)
                for p in sorted(extra):
                    rows = rows + extra[p]
            n_hyc = sum(1 for r in rows if is_hyc(r))
            ratio = (n_hyc / len(rows)) if rows else 0
            print("  [精准] recordCount=%s（%d 页）共 %d 行，其中本店 %d 行（%.0f%%）（%.0fs）"
                  % (total, n_pages, len(rows), n_hyc, ratio * 100, time.time() - t0))
            if n_hyc > 0 and ratio > 0.9:
                hyc_all = dedup(rows)
                print("  [精准] ✅ 命中并去重 %d 行（%.0fs）" % (len(hyc_all), time.time() - t0))
                try:
                    atomic_write_json(OUT_WAREHOUSE, {"fetched_at": time.time(),
                                                      "store": STORE_NAME, "records": hyc_all})
                except Exception as e:
                    print("  [仓] 写仓失败(不影响本次): %s" % e)
            else:
                print("  [精准] ✗ 命中异常（本店占比 %.0f%%），退回常规流程" % (ratio * 100))
        except SystemExit:
            raise
        except Exception as e:
            print("  [精准] ✗ 失败(%s)，退回常规流程" % str(e)[:100])

    # ---- 页定位模式：复用上次命中的页，只拉这几页 ±1 页兜底 ----
    if not hyc_all and not NO_LOCATE and os.path.exists(PAGE_MAP):
        try:
            pm = json.load(open(PAGE_MAP, encoding="utf-8"))
            if pm.get("page_size") != PAGE_SIZE:
                print("  [定位] 页映射 pageSize=%s ≠ 当前 %s，转全量重扫"
                      % (pm.get("page_size"), PAGE_SIZE))
            else:
                hit = sorted(set(pm.get("pages") or []))
                n_cached = int(pm.get("total_pages") or 0)
                wide = sorted({p for h in hit
                               for p in (h - 1, h, h + 1) if 1 <= p <= max(n_cached, h)})
                print("  [定位] 复用页映射 命中页 %s → 实拉 %s（%d 页，%.0f 分钟前）"
                      % (hit, wide, len(wide), (time.time() - pm.get("updated_at", 0)) / 60))
                t0 = time.time()
                got = fetch_pages(hdr, wide)
                cand = [r for p in wide for r in got.get(p, []) if is_hyc(r)]
                exp = int(pm.get("hyc_raw_rows") or 0)
                if cand and exp * (1 - MAP_TOLERANCE) <= len(cand) <= exp * (1 + MAP_TOLERANCE):
                    print("  [定位] ✅ 命中 %d 行（预期 %d，容差 ±%.0f%%），跳过全量扫描（%.0fs）"
                          % (len(cand), exp, MAP_TOLERANCE * 100, time.time() - t0))
                    # 边界体检：命中页的首/末行若仍是李家村，说明门店块可能被页边界截断
                    fp, lp = min(wide), max(wide)
                    if got.get(fp) and is_hyc(got[fp][0]):
                        print("  ⚠️ [定位] 首页(page%d)首行即李家村，块起点可能更靠前" % fp)
                    if lp < n_cached and got.get(lp) and is_hyc(got[lp][-1]):
                        print("  ⚠️ [定位] 末页(page%d)末行仍是李家村，块终点可能更靠后" % lp)
                    write_page_map([p for p in wide if any(is_hyc(r) for r in got.get(p, []))],
                                   n_cached, pm.get("record_count"), cand)
                    hyc_all = dedup(cand)
                    print("  [去重] %d → %d 行" % (len(cand), len(hyc_all)))
                else:
                    print("  [定位] ❌ 命中 %d 行，超出预期 %d 的 ±%.0f%% 容差，降级全量重扫"
                          % (len(cand), exp, MAP_TOLERANCE * 100))
        except SystemExit:
            raise
        except Exception as e:
            print("  [定位] 复用失败，降级全量: %s" % e)

    # ---- 全量扫描（首次 / 定位不可用 / 校验不过）----
    if not hyc_all:
        t0 = time.time()
        total, page1 = fetch_page(hdr, 1)
        n_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        print("  接口 recordCount=%s（全公司）→ %d 页 × %d" % (total, n_pages, PAGE_SIZE))

        hit_pages, raw = [], []
        p1 = [r for r in page1 if is_hyc(r)]
        if p1:
            hit_pages.append(1)
        raw.extend(p1)
        print("  [页 1/%d] 本页李家村 %d 行，累计 %d" % (n_pages, len(p1), len(raw)))

        rest = fetch_pages(hdr, range(2, n_pages + 1))
        for pi in sorted(rest):
            got = [r for r in rest[pi] if is_hyc(r)]
            if got:
                hit_pages.append(pi)
            raw.extend(got)
            print("  [页 %d/%d] 本页李家村 %d 行，累计 %d（%.0fs）"
                  % (pi, n_pages, len(got), len(raw), time.time() - t0))

        print("  [全量] 扫描完成：李家村原始 %d 行，命中页 %s / 共 %d 页（%.0fs）"
              % (len(raw), hit_pages, n_pages, time.time() - t0))
        write_page_map(hit_pages, n_pages, total, raw)

        hyc_all = dedup(raw)
        print("  [去重] %d → %d 行" % (len(raw), len(hyc_all)))
        try:
            atomic_write_json(OUT_WAREHOUSE, {"fetched_at": time.time(),
                                              "store": STORE_NAME, "records": hyc_all})
            print("  [仓] 已写全历史仓 %d 行" % len(hyc_all))
        except Exception as e:
            print("  [仓] 写仓失败(不影响本次): %s" % e)

    # ---- 本地按业务日期筛本月 ----
    begin = MONTH_FIRST.strftime("%Y-%m-%d")
    end = TODAY.strftime("%Y-%m-%d")
    month_rows = [r for r in hyc_all if begin <= str(r.get("dDate") or "")[:10] <= end]
    print("  [筛选] 按 dDate %s~%s 保留 %d / %d 行" % (begin, end, len(month_rows), len(hyc_all)))

    if not month_rows:
        sys.exit("✗ 本月无李家村记录，检查门店名/日期口径")

    # 规范化 dDate 为 ISO（与 update_sa_cache 口径一致）
    for r in month_rows:
        d = str(r.get("dDate") or "")[:10]
        if d:
            r["dDate"] = d

    atomic_write_json(OUT_MONTH, {"begin": begin, "end": end,
                                 "recordCount": len(month_rows), "records": month_rows})
    atomic_write_json(OUT_CACHE, {"records": month_rows})

    # 宽表 TSV（人工核对）
    cols, seen = [], set()
    for r in month_rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    with open(OUT_TSV, "w", encoding="utf-8") as f:
        f.write("\t".join(cols) + "\n")
        for r in month_rows:
            f.write("\t".join(str(r.get(c, "")) for c in cols) + "\n")

    net = sum(float(r.get("fNetMoney") or 0) for r in month_rows)
    qty = sum(float(r.get("fQuantity") or 0) for r in month_rows)
    emps = collections.Counter(r.get("iEmployeeid_name") for r in month_rows)
    print("✓ 李家村本月销售分析已落盘")
    print("  记录 %d 行 | 销售净额 ¥%.2f | 数量 %.0f" % (len(month_rows), net, qty))
    print("  业务员分布: %s" % dict(emps))
    print("  → %s" % OUT_MONTH)
    print("  → %s（merge_qudao 消费）" % OUT_CACHE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
