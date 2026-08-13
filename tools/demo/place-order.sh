#!/usr/bin/env bash
# The one-command lifecycle proof (make demo): place an order as the seeded
# demo customer, drive the kitchen as the owner, and watch the saga walk
# PLACED → CONFIRMED → ACCEPTED → PREPARING → READY → PICKED_UP → DELIVERED
# → SETTLED through the real stack.
#
#   make demo                        # happy path to SETTLED
#   CARD_TOKEN=tok_decline make demo # sad path: declined → CANCELLED, stock restored
#
# Needs: the m2 stack up (make up-m2) and seeded data (make seed).
set -euo pipefail

GATEWAY="${GATEWAY_URL:-http://localhost:8080}"
CARD_TOKEN="${CARD_TOKEN:-tok_ok}"
PASSWORD="demo1234demo"
CUSTOMER="customer@demo.smartfood.dev"

json() { python3 -c "import sys, json; d = json.load(sys.stdin); print(d$1)"; }

idem_key() { uuidgen 2>/dev/null || python3 -c "import uuid; print(uuid.uuid4())"; }

say() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }

LAST_STATUS=""
poll_status() { # poll_status <token> <order_id> <want> <max_seconds>
  LAST_STATUS=""
  for _ in $(seq 1 "$4"); do
    LAST_STATUS=$(curl -s "$GATEWAY/v1/orders/$2" -H "Authorization: Bearer $1" | json "['status']")
    if [ "$LAST_STATUS" = "$3" ]; then echo "   status = $LAST_STATUS"; return 0; fi
    case "$LAST_STATUS" in CANCELLED|REFUNDED) echo "   status = $LAST_STATUS"; return 1 ;; esac
    sleep 1
  done
  echo "   TIMEOUT waiting for $3 (last = $LAST_STATUS)"; return 1
}

say "sign in (demo customer + restaurant owner)"
CTOK=$(curl -s -X POST "$GATEWAY/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$CUSTOMER\",\"password\":\"$PASSWORD\"}" | json "['access_token']")
OTOK=$(curl -s -X POST "$GATEWAY/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"owner-springfield-biryani-house@demo.smartfood.dev\",\"password\":\"$PASSWORD\"}" \
  | json "['access_token']")
ADDRESS_ID=$(curl -s "$GATEWAY/v1/me/addresses" -H "Authorization: Bearer $CTOK" | json "[0]['id']")

say "pick the owner's restaurant + a modifier-free item"
RESTAURANT_ID=$(curl -s "$GATEWAY/v1/restaurants?city=springfield" | python3 -c "
import sys, json
for r in json.load(sys.stdin)['restaurants']:
    if r['name'] == 'Biryani House':
        print(r['id']); raise SystemExit
raise SystemExit('Biryani House not found — run make seed first')
")
MENU=$(curl -s "$GATEWAY/v1/menus/$RESTAURANT_ID")
MENU_VERSION=$(echo "$MENU" | json "['version']")
ITEM_ID=$(echo "$MENU" | python3 -c "
import sys, json
menu = json.load(sys.stdin)
for category in menu['categories']:
    for item in category['items']:
        if item.get('available', True) and not any(
            group.get('min_select', 0) > 0 for group in item.get('modifier_groups', [])
        ):
            print(item['id']); raise SystemExit
raise SystemExit('no modifier-free item — reseed?')
")
echo "   restaurant=$RESTAURANT_ID item=$ITEM_ID menu_version=$MENU_VERSION"

say "quote (the server is the only pricer)"
curl -s -X POST "$GATEWAY/v1/quote" -H "Authorization: Bearer $CTOK" \
  -H 'Content-Type: application/json' \
  -d "{\"restaurant_id\":\"$RESTAURANT_ID\",\"lines\":[{\"item_id\":\"$ITEM_ID\",\"qty\":2}]}" \
  | json "['totals']['total_cents']" | sed 's/^/   total_cents = /'

say "place (card: $CARD_TOKEN, Idempotency-Key minted fresh)"
ORDER_ID=$(curl -s -X POST "$GATEWAY/v1/orders" -H "Authorization: Bearer $CTOK" \
  -H 'Content-Type: application/json' -H "Idempotency-Key: $(idem_key)" \
  -d "{\"restaurant_id\":\"$RESTAURANT_ID\",\"menu_version\":$MENU_VERSION,\"address_id\":\"$ADDRESS_ID\",\"card_token\":\"$CARD_TOKEN\",\"lines\":[{\"item_id\":\"$ITEM_ID\",\"qty\":2}]}" \
  | json "['order_id']")
echo "   order = $ORDER_ID"

say "saga: reserve stock, authorize card → CONFIRMED"
if ! poll_status "$CTOK" "$ORDER_ID" CONFIRMED 20; then
  if [ "$LAST_STATUS" != "CANCELLED" ]; then
    say "FAILED: order never reached CONFIRMED (stuck at ${LAST_STATUS:-unknown}) — is the worker up?"
    exit 1
  fi
  REASON=$(curl -s "$GATEWAY/v1/orders/$ORDER_ID" -H "Authorization: Bearer $CTOK" \
    | json "['cancel_reason']")
  say "sad path complete: order cancelled (reason: $REASON) — stock and hold released"
  exit 0
fi

say "kitchen: accept → preparing → ready (as the owner)"
curl -s -o /dev/null -w '   accept    → %{http_code}\n' -X POST \
  "$GATEWAY/v1/restaurant/orders/$ORDER_ID/accept" -H "Authorization: Bearer $OTOK"
poll_status "$CTOK" "$ORDER_ID" ACCEPTED 15
curl -s -o /dev/null -w '   preparing → %{http_code}\n' -X POST \
  "$GATEWAY/v1/restaurant/orders/$ORDER_ID/preparing" -H "Authorization: Bearer $OTOK"
curl -s -o /dev/null -w '   ready     → %{http_code}\n' -X POST \
  "$GATEWAY/v1/restaurant/orders/$ORDER_ID/ready" -H "Authorization: Bearer $OTOK"

say "simulated courier drives, money captures, order settles"
poll_status "$CTOK" "$ORDER_ID" SETTLED 90

say "done — inspect the trail"
echo "   temporal ui : http://localhost:8233  (workflow ord::$ORDER_ID)"
echo "   kafka ui    : http://localhost:8085  (topic c1.orders.events)"
echo "   this order  : $GATEWAY/v1/orders/$ORDER_ID"
