#!/usr/bin/env python3
"""Multi-source public metrics parser for Gonka ecosystem.

Collects:
- Gonka nodes dashboard metrics (Total Compute Power, Validators, Next PoC)
- Discord invite counts (online members, total members)
- X(Twitter) followers for @gonka_ai
- GitHub stars for gonka-ai/gonka
- HEX exchange current price for GNK OTC page
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass, asdict
from typing import Optional

from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PlaywrightTimeoutError


NODE_URLS = [
    "http://node1.gonka.ai:8000",
    "http://node2.gonka.ai:8000",
]

DISCORD_URL = "https://discord.com/invite/RADwCT2U6R"
X_URL = "https://x.com/gonka_ai"
GITHUB_URL = "https://github.com/gonka-ai/gonka/"
HEX_URL = "https://hex.exchange/otc/gonka38261660"


@dataclass
class NodeMetrics:
    url: str
    available: bool
    total_compute_power: Optional[str] = None
    validators: Optional[str] = None
    next_poc: Optional[str] = None
    error: Optional[str] = None


@dataclass
class DiscordMetrics:
    online: Optional[str] = None
    members: Optional[str] = None
    error: Optional[str] = None


@dataclass
class XMetrics:
    followers: Optional[str] = None
    error: Optional[str] = None


@dataclass
class GitHubMetrics:
    stars: Optional[str] = None
    error: Optional[str] = None


@dataclass
class HexMetrics:
    price: Optional[str] = None
    error: Optional[str] = None


def clean_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def find_near_label(text: str, label_regex: str, value_regex: str, window: int = 120) -> Optional[str]:
    match = re.search(label_regex, text, flags=re.IGNORECASE)
    if not match:
        return None
    start = max(0, match.start() - window)
    end = min(len(text), match.end() + window)
    chunk = text[start:end]

    for m in re.finditer(value_regex, chunk, flags=re.IGNORECASE):
        value = clean_spaces(m.group(0))
        if value:
            return value
    return None


async def goto_soft(page: Page, url: str, timeout_ms: int = 15000) -> tuple[bool, Optional[str]]:
    try:
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        if not resp:
            return False, "No response"
        if resp.status >= 400:
            return False, f"HTTP {resp.status}"
        return True, None
    except PlaywrightTimeoutError:
        return False, "Timeout"
    except Exception as exc:
        return False, str(exc)


async def parse_node(page: Page, url: str) -> NodeMetrics:
    ok, err = await goto_soft(page, url)
    if not ok:
        return NodeMetrics(url=url, available=False, error=err)

    await page.wait_for_selector("text=Total Compute Power", timeout=15000)
    await page.wait_for_timeout(2000)

    total = await page.evaluate(
        """
        () => {
          const labels = [...document.querySelectorAll('[data-value="Total Compute Power"]')];
          for (const label of labels) {
            const card = label.closest('div[class*="bg-base-100"]') || label.closest('div');
            if (!card) continue;
            const valueSpan = card.querySelector('h6 [data-value]');
            const value = valueSpan?.getAttribute('data-value') || valueSpan?.textContent;
            if (value) return value;
          }
          return null;
        }
        """
    )

    text = clean_spaces(await page.inner_text("body"))
    validators = find_near_label(text, r"Validators|Валидатор\w*", r"\d{1,8}")
    next_poc = find_near_label(text, r"Next\s*PoC", r"\d+\s*h\s*\d+\s*m\s*\d+\s*s", window=200)

    return NodeMetrics(
        url=url,
        available=True,
        total_compute_power=clean_spaces(total) if total else None,
        validators=validators,
        next_poc=next_poc,
    )


async def parse_discord(page: Page) -> DiscordMetrics:
    ok, err = await goto_soft(page, DISCORD_URL, timeout_ms=25000)
    if not ok:
        return DiscordMetrics(error=err)

    await page.wait_for_selector("text=/в\s*сети|online/i", timeout=20000)
    await page.wait_for_timeout(2000)
    html = await page.content()
    text = clean_spaces(await page.inner_text("body"))

    combined = f"{html} {text}"
    online_match = re.search(r"(\d[\d\s\u00a0.,]*)\s*(?:в\s*сети|online)", combined, flags=re.IGNORECASE)
    members_match = re.search(r"(\d[\d\s\u00a0.,]*)\s*(?:участник\w*|members?)", combined, flags=re.IGNORECASE)

    online = clean_spaces(online_match.group(1)) if online_match else None
    members = clean_spaces(members_match.group(1)) if members_match else None

    return DiscordMetrics(online=online, members=members)


async def parse_x(page: Page) -> XMetrics:
    ok, err = await goto_soft(page, X_URL, timeout_ms=30000)
    if not ok:
        return XMetrics(error=err)

    await page.wait_for_timeout(3500)
    content = await page.content()
    text = clean_spaces(await page.inner_text("body"))
    combined = f"{text} {content}"

    followers = None
    patterns = [
        r"(\d[\d\s.,]*\s*(?:тыс\.?|k|m)?)\s*(?:читател\w+|followers?)",
        r"verified_followers[^<]{0,500}?>(\d[\d\s.,\u00a0]*\s*(?:тыс\.?|k|m)?)<",
        r"followers_count\D{0,20}(\d[\d\s.,]*)",
    ]
    for p in patterns:
        m = re.search(p, combined, flags=re.IGNORECASE)
        if m:
            followers = clean_spaces(m.group(1))
            break

    return XMetrics(followers=followers)


async def parse_github(page: Page) -> GitHubMetrics:
    ok, err = await goto_soft(page, GITHUB_URL)
    if not ok:
        return GitHubMetrics(error=err)

    await page.wait_for_timeout(1500)

    selectors = [
        'a[href$="/stargazers"] span.Counter',
        'a[href$="/stargazers"] strong',
        'a[href$="/stargazers"]',
    ]
    stars = None
    for sel in selectors:
        loc = page.locator(sel).first
        if await loc.count() > 0:
            raw = clean_spaces(await loc.inner_text())
            m = re.search(r"\d[\d\s.,kKmM]*", raw)
            if m:
                stars = clean_spaces(m.group(0))
                break

    if not stars:
        text = clean_spaces(await page.inner_text("body"))
        m = re.search(r"(\d[\d\s.,kKmM]*)\s*stars?", text, flags=re.IGNORECASE)
        if m:
            stars = clean_spaces(m.group(1))

    return GitHubMetrics(stars=stars)


async def dismiss_hex_popups(page: Page) -> None:
    candidates = [
        "button:has-text('Close')",
        "button:has-text('Закрыть')",
        "button:has-text('Понятно')",
        "button:has-text('OK')",
        "button:has-text('Принять')",
        "[aria-label='Close']",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await loc.click(timeout=1000)
        except Exception:
            pass


async def parse_hex(page: Page) -> HexMetrics:
    ok, err = await goto_soft(page, HEX_URL, timeout_ms=30000)
    if not ok:
        return HexMetrics(error=err)

    await page.wait_for_timeout(3000)
    await dismiss_hex_popups(page)
    await page.wait_for_timeout(1000)

    text = clean_spaces(await page.inner_text("body"))
    price = None

    # Prefer sell/current price markers.
    for pattern in [
        r"(?:Sell\s*Price|Цена\s*продажи)\s*[:]?\s*\$?\s*(\d+(?:[.,]\d+)*)",
        r"(?:Buy\s*Price|Цена\s*покупки)\s*[:]?\s*\$?\s*(\d+(?:[.,]\d+)*)",
        r"\$\s*(\d+(?:[.,]\d+)*)",
    ]:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            price = m.group(1).replace(" ", "")
            break

    return HexMetrics(price=price)


async def collect(headless: bool = True) -> dict:
    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(ignore_https_errors=True)

        node_metrics: list[NodeMetrics] = []
        for url in NODE_URLS:
            page = await context.new_page()
            node_metrics.append(await parse_node(page, url))
            await page.close()

        page = await context.new_page()
        discord = await parse_discord(page)
        await page.close()

        page = await context.new_page()
        x_data = await parse_x(page)
        await page.close()

        page = await context.new_page()
        github = await parse_github(page)
        await page.close()

        page = await context.new_page()
        hex_data = await parse_hex(page)
        await page.close()

        await context.close()
        await browser.close()

    return {
        "nodes": [asdict(item) for item in node_metrics],
        "discord": asdict(discord),
        "x": asdict(x_data),
        "github": asdict(github),
        "hex": asdict(hex_data),
    }


def print_report(data: dict) -> None:
    print("\n=== GONKA PARSER REPORT ===")

    print("\n[Nodes]")
    for item in data["nodes"]:
        status = "OK" if item["available"] else f"DOWN ({item['error']})"
        print(f"- {item['url']} -> {status}")
        if item["available"]:
            print(f"  Total Compute Power: {item.get('total_compute_power')}")
            print(f"  Validators: {item.get('validators')}")
            print(f"  Next PoC: {item.get('next_poc')}")

    print("\n[Discord]")
    print(f"- Online: {data['discord'].get('online')}")
    print(f"- Members: {data['discord'].get('members')}")
    if data["discord"].get("error"):
        print(f"- Error: {data['discord']['error']}")

    print("\n[X / Twitter]")
    print(f"- Followers: {data['x'].get('followers')}")
    if data["x"].get("error"):
        print(f"- Error: {data['x']['error']}")

    print("\n[GitHub]")
    print(f"- Stars: {data['github'].get('stars')}")
    if data["github"].get("error"):
        print(f"- Error: {data['github']['error']}")

    print("\n[HEX Exchange]")
    print(f"- Current price: {data['hex'].get('price')}")
    if data["hex"].get("error"):
        print(f"- Error: {data['hex']['error']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gonka public metrics parser")
    parser.add_argument("--show-browser", action="store_true", help="Run with visible browser window")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data = asyncio.run(collect(headless=not args.show_browser))
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print_report(data)


if __name__ == "__main__":
    main()
