#!/usr/bin/env python3
"""主题切换冒烟测试：验证浅/深色动态切换、持久化与浅色下各页可读。

断言：
  1. 切换按钮把 <html>.dark 取反，body 背景色随之在深浅两套值之间变化
  2. localStorage.llmwiki_theme 与当前主题一致
  3. 刷新后主题保持（首屏防闪由 index.html 内联脚本负责）
  4. 逐页在浅色下截图，供人工确认没有"白底白字"

用法：
    python tools/smoke_theme.py                      # 对容器版
    python tools/smoke_theme.py --url http://localhost:5173
    python tools/smoke_theme.py --shots DIR
"""
from __future__ import annotations

import argparse
import pathlib
import sys

PAGES = ["工作总览", "我的知识库", "全部条目", "上传文档", "审核管理",
         "自增长看板", "可观测性", "销售澄清", "客户状态"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8501")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="admin123")
    ap.add_argument("--channel", default="msedge")
    ap.add_argument("--shots", default=None)
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("需要 playwright：pip install playwright")
        return 2

    shots = pathlib.Path(args.shots) if args.shots else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    errors: list[str] = []

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel=args.channel, headless=True)
        except Exception:
            browser = p.chromium.launch(headless=True)

        page = browser.new_page(viewport={"width": 1560, "height": 1000})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        def is_dark() -> bool:
            return page.evaluate("document.documentElement.classList.contains('dark')")

        def body_bg() -> str:
            return page.evaluate("getComputedStyle(document.body).backgroundColor")

        # 干净起点：清掉历史选择，验证"跟随系统"
        page.goto(args.url, wait_until="domcontentloaded", timeout=30_000)
        page.evaluate("localStorage.clear()")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(700)
        print(f"  [初始] 跟随系统 dark={is_dark()}  bg={body_bg()}")

        page.fill("input[autocomplete='username']", args.user)
        page.fill("input[autocomplete='current-password']", args.password)
        page.click("button:has-text('登录工作台')")
        page.wait_for_timeout(2200)

        toggle = page.locator(".theme-toggle")
        if not toggle.count():
            failures.append("顶栏找不到主题切换按钮")
            browser.close()
            _report(failures, errors)
            return 1

        for i in (1, 2):
            before_dark, before_bg = is_dark(), body_bg()
            label = toggle.inner_text().strip()
            expect_label = "浅色主题" if before_dark else "深色主题"
            toggle.click()
            page.wait_for_timeout(900)
            after_dark, after_bg = is_dark(), body_bg()
            stored = page.evaluate("localStorage.getItem('llmwiki_theme')")

            flip_ok = after_dark != before_dark
            bg_ok = after_bg != before_bg
            label_ok = label == expect_label
            stored_ok = stored == ("dark" if after_dark else "light")
            for name, ok, detail in (
                ("html.dark 取反", flip_ok, f"{before_dark}->{after_dark}"),
                ("背景色变化", bg_ok, f"{before_bg} -> {after_bg}"),
                ("按钮文案", label_ok, f"「{label}」期望「{expect_label}」"),
                ("localStorage", stored_ok, stored),
            ):
                print(f"  [{'OK' if ok else 'FAIL'}] 第{i}次切换 · {name}: {detail}")
                if not ok:
                    failures.append(f"第{i}次切换 {name} 异常（{detail}）")
            if shots:
                page.screenshot(path=str(shots / f"theme-{i}-{'dark' if after_dark else 'light'}.png"))

        # 刷新保持
        before_reload = is_dark()
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(900)
        keep = before_reload == is_dark()
        print(f"  [{'OK' if keep else 'FAIL'}] 刷新后主题保持: {before_reload} -> {is_dark()}")
        if not keep:
            failures.append("刷新后主题未保持")

        # 浅色下逐页截图（人工确认无白底白字）
        if not is_dark():
            for nav in PAGES:
                try:
                    page.click(f".nav-item:has-text('{nav}')", timeout=5000)
                    page.wait_for_timeout(1100)
                    if shots:
                        page.screenshot(path=str(shots / f"light-{nav}.png"))
                except Exception:
                    failures.append(f"浅色下导航到「{nav}」失败")
            print(f"  [OK] 浅色模式下已遍历 {len(PAGES)} 页（截图见 --shots 目录）")

        browser.close()

    _report(failures, errors)
    return 1 if failures else 0


def _report(failures: list[str], errors: list[str]) -> None:
    real = [e for e in errors if "favicon" not in e.lower()]
    print("\n=== 控制台错误 ===")
    print("  " + ("无" if not real else f"{len(real)} 条: {real[:5]}"))
    print("\n=== 结果 ===")
    if failures:
        for f in failures:
            print("   [FAIL]", f)
        print(f"   {len(failures)} 项失败")
    else:
        print("   主题切换全部通过")


if __name__ == "__main__":
    sys.exit(main())
