"""One-command Graph API setup helper — the "do it for me" script.

You do the clicky parts in the browser (create Page, link Instagram,
create app, generate a short token), then run THIS script and paste 3
values when asked. It performs every URL exchange, picks the right Page,
validates the result with a real business_discovery call, and prints the
two env vars ready for Render.

    python graph_setup.py            # interactive, prints the env vars
    python graph_setup.py --save     # ALSO writes them into backend/.env

Requires: requests (already in backend requirements via httpx — this
script uses plain stdlib urllib so it runs even without a venv).
"""
import json
import sys
import urllib.parse
import urllib.request

API = "https://graph.facebook.com/v21.0"


def _get(path, params):
    url = f"{API}/{path}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"error": {"message": f"HTTP {e.code}"}}
    except Exception as e:            return {"error": {"message": f"network: {e}"}}


def _fail(stage, msg, fix):
    print(f"\n[FAIL] {stage}: {msg}")
    print(f"\n>>> FIX: {fix}")
    sys.exit(1)


def main():
    save = "--save" in sys.argv
    print("=" * 64)
    print(" InstaIQ - Graph API setup helper")
    print(" Paste the 3 values from your Meta app when asked.")
    print("=" * 64)

    app_id = input("\n1) APP_ID (App settings > Basic): ").strip()
    app_secret = input("2) APP_SECRET (App settings > Basic > Show): ").strip()
    short_token = input("3) SHORT_TOKEN (Graph API Explorer, YOUR app selected): ").strip()

    if not (app_id and app_secret and short_token):
        _fail("input", "one of the values is empty",
              "Re-run and paste all three values completely.")

    # --- Step A: short -> long-lived user token -------------------------
    print("\n[A] Exchanging short token for a long-lived token...")
    r = _get("oauth/access_token", {
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": short_token,
    })
    if r.get("error"):
        code = r["error"].get("code")
        _fail("token exchange", r["error"].get("message", "")[:200],
              "Regenerate the short token in Graph API Explorer with YOUR "
              "app selected (not 'Meta Graph API Explorer') and all 4 "
              "permissions, then re-run this script within the hour."
              if code in (190, 15, 102) else str(r["error"])[:300])
    long_token = r["access_token"]
    print(f"    [OK] long-lived token received ({len(long_token)} chars)")

    # --- Step B: list Pages + their IG accounts -------------------------
    print("\n[B] Reading your Pages (this is where the IG link shows up)...")
    r = _get("me/accounts", {
        "fields": "id,name,access_token,instagram_business_account{id,username,followers_count,media_count}",
        "limit": 50,
        "access_token": long_token,
    })
    if r.get("error"):
        _fail("me/accounts", r["error"].get("message", "")[:200],
              "The long-lived token was rejected - regenerate and retry.")
    pages = r.get("data") or []
    if not pages:
        _fail("me/accounts", "token sees NO Pages (empty data:[])",
              "In Graph API Explorer (YOUR app selected) re-generate the "
              "token with pages_show_list + pages_read_engagement, and "
              "when the dialog asks for Pages, TICK your Page. Then re-run.")
    print(f"    [OK] {len(pages)} Page(s) visible")

    # --- Step C: pick the Page with a linked Instagram account ----------
    with_ig = [p for p in pages if p.get("instagram_business_account")]
    if not with_ig:
        print("\n    Your Page(s) have NO linked Instagram account:")
        for p in pages:
            print(f"      - {p['name']} (id {p['id']})")
        _fail("instagram link",
              "no Page has instagram_business_account",
              "Instagram app > Settings > Business tools and controls > "
              "Connected Facebook Page > connect the Page above. Wait 5-15 "
              "minutes for Meta to propagate, then RE-RUN this script.")
    page = with_ig[0] if len(with_ig) == 1 else None
    if page is None:
        print("\n    Multiple Pages have linked Instagram accounts:")
        for i, p in enumerate(with_ig, 1):
            iga = p["instagram_business_account"]
            print(f"      {i}) {p['name']} -> @{iga.get('username', '?')} "
                  f"({iga.get('followers_count', '?')} followers)")
        pick = input("    Which one? [1]: ").strip() or "1"
        try:
            page = with_ig[int(pick) - 1]
        except (ValueError, IndexError):
            _fail("selection", "invalid pick", "Re-run and type a listed number.")
    iga = page["instagram_business_account"]
    page_token = page["access_token"]
    ig_id = str(iga["id"])
    username = iga.get("username") or ""
    print(f"    [OK] Page: {page['name']}")
    print(f"    [OK] Instagram: @{username} (id {ig_id}, "
          f"{iga.get('followers_count', '?')} followers)")

    # --- Step D: validate with the EXACT production call shape ----------
    print("\n[C] Validating with a real business_discovery call...")
    fields = f"business_discovery.username({username}){{biography,followers_count,media_count}}"
    r = _get(ig_id, {"fields": fields, "access_token": page_token})
    if r.get("error"):
        _fail("business_discovery", r["error"].get("message", "")[:200],
              "Usually means the account is not Business/Creator. Instagram "
              "app > Settings > Account type and tools > confirm 'Professional'.")
    bd = r.get("business_discovery") or {}
    if not bd:
        _fail("business_discovery", "empty response",
              "Confirm the account is a professional (Business/Creator) account.")
    print(f"    [OK] reads own account: {bd.get('followers_count')} followers, "
          f"{bd.get('media_count')} posts")
    print("    [OK] SAME call shape works for any business account: nike, natgeo...")

    # --- Step E: print / optionally save the env vars -------------------
    print("\n" + "=" * 64)
    print(" SUCCESS. Paste these TWO variables in Render:")
    print(" (dashboard.render.com > instaiq > Environment > Add)")
    print("=" * 64)
    print(f"\n  IG_ACCESS_TOKEN = {page_token}\n")
    print(f"  IG_BUSINESS_ACCOUNT_ID = {ig_id}\n")
    print("=" * 64)
    print(" Then: Save > wait for the deploy to show 'Live' > open")
    print(" https://instaiq-1mka.onrender.com/api/graph-status")
    print(" It must print \"verdict\": \"ready\"")
    print("=" * 64)

    if save:
        env_path = "backend/.env"
        try:
            lines = open(env_path, encoding="utf-8").read().splitlines()
        except FileNotFoundError:
            lines = []
        wanted = {"IG_ACCESS_TOKEN": page_token, "IG_BUSINESS_ACCOUNT_ID": ig_id}
        seen = set()
        for i, line in enumerate(lines):
            key = line.split("=", 1)[0].strip()
            if key in wanted:
                lines[i] = f"{key}={wanted[key]}"
                seen.add(key)
        for key, val in wanted.items():
            if key not in seen:
                lines.append(f"{key}={val}")
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"\n[OK] also written to {env_path} (local only - it is gitignored)")


if __name__ == "__main__":
    main()
