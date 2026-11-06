"""Normalize observed supplier catalog floors without inventing a sellable quote."""
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import math

PAYMENT_RATE = Decimal("0.029")
PAYMENT_FIXED_CENTS = 30
RESERVE_RATE = Decimal("0.05")  # Planning assumption, not observed return performance.


def cents(value):
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            return None
        return int((number * 100).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError, TypeError):
        return None


def screen(row):
    if not row["url"].startswith("https://products.fourthwall.com/"):
        return {"status": "requires_wholesale_quote"}
    floors = []
    for product in row.get("public_product_data", []):
        offers = product.get("catalog_offers")
        offers = offers if isinstance(offers, list) else [offers]
        for offer in offers:
            if not isinstance(offer, dict) or offer.get("priceCurrency") != "USD":
                continue
            base = cents(offer.get("price"))
            if base is None:
                continue
            shipping_details = offer.get("shippingDetails", [])
            shipping_details = shipping_details if isinstance(shipping_details, list) else [shipping_details]
            for shipping in shipping_details:
                if not isinstance(shipping, dict):
                    continue
                destination = shipping.get("shippingDestination", {})
                rate = shipping.get("shippingRate", {})
                if not isinstance(destination, dict) or not isinstance(rate, dict):
                    continue
                if destination.get("addressCountry") != "US" or rate.get("currency") != "USD":
                    continue
                freight = cents(rate.get("value"))
                if freight is None:
                    continue
                landed_floor = base + freight
                # Required buyer payment if seller absorbs the observed shipping floor.
                denominator = Decimal(1) - PAYMENT_RATE - RESERVE_RATE
                thresholds = {}
                for target in (1500, 2500, 4000):
                    price = int((Decimal(landed_floor + PAYMENT_FIXED_CENTS + target + 2)
                                 / denominator).to_integral_value(rounding=ROUND_CEILING))
                    thresholds[str(target)] = price
                floors.append({
                    "sku": product.get("sku"), "name": product.get("name"),
                    "offer_name": offer.get("name"),
                    "catalog_base_floor_cents": base,
                    "us_shipping_floor_cents": freight,
                    "landed_floor_cents": landed_floor,
                    "required_buyer_payment_for_contribution_cents": thresholds,
                    "transit_time": shipping.get("deliveryTime"),
                    "availability": offer.get("availability"),
                })
    if not floors:
        return {"status": "missing_us_cost_or_shipping"}
    floors.sort(key=lambda offer: offer["landed_floor_cents"])
    return {"status": "catalog_floor_only", "offers": floors,
            "assumptions": {"us_card_rate": str(PAYMENT_RATE), "fixed_fee_cents": PAYMENT_FIXED_CENTS,
                            "planning_reserve_rate": str(RESERVE_RATE), "ads_cents": 0},
            "limits": ["Not an exact size, decoration or address quote",
                       "Required selling price has no validated conversion evidence",
                       "Taxes, fixed overhead and exceptional returns may change contribution"],
            "eligible_for_checkout": False}


def compare_posted_price(cost_screen, reference):
    prices = []
    for product in reference.get("products", []):
        offers = product.get("catalog_offers")
        offers = offers if isinstance(offers, list) else [offers]
        for offer in offers:
            if isinstance(offer, dict) and offer.get("priceCurrency") == "USD":
                price = cents(offer.get("price"))
                if price is not None and offer.get("availability") in ("https://schema.org/InStock", "http://schema.org/InStock"):
                    prices.append(price)
    if not prices or cost_screen.get("status") != "catalog_floor_only":
        return {"status": "insufficient_current_evidence"}
    price = min(prices)
    supplier = cost_screen["offers"][0]
    processing = int((Decimal(price) * PAYMENT_RATE).to_integral_value(rounding=ROUND_CEILING)) + PAYMENT_FIXED_CENTS
    reserve = int((Decimal(price) * RESERVE_RATE).to_integral_value(rounding=ROUND_CEILING))
    buyer_shipping_ceiling = price - supplier["catalog_base_floor_cents"] - processing - reserve
    seller_shipping_ceiling = price - supplier["landed_floor_cents"] - processing - reserve
    return {
        "status": "posted_price_comparison_only",
        "reference_url": reference["url"], "observed_at": reference["observed_at"],
        "reference_minimum_price_cents": price,
        "contribution_ceiling_if_buyer_pays_shipping_cents": buyer_shipping_ceiling,
        "contribution_ceiling_if_seller_pays_shipping_cents": seller_shipping_ceiling,
        "minimum_orders_for_10000_before_overhead_if_buyer_pays_shipping":
            math.ceil(1_000_000 / buyer_shipping_ceiling) if buyer_shipping_ceiling > 0 else None,
        "free_shipping_at_reference_price": "nonpositive_even_at_catalog_floor"
            if seller_shipping_ceiling <= 0 else "requires_exact_quote",
        "limits": [
            "Independent seller art and audience differ",
            "Reference minimum price and supplier minimum cost may represent different variants",
            "Buyer-paid shipping case excludes processing charged on freight and tax",
            "Catalog cost is a floor, so contribution is an upper bound",
            "No demand, conversion rate or realized profit is established"],
        "eligible_for_checkout": False,
    }
