#!/usr/bin/env python3
"""浅色/深色主题可读性检查（WCAG 对比度，无需人工看图）。

做法：在真实浏览器里遍历可见文本元素，取其文字色与最近的"不透明背景"色，
按 WCAG 2.1 计算对比度（含 alpha 合成），列出低于阈值的元素。

阈值：正文 4.5:1；大号文本（≥18.66px 且常规体，或 ≥24px）3.0:1。
用法：
    python tools/check_contrast.py --url http://127.0.0.1:8501
    python tools/check_contrast.py --url http://localhost:5173 --theme light
"""
from __future__ import annotations

import argparse
import sys

PAGES = ["工作总览", "我的知识库", "全部条目", "上传文档", "审核管理",
         "自增长看板", "可观测性", "销售澄清", "客户状态"]

JS = r"""
() => {
  const parse = (c) => {
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(',').map((x) => parseFloat(x));
    return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
  };
  const blend = (fg, bg) => ({
    r: fg.r * fg.a + bg.r * (1 - fg.a),
    g: fg.g * fg.a + bg.g * (1 - fg.a),
    b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1,
  });
  const lum = (c) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
  };
  const ratio = (a, b) => {
    const l1 = lum(a), l2 = lum(b);
    return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
  };
  // 从元素向上找第一个非透明背景
  const bgOf = (el) => {
    let node = el, acc = null;
    while (node && node !== document.documentElement.parentNode) {
      const c = parse(getComputedStyle(node).backgroundColor);
      if (c && c.a > 0) acc = acc ? blend(acc, c) : c;
      if (acc && acc.a >= 0.999) return acc;
      node = node.parentElement;
    }
    return acc || { r: 255, g: 255, b: 255, a: 1 };
  };

  const out = [];
  const seen = new Set();
  document.querySelectorAll('body *').forEach((el) => {
    if (el.children.length) return;                       // 只看叶子节点
    const text = (el.textContent || '').trim();
    if (!text || text.length > 80) return;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') return;
    const op = parseFloat(st.opacity);
    if (Number.isFinite(op) && op < 0.15) return;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return;
    // 叶子内的 svg/icon 不算文本
    if (el.tagName === 'svg' || el.tagName === 'SVG') return;

    const fg = parse(st.color);
    if (!fg) return;
    const bg = bgOf(el);
    const eff = fg.a < 1 ? blend(fg, bg) : fg;
    const size = parseFloat(st.fontSize);
    if (!Number.isFinite(size)) return;
    const weight = parseInt(st.fontWeight || '400', 10);
    const large = size >= 24 || (size >= 18.66 && weight >= 700);
    const need = large ? 3.0 : 4.5;
    const cr = ratio(eff, bg);
    const key = `${st.color}|${Math.round(bg.r)},${Math.round(bg.g)},${Math.round(bg.b)}|${Math.round(size)}`;
    if (cr < need) {
      if (seen.has(key + text.slice(0, 12))) return;
      seen.add(key + text.slice(0, 12));
      out.push({
        tag: el.tagName.toLowerCase(),
        cls: (el.className || '').toString().slice(0, 40),
        text: text.slice(0, 34),
        color: st.color, bg: `rgb(${Math.round(bg.r)},${Math.round(bg.g)},${Math.round(bg.b)})`,
        size: Math.round(size), need, ratio: Math.round(cr * 100) / 100,
      });
    }
  });
  return out;
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8501")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin123")
    ap.add_argument("--channel", default="msedge")
    ap.add_argument("--themes", default="light,dark", help="要检查的主题，逗号分隔")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("需要 playwright：pip install playwright")
        return 2

    total_bad = 0
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel=args.channel, headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1560, "height": 1000})

        page.goto(args.url, wait_until="domcontentloaded", timeout=30_000)
        page.evaluate("localStorage.clear()")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(600)
        page.fill("input[autocomplete='username']", args.user)
        page.fill("input[autocomplete='current-password']", args.password)
        page.click("button:has-text('登录工作台')")
        page.wait_for_timeout(2200)

        for theme in [t.strip() for t in args.themes.split(",") if t.strip()]:
            # 直接设定主题（不经按钮），保证两次检查的起点一致
            page.evaluate(
                "([t]) => { localStorage.setItem('llmwiki_theme', t);"
                " document.documentElement.classList.toggle('dark', t === 'dark'); }",
                [theme])
            page.wait_for_timeout(500)
            print(f"\n===== 主题：{theme} =====")
            for nav in PAGES:
                try:
                    page.click(f".nav-item:has-text('{nav}')", timeout=6000)
                    page.wait_for_timeout(1000)
                except Exception:
                    print(f"  [skip] {nav}（导航失败）")
                    continue
                bad = page.evaluate(JS)
                total_bad += len(bad)
                flag = "OK  " if not bad else "LOW "
                print(f"  [{flag}] {nav:8s} 低对比元素 {len(bad)}")
                for item in bad[:6]:
                    print(f"          {item['tag']}.{item['cls'][:22]:22s} 「{item['text'][:18]:18s}」"
                          f" {item['color']} on {item['bg']} {item['size']}px "
                          f"对比 {item['ratio']} < {item['need']}")
                if len(bad) > 6:
                    print(f"          ... 另有 {len(bad) - 6} 个")
        browser.close()

    print(f"\n=== 汇总：低对比元素合计 {total_bad} 个 ===")
    return 1 if total_bad else 0


if __name__ == "__main__":
    sys.exit(main())
