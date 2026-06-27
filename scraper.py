#!/usr/bin/env python3
"""
一抽露魂爬蟲
- 搜尋頁先篩選：只進「尚有大賞」或「大賞齊全」的商品
- 商品頁解析「賞品配率」區塊取得各賞名稱、數量、機率
- 產出互動式 HTML 報告
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

# ── 設定 ─────────────────────────────────────────────────────────────────────
SEARCHES = [
    {
        "label": "女神",
        "url": "https://www.ruten.com.tw/event/ichiban_search.php/?q=%E5%A5%B3%E7%A5%9E",
        "skip_keywords": ["勝利女神"],   # NIKKE 系列，略過不查
    },
    {
        "label": "老婆",
        "url": "https://www.ruten.com.tw/event/ichiban_search.php/?q=%E8%80%81%E5%A9%86",
        "skip_keywords": [],
    },
]

# 只爬有這些徽章的商品
TARGET_BADGES = ["尚有大賞", "大賞齊全"]

# 所有搜尋都過濾掉這些關鍵字（卡牌類、KPOP 等非模型商品）
GLOBAL_SKIP = ["小卡", "KPOP", "kpop", "K-POP"]

DELAY_MS    = 1500
OUTPUT_FILE = Path("result.html")
DEBUG       = True

# ── 彈跳視窗處理 ──────────────────────────────────────────────────────────────
async def dismiss_modal(page) -> None:
    """偵測並關閉「我明白了」彈跳視窗（若有）。"""
    try:
        btn = page.get_by_text("我明白了", exact=True)
        if await btn.is_visible(timeout=2500):
            await btn.click()
            await page.wait_for_timeout(600)
            print("  → 已關閉彈跳視窗")
    except Exception:
        pass  # 沒有彈窗，正常繼續

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── 搜尋頁：只收有大賞的商品 ───────────────────────────────────────────────────
SCAN_CARDS_JS = """
(targetBadges) => {
    const results = [];
    const seen    = new Set();

    // 只走訪露天商品連結 <a href=".../item/數字...">
    for (const a of document.querySelectorAll('a[href*="ruten.com.tw/item/"]')) {
        const href = a.href;
        if (!href || seen.has(href)) continue;
        // 確保是商品頁（/item/純數字 或 /item/show?）
        if (!href.match(/ruten\\.com\\.tw\\/item\\/(?:\\d+|\\/show)/)) continue;

        // 往上找整個卡片容器（需包含大賞徽章）
        // rt-product-card 是露天商品卡片的 wrapper class
        const card = a.closest('[class*="rt-product-card"],[class*="product-card"],li,[class*="card"]')
                  || a.closest('div,article')
                  || a.parentElement;
        if (!card || card === document.body) continue;

        const cardText = card.textContent || '';

        // 必須包含目標徽章
        const badge = targetBadges.find(b => cardText.includes(b));
        if (!badge) continue;

        seen.add(href);

        // 價格：抓最後出現的「數字/抽」（避免打折前的原價干擾）
        const priceMatches = [...cardText.matchAll(/(\\d+)\\s*\\/\\s*抽/g)];
        const price = priceMatches.length
            ? parseInt(priceMatches[priceMatches.length - 1][1])
            : null;

        // 剩餘：「剩餘 XX/YYY」
        const remMatch = cardText.match(/剩餘\\s*(\\d+)\\s*\\/\\s*(\\d+)/);
        const remaining = remMatch ? parseInt(remMatch[1]) : null;
        const totalPulls = remMatch ? parseInt(remMatch[2]) : null;

        // 商品名：優先用 <a title="..."> 屬性，避免「開套優惠」等標籤干擾
        const name = a.title?.trim() || a.textContent?.trim() || '';

        const img = card.querySelector('img')?.src || '';

        results.push({ url: href, name, price, remaining, totalPulls, badge, img });
    }
    return results;
}
"""

# ── 商品頁：解析「賞品配率」區塊（使用精確 class 選擇器）─────────────────────
PARSE_PRIZES_JS = """
() => {
    const prizes = [];

    // 賞品列表容器
    const list = document.querySelector('ul.ichiban-prize-list');
    if (!list) return prizes;

    for (const item of list.querySelectorAll('li.ichiban-prize-item')) {
        // 賞別（A賞 / B賞 / G賞…）
        const tier = item.querySelector('.ichiban-prize-tier-text')?.textContent?.trim() || '';
        if (!tier) continue;

        // 品名
        const name = item.querySelector('.ichiban-prize-title, h3')?.textContent?.trim() || '';

        // 已抽完判定：有 .ichiban-prize-sold-out 元素
        const isSoldOut = !!item.querySelector('.ichiban-prize-sold-out');

        // 數量 & 機率：從 dl.ichiban-prize-status 的 dt/dd 配對取出
        let qty = null, qtyTotal = null, prob = 0;
        const statusDl = item.querySelector('.ichiban-prize-status');
        if (statusDl) {
            for (const div of statusDl.querySelectorAll('div')) {
                const label = div.querySelector('dt')?.textContent?.trim();
                const value = div.querySelector('dd')?.textContent?.trim() || '';
                if (label === '數量') {
                    const m = value.match(/(\\d+)\\s*\\/\\s*(\\d+)/);
                    if (m) { qty = parseInt(m[1]); qtyTotal = parseInt(m[2]); }
                }
                if (label === '機率') {
                    const m = value.match(/([\\d.]+)%/);
                    if (m) prob = parseFloat(m[1]);
                }
            }
        }

        prizes.push({ tier, name, prob, qty, qtyTotal, isSoldOut });
    }

    return prizes;
}
"""

# ── 主流程 ────────────────────────────────────────────────────────────────────
async def main() -> None:
    print("一抽露魂爬蟲啟動（只抓尚有大賞 / 大賞齊全）\n")
    products: list[dict] = []
    seen_titles: set[str] = set()   # 跨搜尋去重：記錄已抓過的商品標題

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=False,           # 本地測試開著看；GitHub Actions 改 True
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            locale="zh-TW",
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        for search in SEARCHES:
            label        = search["label"]
            url          = search["url"]
            skip_kws     = search.get("skip_keywords", [])
            print(f"[{label}] 搜尋頁：{url}")

            try:
                await page.goto(url, wait_until="networkidle", timeout=60000)
                await page.wait_for_timeout(3000)
                await dismiss_modal(page)   # ← 關彈窗

                if DEBUG:
                    Path(f"debug_{label}.html").write_text(
                        await page.content(), encoding="utf-8"
                    )
                    print(f"  ✓ 已存 debug_{label}.html")

                cards = await page.evaluate(SCAN_CARDS_JS, TARGET_BADGES)

                # 合併全域 + 本搜尋的過濾關鍵字
                all_skip = GLOBAL_SKIP + skip_kws
                before = len(cards)
                cards = [c for c in cards if not any(kw in c["name"] for kw in all_skip)]
                skipped = before - len(cards)
                if skipped:
                    print(f"  略過 {skipped} 個商品（關鍵字：{all_skip}）")

                print(f"  找到 {len(cards)} 個有大賞的商品\n")

                for i, card in enumerate(cards, 1):
                    # 跨搜尋去重：相同標題只爬一次
                    title_key = card["name"].strip()
                    if title_key in seen_titles:
                        print(f"  [{i:>2}/{len(cards)}] ⟳ 重複略過：{title_key[:50]}")
                        continue
                    seen_titles.add(title_key)

                    print(
                        f"  [{i:>2}/{len(cards)}] {card['badge']:6s} "
                        f"NT${card['price'] or '?':>4}  "
                        f"剩{card['remaining']}/{card['totalPulls']}  "
                        f"{card['name'][:45]}"
                    )

                    # 進商品頁
                    prizes = []
                    try:
                        await page.goto(card["url"], wait_until="domcontentloaded", timeout=30000)
                        await page.wait_for_timeout(DELAY_MS)
                        await dismiss_modal(page)   # ← 商品頁也可能有彈窗
                        prizes = await page.evaluate(PARSE_PRIZES_JS)
                    except Exception as e:
                        print(f"          ↳ 商品頁錯誤：{e}")

                    if prizes:
                        print(f"          ↳ 解析到 {len(prizes)} 個賞項")
                    else:
                        print(f"          ↳ 未解析到賞品配率")

                    products.append({
                        "source":     label,
                        "name":       card["name"],
                        "price":      card["price"],
                        "url":        card["url"],
                        "img":        card.get("img", ""),
                        "badge":      card["badge"],
                        "remaining":  card["remaining"],
                        "totalPulls": card["totalPulls"],
                        "prizes":     prizes,
                        "parsedOk":   len(prizes) >= 2,
                    })

                    # 回到搜尋頁繼續
                    await page.goto(url, wait_until="networkidle", timeout=60000)
                    await page.wait_for_timeout(2000)
                    await dismiss_modal(page)   # ← 回來後可能再出現

            except Exception as exc:
                print(f"  [錯誤] {exc}")

        await browser.close()

    print(f"\n爬取完成，共 {len(products)} 件商品")
    OUTPUT_FILE.write_text(generate_html(products), encoding="utf-8")
    print(f"結果已儲存至 {OUTPUT_FILE}")


# ── HTML 產生 ─────────────────────────────────────────────────────────────────
def generate_html(products: list[dict]) -> str:
    updated   = datetime.now().strftime("%Y-%m-%d %H:%M")
    data_json = json.dumps(products, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>一抽露魂分析 {updated}</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#0f172a;--surface:#1e293b;--surface2:#243147;--border:#2d3f58;
  --text:#e2e8f0;--text2:#7f96b4;--accent:#e63946;
  --green:#22c55e;--yellow:#f59e0b;--red:#ef4444;--blue:#60a5fa;
  --font:system-ui,-apple-system,'Segoe UI',sans-serif;
}}
body{{background:var(--bg);color:var(--text);font-family:var(--font);font-size:14px}}

header{{background:var(--surface);border-bottom:1px solid var(--border);padding:18px 24px}}
header h1{{font-size:20px;font-weight:700;color:var(--accent)}}
header p{{color:var(--text2);font-size:12px;margin-top:3px}}
.stats{{display:flex;gap:16px;margin-top:14px;flex-wrap:wrap}}
.stat{{background:var(--surface2);border-radius:8px;padding:8px 16px}}
.stat-val{{font-size:20px;font-weight:700;color:var(--accent)}}
.stat-label{{font-size:11px;color:var(--text2);margin-top:1px}}

.controls{{padding:12px 24px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;
           border-bottom:1px solid var(--border);background:var(--surface)}}
.controls input{{background:var(--bg);border:1px solid var(--border);color:var(--text);
  padding:6px 12px;border-radius:6px;font-size:13px;width:220px}}
.controls input:focus{{outline:none;border-color:var(--accent)}}
.tag{{background:var(--surface2);border:1px solid var(--border);color:var(--text2);
  padding:5px 12px;border-radius:6px;cursor:pointer;font-size:12px;transition:all .15s;white-space:nowrap}}
.tag.on{{background:var(--accent);border-color:var(--accent);color:#fff}}
.sep{{width:1px;height:24px;background:var(--border);margin:0 4px}}

.wrap{{overflow-x:auto;padding:0 24px 32px}}
table{{width:100%;border-collapse:collapse;margin-top:14px;min-width:780px}}
th{{background:var(--surface);color:var(--text2);font-size:11px;text-transform:uppercase;
  letter-spacing:.05em;padding:9px 12px;text-align:left;border-bottom:2px solid var(--border);
  white-space:nowrap;cursor:pointer;user-select:none}}
th:hover{{color:var(--text)}}
th.asc::after{{content:" ↑";color:var(--accent)}}
th.desc::after{{content:" ↓";color:var(--accent)}}
td{{padding:9px 12px;border-bottom:1px solid var(--border);vertical-align:middle}}
tr.prow{{cursor:pointer;transition:background .1s}}
tr.prow:hover{{background:var(--surface2)}}
tr.drow{{display:none;background:var(--surface2)}}
tr.drow.open{{display:table-row}}

.badge{{display:inline-block;padding:2px 7px;border-radius:4px;font-size:11px;font-weight:600}}
.badge-女神{{background:#7c3aed22;color:#c4b5fd;border:1px solid #7c3aed55}}
.badge-老婆{{background:#be185d22;color:#f9a8d4;border:1px solid #be185d55}}
.badge-尚有大賞{{background:#f59e0b22;color:#fcd34d;border:1px solid #f59e0b55}}
.badge-大賞齊全{{background:#22c55e22;color:#86efac;border:1px solid #22c55e55}}

.pname a{{color:var(--text);text-decoration:none;font-weight:500}}
.pname a:hover{{color:var(--accent)}}
.arrow{{float:right;color:var(--text2);transition:transform .2s;font-style:normal}}
tr.prow.open .arrow{{transform:rotate(180deg)}}

.pct{{font-weight:600}}
.pct-high{{color:var(--green)}}
.pct-mid{{color:var(--yellow)}}
.pct-low{{color:var(--red)}}
.pct-none{{color:var(--text2)}}

.price-val{{font-weight:600}}
.price-cheap{{color:var(--green)}}
.price-mid{{color:var(--yellow)}}
.price-exp{{color:var(--red)}}

.rem-bar-wrap{{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text2)}}
.rem-bar-bg{{background:var(--border);border-radius:3px;width:80px;height:6px;overflow:hidden}}
.rem-bar{{height:6px;border-radius:3px;background:var(--blue)}}

/* 展開的賞品明細 */
.detail-inner{{padding:16px 20px}}
.detail-inner h4{{font-size:11px;color:var(--text2);text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px}}
.prize-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px}}
.prize-card{{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:10px 12px}}
.prize-card-low{{opacity:.6;border-style:dashed}}
.prize-tier{{display:inline-block;font-size:10px;font-weight:700;padding:1px 6px;border-radius:3px;
  margin-bottom:4px;background:var(--accent)22;color:var(--accent);border:1px solid var(--accent)44}}
.prize-tier-low{{background:#33415522;color:var(--text2);border-color:#33415566}}
.prize-name{{font-size:12px;color:var(--text2);margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.prize-prob{{font-size:15px;font-weight:700}}
.prize-qty{{font-size:11px;color:var(--text2);margin-top:2px}}
.prob-bar-bg{{background:var(--border);border-radius:3px;height:5px;margin-top:5px;overflow:hidden}}
.prob-bar{{height:5px;border-radius:3px}}
.gone-note{{font-size:11px;color:var(--text2);margin-bottom:8px;padding:4px 8px;
  background:var(--surface);border-radius:4px;display:inline-block}}
.best-banner{{display:flex;align-items:center;gap:12px;margin-bottom:12px;
  padding:10px 14px;background:linear-gradient(90deg,#1e3a5f,#1e293b);
  border:1px solid #3b82f655;border-radius:8px;flex-wrap:wrap}}
.best-banner .best-label{{font-size:11px;color:var(--text2);white-space:nowrap}}
.best-banner .best-tier{{font-size:12px;font-weight:700;padding:2px 8px;border-radius:4px;
  background:var(--accent)22;color:var(--accent);border:1px solid var(--accent)44}}
.best-banner .best-name{{font-size:13px;font-weight:500;color:var(--text);flex:1;min-width:0;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.best-banner .best-prob{{font-size:18px;font-weight:700;color:var(--green);white-space:nowrap}}
.best-banner .best-price{{font-size:12px;color:var(--text2);white-space:nowrap}}

.empty{{padding:48px;text-align:center;color:var(--text2)}}
.no-prize{{color:var(--text2);font-size:12px;font-style:italic}}
</style>
</head>
<body>

<header>
  <h1>一抽露魂 機率分析</h1>
  <p>更新時間：{updated}　只顯示「尚有大賞」與「大賞齊全」商品</p>
  <div class="stats" id="statsRow"></div>
</header>

<div class="controls">
  <input id="q" placeholder="搜尋商品名稱...">
  <div class="sep"></div>
  <span style="color:var(--text2);font-size:12px">大賞：</span>
  <button class="tag on" data-f="badge" data-v="all">全部</button>
  <button class="tag" data-f="badge" data-v="尚有大賞">尚有大賞</button>
  <button class="tag" data-f="badge" data-v="大賞齊全">大賞齊全</button>
</div>

<div class="wrap">
<table id="tbl">
  <thead><tr>
    <th>來源</th>
    <th>商品名稱</th>
    <th data-col="price">單抽價格</th>
    <th data-col="rem">剩餘抽數</th>
    <th data-col="remPct">剩餘%</th>
    <th data-col="topProb">大賞機率</th>
    <th data-col="origProb">開套機率</th>
    <th>狀態</th>
  </tr></thead>
  <tbody id="tbody"></tbody>
</table>
<div class="empty" id="empty" style="display:none">沒有符合條件的商品</div>
</div>

<script>
const DATA = {data_json};

/* ─ 賞品分類 ─
   低賞判定：名稱含「景品」OR 機率 > 50%（G賞類大量隨機填充賞）
   已抽完：isSoldOut === true OR prob === 0 OR qty === 0 */
function categorizePrizes(prizes) {{
  if (!prizes?.length) return {{ main:[], low:[], gone:[], lowProb:0, lowTiers:'' }};
  const gone = prizes.filter(p => p.isSoldOut || p.prob === 0 || p.qty === 0);
  const live = prizes.filter(p => !p.isSoldOut && p.prob > 0 && p.qty !== 0);
  const isLow = p => p.name.includes('景品') || p.name.includes('絨毛') || p.prob > 50;
  const low   = live.filter(p => isLow(p));
  const main  = live.filter(p => !isLow(p));
  const lowProb  = +low.reduce((s,p) => s+p.prob, 0).toFixed(2);
  const lowTiers = low.map(p=>p.tier).join('+');
  return {{ main, low, gone, lowProb, lowTiers }};
}}

/* ─ 工具 ─ */
const pctClass  = p => p===null?'pct-none':p>=5?'pct-high':p>=2?'pct-mid':'pct-low';
const priceClass = p => !p?'':p<=150?'price-cheap':p<=300?'price-mid':'price-exp';
const probColor  = p => p>=5?'#22c55e':p>=2?'#f59e0b':'#ef4444';
const remPct     = d => (d.remaining!=null&&d.totalPulls) ? d.remaining/d.totalPulls*100 : null;

// 大賞機率 = 所有主賞機率加總（排除低賞 / 已抽完）
function topProb(d) {{
  const {{ main }} = categorizePrizes(d.prizes);
  if (!main.length) return null;
  return +main.reduce((s,p) => s+p.prob, 0).toFixed(2);
}}

// 個別主賞中機率最高的那一項
function bestPrize(d) {{
  const {{ main }} = categorizePrizes(d.prizes);
  if (!main.length) return null;
  return main.reduce((a,b) => b.prob > a.prob ? b : a, main[0]);
}}

// 開套大賞機率 = 以 qtyTotal/totalPulls 算出的原始機率加總（排除景品/絨毛/高機率低賞）
function origTopProb(d) {{
  if (!d.totalPulls) return null;
  const main = d.prizes.filter(p => {{
    if (!p.qtyTotal) return false;
    const op = p.qtyTotal / d.totalPulls * 100;
    return !p.name.includes('景品') && !p.name.includes('絨毛') && op <= 50;
  }});
  if (!main.length) return null;
  return +(main.reduce((s,p) => s + p.qtyTotal / d.totalPulls * 100, 0)).toFixed(2);
}}

/* ─ 統計列 ─ */
function renderStats(items) {{
  const prices = items.map(d=>d.price).filter(Boolean);
  const avgP   = prices.length ? Math.round(prices.reduce((a,b)=>a+b,0)/prices.length) : '—';

  // CP最佳 = 大賞機率(%) ÷ 單抽價格 最高者
  const withCP = items.filter(d => topProb(d) && d.price);
  let cpHtml = '<span style="color:var(--text2);font-size:13px">—</span>';
  if (withCP.length) {{
    const best = withCP.reduce((a,b) =>
      (topProb(b)/b.price) > (topProb(a)/a.price) ? b : a, withCP[0]);
    const prob  = topProb(best);
    const ratio = (prob / best.price * 100).toFixed(3);  // % per NT$100
    cpHtml = `<a href="${{best.url}}" target="_blank"
        style="color:var(--green);text-decoration:none;display:block;line-height:1.3">
        <span style="font-size:18px;font-weight:700">${{prob}}%</span>
        <span style="font-size:12px;color:var(--text2)"> @ NT$${{best.price}}</span><br>
        <span style="font-size:11px;color:var(--text2)">${{best.name.substring(0,22)}}...</span>
      </a>`;
  }}

  document.getElementById('statsRow').innerHTML =
    `<div class="stat"><div class="stat-val">${{items.length}}</div><div class="stat-label">商品數</div></div>`+
    `<div class="stat"><div class="stat-val">NT$${{avgP}}</div><div class="stat-label">平均單抽</div></div>`+
    `<div class="stat" style="min-width:200px"><div class="stat-label" style="margin-bottom:4px">CP最佳商品</div>${{cpHtml}}</div>`+
    `<div class="stat"><div class="stat-val">${{items.filter(d=>d.parsedOk).length}}</div><div class="stat-label">已解析賞項</div></div>`;
}}

/* ─ 列渲染 ─ */
function renderRows(items) {{
  const tbody = document.getElementById('tbody');
  tbody.innerHTML='';
  document.getElementById('empty').style.display = items.length?'none':'block';
  renderStats(items);

  items.forEach(item => {{
    const tp  = topProb(item);
    const op  = origTopProb(item);
    const tpWins = tp !== null && op !== null && tp > op;
    const opWins = op !== null && tp !== null && op > tp;
    const rp  = remPct(item);
    const bar = rp!==null ? Math.round(rp) : 0;

    // ── 賞品卡片（分主賞 / 低賞合計 / 已抽完提示）
    const {{ main, low, gone, lowProb, lowTiers }} = categorizePrizes(item.prizes);

    const mkCard = (p, isLow=false) => {{
      const bw = Math.min(100, Math.max(3, Math.round(p.prob * 1.5)));
      return `<div class="prize-card${{isLow?' prize-card-low':''}}">
        <div class="prize-tier${{isLow?' prize-tier-low':{{}}}}">${{p.tier}}</div>
        <div class="prize-name" title="${{p.name}}">${{p.name||'—'}}</div>
        <div class="prize-prob ${{pctClass(p.prob)}}">${{p.prob}}%</div>
        <div class="prize-qty">${{p.qty!=null?p.qty+' / '+p.qtyTotal+' 件':''}}</div>
        <div class="prob-bar-bg"><div class="prob-bar" style="width:${{bw}}%;background:${{probColor(p.prob)}}"></div></div>
      </div>`;
    }};

    const mainCards = main.map(p => mkCard(p)).join('');
    const lowCard   = low.length ? `<div class="prize-card prize-card-low">
        <div class="prize-tier prize-tier-low">${{lowTiers||'低賞'}}</div>
        <div class="prize-name">景品 / 隨機低賞（${{low.length}} 種合計）</div>
        <div class="prize-prob pct-none">${{lowProb}}%</div>
        <div class="prize-qty">&nbsp;</div>
        <div class="prob-bar-bg"><div class="prob-bar" style="width:${{Math.min(100,lowProb)}}%;background:#475569"></div></div>
      </div>` : '';
    const goneNote  = gone.length
      ? `<div class="gone-note">已抽完：${{gone.map(p=>p.tier).join('、')}}</div>` : '';
    // 最佳賞 banner（主賞中機率最高者 + 本商品單抽價格）
    const bp = bestPrize(item);
    const bestBanner = bp ? `<div class="best-banner">
      <span class="best-label">最佳大賞</span>
      <span class="best-tier">${{bp.tier}}</span>
      <span class="best-name">${{bp.name}}</span>
      <span class="best-prob">${{bp.prob}}%</span>
      <span class="best-price">${{item.price ? 'NT$'+item.price+' / 抽' : ''}}</span>
    </div>` : '';

    const prizeGrid = `${{bestBanner}}${{goneNote}}<div class="prize-grid">${{mainCards}}${{lowCard}}</div>`;

    const prow = document.createElement('tr');
    prow.className = 'prow';
    prow.innerHTML = `
      <td>
        <span class="badge badge-${{item.source}}">${{item.source}}</span><br>
        <span class="badge badge-${{item.badge}}" style="margin-top:3px">${{item.badge}}</span>
      </td>
      <td class="pname">
        <i class="arrow">▾</i>
        <a href="${{item.url}}" target="_blank" onclick="event.stopPropagation()">${{item.name.substring(0,55)}}</a>
      </td>
      <td><span class="price-val ${{priceClass(item.price)}}">${{item.price?'NT$'+item.price:'—'}}</span></td>
      <td>${{item.remaining!=null?item.remaining+' / '+item.totalPulls:'—'}}</td>
      <td>
        ${{rp!==null?`<div class="rem-bar-wrap">
          <span style="min-width:34px">${{rp.toFixed(0)}}%</span>
          <div class="rem-bar-bg"><div class="rem-bar" style="width:${{bar}}%"></div></div>
        </div>`:'—'}}
      </td>
      <td>${{tp!==null?`<span class="pct ${{pctClass(tp)}}" style="${{tpWins?'font-weight:800;text-shadow:0 0 8px currentColor':opWins?'opacity:.55':''}}">${{tp}}%</span>`:'<span class="pct-none">—</span>'}}</td>
      <td>${{op!==null?`<span class="pct ${{pctClass(op)}}" style="${{opWins?'font-weight:800;text-shadow:0 0 8px currentColor':'opacity:.55'}}">${{op}}%</span>`:'<span class="pct-none">—</span>'}}</td>
      <td>${{item.parsedOk
        ?'<span class="badge" style="background:#22c55e22;color:#86efac;border:1px solid #22c55e44">✓ 已解析</span>'
        :'<span class="badge" style="background:#f59e0b22;color:#fcd34d;border:1px solid #f59e0b44">? 待確認</span>'}}</td>
    `;

    const drow = document.createElement('tr');
    drow.className = 'drow';
    const dtd = document.createElement('td');
    dtd.colSpan = 8;
    dtd.innerHTML = `<div class="detail-inner">
      <h4>賞品配率 — 總抽數 ${{item.totalPulls||'?'}}</h4>
      ${{item.parsedOk
        ? prizeGrid
        : `<p class="no-prize">未能解析賞品配率，請至 <a href="${{item.url}}" target="_blank" style="color:var(--accent)">商品頁面</a> 查看</p>`
      }}
    </div>`;
    drow.appendChild(dtd);

    prow.addEventListener('click', () => {{
      prow.classList.toggle('open');
      drow.classList.toggle('open');
    }});
    tbody.appendChild(prow);
    tbody.appendChild(drow);
  }});
}}

/* ─ 篩選 & 排序 ─ */
let state = {{ badge:'all', q:'', col:'topProb', asc:false }};

function apply() {{
  let rows = DATA.slice();
  if (state.badge!=='all')  rows = rows.filter(d=>d.badge===state.badge);
  if (state.q) {{
    const q = state.q.toLowerCase();
    rows = rows.filter(d=>d.name.toLowerCase().includes(q));
  }}
  if (state.col) {{
    rows.sort((a,b) => {{
      if (state.col==='topProb') {{
        const tpa=topProb(a)??-1, tpb=topProb(b)??-1;
        const aUp=(tpa>(origTopProb(a)??-1))?1:0, bUp=(tpb>(origTopProb(b)??-1))?1:0;
        if (aUp!==bUp) return bUp-aUp;
        return state.asc?tpa-tpb:tpb-tpa;
      }}
      let va,vb;
      if (state.col==='price')    {{va=a.price||9999;     vb=b.price||9999;}}
      if (state.col==='rem')      {{va=a.remaining??9999; vb=b.remaining??9999;}}
      if (state.col==='remPct')   {{va=remPct(a)??-1;     vb=remPct(b)??-1;}}
      if (state.col==='origProb') {{va=origTopProb(a)??-1;vb=origTopProb(b)??-1;}}
      return state.asc?va-vb:vb-va;
    }});
  }}
  renderRows(rows);
  // 更新 th 箭頭
  document.querySelectorAll('th[data-col]').forEach(th => {{
    th.classList.remove('asc','desc');
    if (th.dataset.col===state.col) th.classList.add(state.asc?'asc':'desc');
  }});
}}

document.getElementById('q').addEventListener('input', e=>{{ state.q=e.target.value; apply(); }});

document.querySelectorAll('.tag').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const f=btn.dataset.f, v=btn.dataset.v;
    state[f]=v;
    document.querySelectorAll(`.tag[data-f="${{f}}"]`).forEach(b=>b.classList.remove('on'));
    btn.classList.add('on');
    apply();
  }});
}});

document.querySelectorAll('th[data-col]').forEach(th => {{
  th.addEventListener('click', () => {{
    const c=th.dataset.col;
    if (state.col===c) state.asc=!state.asc; else {{state.col=c;state.asc=true;}}
    apply();
  }});
}});

apply();
</script>
</body>
</html>"""


if __name__ == "__main__":
    asyncio.run(main())
