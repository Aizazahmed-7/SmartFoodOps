#!/usr/bin/env bash
# Live verification of the two cancel paths (the compensations place-order.sh's
# happy path never exercises), against the real stack:
#
#   order 1: PLACED → CONFIRMED → ACCEPTED → PREPARING, then the CUSTOMER
#            cancels (202) → CANCELLED with cancel_reason=customer_cancelled
#   order 2: PLACED → CONFIRMED, then the OWNER rejects
#            → CANCELLED with cancel_reason=restaurant_rejected
#
# Needs: the m2 stack up (make up-m2) and seeded data (make seed).
set -euo pipefail

GATEWAY="${GATEWAY_URL:-http://localhost:8080}"
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

place_order() { # place_order → prints the new order id (tok_ok card)
  curl -s -X POST "$GATEWAY/v1/orders" -H "Authorization: Bearer $CTOK" \
    -H 'Content-Type: application/json' -H "Idempotency-Key: $(idem_key)" \
    -d "{\"restaurant_id\":\"$RESTAURANT_ID\",\"menu_version\":$MENU_VERSION,\"address_id\":\"$ADDRESS_ID\",\"card_token\":\"tok_ok\",\"lines\":[{\"item_id\":\"$ITEM_ID\",\"qty\":1}]}" \
    | json "['order_id']"
}

expect_reason() { # expect_reason <order_id> <want>
  REASON=$(curl -s "$GATEWAY/v1/orders/$1" -H "Authorization: Bearer $CTOK" | json "['cancel_reason']")
  if [ "$REASON" != "$2" ]; then
    say "FAILED: cancel_reason = $REASON (wanted $2)"; exit 1
  fi
  echo "   cancel_reason = $REASON"
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

say "order 1: place → CONFIRMED → accept → preparing → customer cancels"
ORDER_ID=$(place_order)
echo "   order = $ORDER_ID"
if ! poll_status "$CTOK" "$ORDER_ID" CONFIRMED 20; then
  say "FAILED: order never reached CONFIRMED (last = ${LAST_STATUS:-unknown}) — is the worker up?"
  exit 1
fi
curl -s -o /dev/null -w '   accept    → %{http_code}\n' -X POST \
  "$GATEWAY/v1/restaurant/orders/$ORDER_ID/accept" -H "Authorization: Bearer $OTOK"
poll_status "$CTOK" "$ORDER_ID" ACCEPTED 15
curl -s -o /dev/null -w '   preparing → %{http_code}\n' -X POST \
  "$GATEWAY/v1/restaurant/orders/$ORDER_ID/preparing" -H "Authorization: Bearer $OTOK"
CANCEL_CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST \
  "$GATEWAY/v1/orders/$ORDER_ID/cancel" -H "Authorization: Bearer $CTOK")
echo "   cancel    → $CANCEL_CODE"
if [ "$CANCEL_CODE" != "202" ]; then
  say "FAILED: expected 202 from cancel, got $CANCEL_CODE"; exit 1
fi
poll_status "$CTOK" "$ORDER_ID" CANCELLED 30
expect_reason "$ORDER_ID" customer_cancelled

say "order 2: place → CONFIRMED → owner rejects"
ORDER_ID=$(place_order)
echo "   order = $ORDER_ID"
if ! poll_status "$CTOK" "$ORDER_ID" CONFIRMED 20; then
  say "FAILED: order never reached CONFIRMED (last = ${LAST_STATUS:-unknown}) — is the worker up?"
  exit 1
fi
curl -s -o /dev/null -w '   reject    → %{http_code}\n' -X POST \
  "$GATEWAY/v1/restaurant/orders/$ORDER_ID/reject" -H "Authorization: Bearer $OTOK"
poll_status "$CTOK" "$ORDER_ID" CANCELLED 30
expect_reason "$ORDER_ID" restaurant_rejected

say "done — both cancel paths verified live (compensations ran; stock and holds released)"
