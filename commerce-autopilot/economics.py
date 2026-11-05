"""Evidence-based unit economics. All money is integer USD cents."""
import datetime as dt
import math

REQUIRED = ("price_cents", "supplier_cost_cents", "seller_shipping_cents",
            "payment_fee_cents", "return_reserve_cents", "ad_cost_cents",
            "other_cost_cents", "cash_required_cents")


def assess(candidate, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    missing = [field for field in REQUIRED
               if type(candidate.get(field)) is not int or candidate[field] < 0]
    missing += [field for field in ("quote_source", "quote_expires_at",
                                   "demand_source", "fulfillment_source", "media_rights_source")
                if not candidate.get(field)]
    if missing:
        return {"decision": "hold", "missing": missing, "profit_per_order_cents": None}
    try:
        expires = dt.datetime.fromisoformat(candidate["quote_expires_at"].replace("Z", "+00:00"))
        if expires.tzinfo is None or expires <= now:
            return {"decision": "hold", "reason": "Quote expired or lacks timezone"}
    except (TypeError, ValueError, AttributeError):
        return {"decision": "hold", "reason": "Invalid quote expiry"}
    costs = sum(candidate[key] for key in REQUIRED
                if key not in ("price_cents", "cash_required_cents"))
    contribution = candidate["price_cents"] - costs
    if contribution <= 0:
        return {"decision": "reject", "reason": "Non-positive contribution",
                "profit_per_order_cents": contribution}
    # No order funding is currently allocated. Buyer-funded fulfillment is eligible.
    if candidate["cash_required_cents"] > 0:
        return {"decision": "hold", "reason": "Requires cash before buyer payout",
                "profit_per_order_cents": contribution}
    if not candidate.get("checkout_verified") or not candidate.get("supplier_acceptance_verified"):
        return {"decision": "hold", "reason": "Checkout or supplier acceptance unverified",
                "profit_per_order_cents": contribution}
    return {"decision": "bounded_organic_test",
            "profit_per_order_cents": contribution,
            "orders_for_10000_profit_before_fixed_overhead": math.ceil(1_000_000 / contribution)}
