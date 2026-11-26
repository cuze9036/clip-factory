"""One step to connect Instagram + Facebook: the owner pastes ONE token from Meta's Graph API Explorer and this
finds the Page, its Instagram account and a Page token that doesn't expire, then saves all four keys in .env.

Run:  powershell -ExecutionPolicy Bypass -File windows\\meta-login.ps1
Nothing is printed that contains a token; tokens go in headers or form bodies, never in a URL.
"""
import getpass
import os

import requests

from . import config

NEEDED = ["instagram_basic", "instagram_content_publish", "instagram_manage_insights", "pages_show_list",
          "pages_read_engagement", "pages_manage_posts", "read_insights", "publish_video", "business_management"]
# posting needs the first five; reading views (daily study, flops) needs the two insights ones
MUST = {"instagram_basic", "instagram_content_publish", "pages_show_list", "pages_read_engagement",
        "pages_manage_posts", "instagram_manage_insights", "read_insights"}


def _graph(path):
    return f"https://graph.facebook.com/{config.get().get('graph_api_version', 'v21.0')}/{path}"


def _get(path, token, **params):
    r = requests.get(_graph(path), params=params, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    data = r.json() if r.content else {}
    if r.status_code >= 400 or "error" in data:
        raise RuntimeError((data.get("error") or {}).get("message", f"HTTP {r.status_code}")[:200])
    return data


def token_info(token):
    """{app_id, scopes, expires_at (0 = never)} of a user token (Meta lets a token describe itself)."""
    r = requests.get(_graph("debug_token"), params={"input_token": token, "access_token": token}, timeout=30)
    data = (r.json() if r.content else {}).get("data") or {}
    if not data.get("is_valid"):
        raise RuntimeError("Meta says this token isn't valid. Make a new one (step 3) and paste it right away.")
    return {"app_id": str(data.get("app_id") or ""), "scopes": set(data.get("scopes") or []),
            "expires_at": int(data.get("expires_at") or 0)}


def long_lived(token, app_id, secret):
    """A short-lived (1-2 h) user token → a 60-day one, whose Page tokens never expire."""
    r = requests.post(_graph("oauth/access_token"), data={"grant_type": "fb_exchange_token", "client_id": app_id,
                                                          "client_secret": secret, "fb_exchange_token": token},
                      timeout=30)
    data = r.json() if r.content else {}
    if "access_token" not in data:
        raise RuntimeError("Meta didn't accept the App Secret: " +
                           str((data.get("error") or {}).get("message", r.status_code))[:150])
    return data["access_token"]


def pages(user_token):
    data = _get("me/accounts", user_token, fields="id,name,access_token,instagram_business_account{id,username}",
                limit=50)
    return data.get("data") or []


def pick(found, choose=input):
    with_ig = [p for p in found if p.get("instagram_business_account")]
    options = with_ig or found
    if len(options) == 1:
        return options[0]
    for i, p in enumerate(options, 1):
        ig = (p.get("instagram_business_account") or {}).get("username")
        print(f"  {i}. {p['name']}" + (f"  (Instagram @{ig})" if ig else ""))
    while True:
        n = choose("Which one is yours for clips? Type its number: ").strip()
        if n.isdigit() and 1 <= int(n) <= len(options):
            return options[int(n) - 1]


def save_env(values, path=None):
    """Replaces (or adds) NAME=value lines in .env, keeping everything else."""
    path = path or (config.ROOT / ".env")
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.split("=", 1)[0].strip() not in values]
    lines += [f"{k}={v}" for k, v in values.items()]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    for k, v in values.items():
        os.environ[k] = v


def connect(user_token, secret=None, ask=getpass.getpass, choose=input, env_path=None):
    """Does everything after the paste. Returns lines to print."""
    acc = config.get().get("accounts") or {}
    ig, fb = acc["instagram"]["main"], acc["facebook"]["main"]
    info = token_info(user_token)
    missing = sorted(MUST - info["scopes"])
    if missing:
        raise RuntimeError("The token is missing these permissions: " + ", ".join(missing) +
                           ". Tick them in Graph API Explorer, press Generate Access Token again, and paste the new one.")
    saved = {}
    if info["expires_at"]:   # a short-lived token: make it long-lived, so the Page token never expires
        secret = secret or config.env("META_APP_SECRET")
        if not secret:
            secret = ask("Paste the App Secret (developers.facebook.com > your app > App settings > Basic > "
                         "App secret > Show). It stays hidden: ").strip()
        user_token = long_lived(user_token, info["app_id"], secret)
        saved["META_APP_SECRET"] = secret
    found = pages(user_token)
    if not found:
        raise RuntimeError("Meta shows no Facebook Page for this login. In Graph API Explorer, when Facebook asks "
                           "which Pages to allow, tick your Page (and its Instagram account), then try again.")
    page = pick(found, choose)
    saved[fb["page_id_env"]] = str(page["id"])
    saved[fb["token_env"]] = page["access_token"]
    lines = [f"Facebook Page: {page['name']}"]
    igacc = page.get("instagram_business_account")
    if igacc:
        saved[ig["user_id_env"]] = str(igacc["id"])
        try:   # the Page token works for its Instagram account and never expires; else the 60-day user token
            _get(str(igacc["id"]), page["access_token"], fields="username")
            saved[ig["token_env"]] = page["access_token"]
        except RuntimeError:
            saved[ig["token_env"]] = user_token
        lines.append(f"Instagram: @{igacc.get('username', igacc['id'])}")
    else:
        lines.append("This Page has no Instagram account linked: in the Instagram app, Settings > Account type and "
                     "tools > switch to Professional, then link it to this Page and run this again.")
    save_env(saved, env_path)
    from . import db
    db.x("DELETE FROM kv WHERE k LIKE 'meta_ok:%'")   # the auto-fix asks Meta again right away
    from . import selftest
    for p in ("instagram", "facebook"):
        lines.append(selftest._check(p, selftest._meta(p)))
    return lines


def main():
    print("Connect Instagram + Facebook (one time).\n"
          "1. Open https://developers.facebook.com/tools/explorer\n"
          "2. Right side: pick your app; 'User or Page' = Get User Access Token; add these permissions:\n   "
          + ", ".join(NEEDED) + "\n"
          "3. Press Generate Access Token, allow everything (tick your Page and Instagram), then copy the token.\n")
    token = getpass.getpass("Paste the token here (it stays hidden), then press Enter: ").strip().strip("\"'")
    if "EAA" in token and not token.startswith("EAA"):
        token = token[token.index("EAA"):]
    if not token:
        print("Nothing pasted, nothing changed.")
        return 1
    try:
        for line in connect(token):
            print(line)
    except RuntimeError as e:
        print(f"Not connected: {e}")
        return 1
    print("\nSaved. Clip Factory restarts by itself in about 10 seconds and then posts Instagram + Facebook itself.")
    return 0
