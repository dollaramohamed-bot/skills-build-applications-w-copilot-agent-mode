"""Live public sourcing checks. Uses no mock events or customer information."""
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from economics import assess
from screening import screen

CATALOG = "https://royalcraftwood.com/collections/storage-organization"
TRENDS = "https://trends.google.com/trending/rss?geo=US"
ALLOWED = {"royalcraftwood.com", "www.royalcraftwood.com", "trends.google.com", "products.fourthwall.com"}
FULFILLMENT_CATALOG = "https://products.fourthwall.com/"
LIMIT = 4_000_000


def fetch(url):
    if urllib.parse.urlparse(url).hostname not in ALLOWED:
        raise ValueError("Unapproved source")
    request = urllib.request.Request(url, headers={"User-Agent": "CommerceAutopilot/1.0 (public catalog research)"})
    with urllib.request.urlopen(request, timeout=15) as response:
        if urllib.parse.urlparse(response.url).hostname not in ALLOWED:
            raise ValueError("Unexpected redirect")
        data = response.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError("Source exceeds size limit")
        return data.decode("utf-8", errors="replace")


def inspect(url):
    try:
        text = fetch(url)
        if re.search(r"verify you are human|checking your browser|automated traffic", text, re.I):
            return {"url": url, "status": "blocked"}
        # Only retain links and the digest. Never execute page scripts or instructions.
        links = sorted(set(urllib.parse.urljoin(url, link) for link in
            re.findall(r'href=["\x27]([^"\x27]+)', text)))
        products = [link.split("?")[0] for link in links
                    if urllib.parse.urlparse(link).hostname in ALLOWED
                    and ("/products/" in link or
                         (urllib.parse.urlparse(link).hostname == "products.fourthwall.com"
                          and re.fullmatch(r"/[a-z0-9-]{8,}/?", urllib.parse.urlparse(link).path)))]
        return {"url": url, "status": "fetched", "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "product_urls": sorted(set(products))[:96], "_body": text}
    except Exception as error:
        return {"url": url, "status": "failed", "error_type": type(error).__name__}


def main():
    now = dt.datetime.now(dt.timezone.utc)
    directory = pathlib.Path(__file__).parent / "status"
    directory.mkdir(exist_ok=True)
    previous_path = directory / "latest.json"
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
    cursor = previous.get("scan_cursor", 0)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        sources = list(pool.map(inspect, [CATALOG, TRENDS, FULFILLMENT_CATALOG]))
    trends = []
    trend_source = sources[1]
    if trend_source["status"] == "fetched":
        try:
            root = ET.fromstring(trend_source["_body"])
            trends = [{"query": item.findtext("title"), "published": item.findtext("pubDate")}
                      for item in root.findall("./channel/item")[:20]]
        except ET.ParseError:
            trend_source["status"] = "invalid_feed"
    candidates = [{"url": url, "decision": "hold",
                   "missing": ["exact SKU quote", "US inventory", "delivery commitment",
                               "returns terms", "authorized media", "purchase-intent evidence",
                               "funding before payout"]}
                  for source in (sources[2], sources[0])
                  for url in source.get("product_urls", [])]
    previous_rows = {row["url"]: row for row in previous.get("candidates", [])}
    for row in candidates:
        prior = previous_rows.get(row["url"], {})
        try:
            observed = dt.datetime.fromisoformat(prior.get("source_observed_at", ""))
            fresh = dt.timedelta(0) <= now - observed < dt.timedelta(hours=48)
        except (ValueError, TypeError):
            fresh = False
        if fresh:
            for field in ("public_product_data", "page_sha256", "source_observed_at"):
                if field in prior:
                    row[field] = prior[field]
    for source in sources:
        source.pop("_body", None)
    selected = []
    for source in (sources[2], sources[0]):
        urls = source.get("product_urls", [])
        if urls:
            start = cursor % len(urls)
            rotated = urls[start:] + urls[:start]
            selected.extend(row for url in rotated[:8] for row in candidates if row["url"] == url)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        details = list(pool.map(inspect, [row["url"] for row in selected]))
    for row, detail in zip(selected, details):
        text = detail.pop("_body", "")
        row["page_status"] = detail["status"]
        if detail["status"] != "fetched":
            continue
        row["source_observed_at"] = now.isoformat()
        row["page_sha256"] = detail.get("sha256")
        row["public_product_data"] = []
        for block in re.findall(r'<script[^>]+type=["\x27]application/ld\+json["\x27][^>]*>(.*?)</script>', text, re.S | re.I):
            try:
                value = json.loads(block)
                nodes = value if isinstance(value, list) else value.get("@graph", [value])
                for node in nodes:
                    if isinstance(node, dict) and node.get("@type") == "Product":
                        row["public_product_data"].append({
                            "name": node.get("name"), "sku": node.get("sku"),
                            "catalog_offers": node.get("offers"),
                            "cost_basis": ("Supplier catalog starting costs; exact variant quote unverified"
                                           if "products.fourthwall.com" in row["url"] else
                                           "Retail page only; wholesale and freight unverified")})
            except (ValueError, TypeError, AttributeError):
                continue
    for row in candidates:
        row["profit_gate"] = assess(row, now)
        row["cost_screen"] = screen(row)
    result = {
        "objective": {"rolling_30_day_operating_profit_cents": 1_000_000,
                      "product_categories": "unrestricted", "available_spend_cents": 0},
        "observed_at": now.isoformat(), "run_id": os.getenv("GITHUB_RUN_ID"),
        "mode": "live_public_research", "sources": sources, "scan_cursor": cursor + 8,
        "market_attention_queries": trends,
        "market_caveat": "Trending searches measure attention, not purchase intent or product demand.",
        "candidates": candidates,
        "quote_priorities": sorted(
            [{"url": row["url"],
              "required_buyer_payment_for_25_contribution_floor_cents":
                  row["cost_screen"]["offers"][0]["required_buyer_payment_for_contribution_cents"]["2500"],
              "observed_at": row.get("source_observed_at")}
             for row in candidates if row["cost_screen"]["status"] == "catalog_floor_only"],
            key=lambda item: item["required_buyer_payment_for_25_contribution_floor_cents"]),
        "quote_priority_caveat": "Cost screening only. Demand and exact quote still required.",
        "operating_status": "sourcing_only",
        "blockers": ["Shopify connected store rejects API access",
                     "No authenticated supplier fulfillment connection",
                     "No verified landed costs or funding available"],
        "worker_spend_cents": 0, "actual_orders": None, "actual_profit_cents": None,
        "ledger_status": "No authenticated order ledger connected",
        "next_schedule_utc": "17 minutes past every sixth hour",
    }
    directory = pathlib.Path(__file__).parent / "status"
    directory.mkdir(exist_ok=True)
    target = directory / "latest.json"
    temporary = directory / "latest.tmp"
    temporary.write_text(json.dumps(result, indent=2) + "\n")
    temporary.replace(target)
    history = directory / "history.json"
    prior = json.loads(history.read_text()) if history.exists() else []
    identifier = result["run_id"] or now.isoformat()
    prior = [row for row in prior if row["id"] != identifier]
    prior.append({"id": identifier, "observed_at": result["observed_at"],
                  "fetched_sources": sum(s["status"] == "fetched" for s in sources),
                  "candidate_count": len(candidates)})
    history.write_text(json.dumps(prior[-120:], indent=2) + "\n")
    print(json.dumps({"sources": [{k: v for k, v in s.items() if k != "product_urls"} for s in sources],
                      "candidate_count": len(candidates), "attention_queries": len(trends)}))
    if not any(source["status"] == "fetched" for source in sources):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
