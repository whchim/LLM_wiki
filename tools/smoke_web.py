#!/usr/bin/env python3
"""前端冒烟测试：登录后逐页断言标题渲染，并收集控制台错误。

用途：Vue 工作台改动后的最小回归——比人工点一遍快，也比"构建通过"更接近真实。
前置：vite dev（:5173）与 FastAPI（:8000，由 vite 代理）都在运行。

用法：
    python tools/smoke_web.py                 # 无头，仅断言
    python tools/smoke_web.py --shots DIR     # 同时把每页截图写入 DIR
    python tools/smoke_web.py --url http://localhost:5173 --channel msedge
"""
from __future__ import annotations

import argparse
import pathlib
import sys

# (导航文案, 期望出现的页面标题)
PAGES = [
    ("工作总览", "你好"),
    ("我的知识库", "我的知识库"),
    ("全部条目", "全部知识条目"),
    ("上传文档", "上传文档"),
    ("审核管理", "审核管理"),
    ("自增长看板", "自增长看板"),
    ("可观测性", "可观测性"),
    ("销售澄清", "销售澄清"),
    ("客户状态", "客户状态"),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:5173")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin123")
    ap.add_argument("--channel", default="msedge", help="Playwright 浏览器通道（msedge/chrome）；空则用内置 chromium")
    ap.add_argument("--shots", default=None, help="截图输出目录（可选）")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("需要 playwright：pip install playwright && playwright install chromium")
        return 2

    errors: list[str] = []
    failures: list[str] = []
    shots = pathlib.Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel=args.channel, headless=True) if args.channel \
                else p.chromium.launch(headless=True)
        except Exception as exc:  # 通道不可用时退回内置 chromium
            print(f"[warn] {args.channel} 不可用（{exc.__class__.__name__}），改用内置 chromium")
            browser = p.chromium.launch(headless=True)

        page = browser.new_page(viewport={"width": 1560, "height": 1000})
        page.on("console", lambda m: errors.append(f"[console.{m.type}] {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))

        # 1) 登录页
        page.goto(args.url, wait_until="networkidle", timeout=30_000)
        page.wait_for_timeout(800)
        if shots:
            page.screenshot(path=str(shots / "01-login.png"))
        if not page.locator("button:has-text('登录工作台')").count():
            failures.append("登录页未渲染（找不到登录按钮）")
            browser.close()
            _report(errors, failures)
            return 1

        # 2) 登录
        page.fill("input[autocomplete='username']", args.user)
        page.fill("input[autocomplete='current-password']", args.password)
        page.click("button:has-text('登录工作台')")
        page.wait_for_timeout(2500)
        if shots:
            page.screenshot(path=str(shots / "02-overview.png"))

        # 3) 逐页断言
        for idx, (nav, expect) in enumerate(PAGES, start=3):
            try:
                page.click(f".nav-item:has-text('{nav}')", timeout=6000)
                page.wait_for_timeout(1300)
            except Exception:
                failures.append(f"{nav}: 导航点击失败（可能无权限）")
                continue
            body = page.inner_text("body")
            ok = expect in body
            print(f"  {'[OK]  ' if ok else '[FAIL]'} {nav:12s} 期望标题「{expect}」")
            if not ok:
                failures.append(f"{nav}: 未找到标题「{expect}」")
            if shots:
                page.screenshot(path=str(shots / f"{idx:02d}-{nav}.png"))

        browser.close()

    _report(errors, failures)
    return 1 if failures else 0


def _report(errors: list[str], failures: list[str]) -> None:
    real_errors = [e for e in errors if "favicon" not in e.lower()]
    print("\n=== 页面控制台错误 ===")
    if real_errors:
        for e in real_errors[:20]:
            print("  ", e)
        print(f"   合计 {len(real_errors)} 条")
    else:
        print("   无")
    print("\n=== 结果 ===")
    if failures:
        for f in failures:
            print("   [FAIL]", f)
        print(f"   {len(failures)} 项失败")
    else:
        print("   全部页面渲染正常")


if __name__ == "__main__":
    sys.exit(main())
