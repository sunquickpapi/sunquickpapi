"""Builds level-up.svg and the "Recently shipped" list in README.md from live GitHub data.

Runs daily in GitHub Actions (.github/workflows/level-up.yml). Needs GITHUB_TOKEN and GH_USER.
Run locally with `python scripts/level_up.py --demo` to render with sample data.
"""
import base64, datetime as dt, html, json, math, os, re, sys, urllib.request

USER = os.environ.get("GH_USER", "sunquickpapi")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SVG_OUT = os.path.join(ROOT, "level-up.svg")
README = os.path.join(ROOT, "README.md")
SKIP_REPOS = {f"{USER}/{USER}"}  # the profile repo itself is just README edits

RED, BLUE, INK, PAPER = "#E23636", "#2F6FD6", "#111111", "#FFF6E0"
FONTS = {
    "Bangers": "https://cdn.jsdelivr.net/npm/@fontsource/bangers@5.1.0/files/bangers-latin-400-normal.woff2",
    "SG400": "https://cdn.jsdelivr.net/npm/@fontsource/space-grotesk@5.1.0/files/space-grotesk-latin-400-normal.woff2",
    "SG700": "https://cdn.jsdelivr.net/npm/@fontsource/space-grotesk@5.1.0/files/space-grotesk-latin-700-normal.woff2",
}


# ----------------------------------------------------------------- GitHub API
def _req(url, body=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "level-up"}
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=30) as r:
        return json.loads(r.read())


def gql(query, **variables):
    out = _req("https://api.github.com/graphql", {"query": query, "variables": variables})
    if out.get("errors"):
        raise SystemExit(f"GraphQL error: {out['errors']}")
    return out["data"]


def fetch():
    user = gql("""query($u:String!){ user(login:$u){ createdAt
      repositories(first:100, ownerAffiliations:OWNER, isFork:false, privacy:PUBLIC, orderBy:{field:CREATED_AT, direction:DESC}){
        totalCount nodes{ name createdAt description
          languages(first:10, orderBy:{field:SIZE, direction:DESC}){ edges{ size node{ name } } } } } } }""", u=USER)["user"]

    # contribution calendar, one year at a time (the API caps each query at a year)
    start_year = int(user["createdAt"][:4])
    today = dt.date.today()
    days, total = {}, 0
    for year in range(start_year, today.year + 1):
        frm = f"{year}-01-01T00:00:00Z"
        to = f"{year}-12-31T23:59:59Z" if year < today.year else dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        cal = gql("""query($u:String!,$f:DateTime!,$t:DateTime!){ user(login:$u){
          contributionsCollection(from:$f, to:$t){ contributionCalendar{ totalContributions
            weeks{ contributionDays{ date contributionCount } } } } } }""", u=USER, f=frm, t=to)
        c = cal["user"]["contributionsCollection"]["contributionCalendar"]
        total += c["totalContributions"]
        for w in c["weeks"]:
            for dday in w["contributionDays"]:
                days[dday["date"]] = dday["contributionCount"]

    events = _req(f"https://api.github.com/users/{USER}/events/public?per_page=100")
    return user, days, total, events


# ------------------------------------------------------------------- numbers
def streaks(days):
    today = dt.date.today()
    dates = sorted(days)
    best = run = 0
    for d in dates:
        run = run + 1 if days[d] > 0 else 0
        best = max(best, run)
    cur, d = 0, today
    if days.get(d.isoformat(), 0) == 0:  # today not counted yet: start from yesterday
        d -= dt.timedelta(days=1)
    while days.get(d.isoformat(), 0) > 0:
        cur += 1
        d -= dt.timedelta(days=1)
    return cur, best


def level_for(xp):
    # level n starts at 25*n*(n-1) XP: 0, 50, 150, 300, 500, 750, ...
    n = 1
    while xp >= 25 * (n + 1) * n:
        n += 1
    return n, 25 * n * (n - 1), 25 * (n + 1) * n


def top_languages(repos, k=4):
    sizes = {}
    for r in repos:
        for e in r["languages"]["edges"]:
            sizes[e["node"]["name"]] = sizes.get(e["node"]["name"], 0) + e["size"]
    tot = sum(sizes.values()) or 1
    return [(n, s / tot) for n, s in sorted(sizes.items(), key=lambda x: -x[1])[:k]]


def recent_lines(events, n=6):
    lines, seen = [], set()
    for e in events:
        repo = e["repo"]["name"]
        if repo in SKIP_REPOS:
            continue
        p, t = e.get("payload", {}), e["type"]
        day = e["created_at"][:10]
        name = repo.split("/", 1)[1] if repo.startswith(USER + "/") else repo
        link = f"[{name}](https://github.com/{repo})"
        if t == "PushEvent":
            key = ("push", repo, day)
            if key in seen:
                continue
            seen.add(key)
            text = f"🚀 Pushed to {link}"
        elif t == "CreateEvent" and p.get("ref_type") == "repository":
            text = f"✨ Created {link}"
        elif t == "ReleaseEvent":
            text = f"🏷️ Released {p.get('release', {}).get('tag_name', '')} of {link}"
        elif t == "PullRequestEvent" and p.get("action") in ("opened", "closed"):
            merged = p.get("pull_request", {}).get("merged")
            text = f"{'🔀 Merged' if merged else '📬 Opened'} a pull request in {link}"
        elif t == "IssuesEvent" and p.get("action") == "opened":
            text = f"🐛 Opened an issue in {link}"
        elif t == "PublicEvent":
            text = f"🌍 Made {link} public"
        elif t == "WatchEvent":
            text = f"⭐ Starred {link}"
        elif t == "ForkEvent":
            text = f"🍴 Forked {link}"
        else:
            continue
        when = dt.date.fromisoformat(day).strftime("%-d %b")
        lines.append(f"- {text} <sub>· {when}</sub>")
        if len(lines) >= n:
            break
    return lines or ["- 🌱 Nothing public in the last 90 days, but I'm building something"]


# ---------------------------------------------------------------------- SVG
def font_css():
    css = ""
    for key, url in FONTS.items():
        local = os.path.join(os.environ.get("FONT_DIR", ""), url.rsplit("/", 1)[1])
        try:
            if os.environ.get("FONT_DIR") and os.path.exists(local):
                b = base64.b64encode(open(local, "rb").read()).decode()
            else:
                with urllib.request.urlopen(url, timeout=30) as r:
                    b = base64.b64encode(r.read()).decode()
        except Exception:
            continue  # fall back to system fonts
        fam, weight = ("Bangers", 400) if key == "Bangers" else ("SG", int(key[2:]))
        css += f"@font-face{{font-family:'{fam}';font-weight:{weight};src:url(data:font/woff2;base64,{b}) format('woff2');}}"
    return css


def esc(s):
    return html.escape(str(s), quote=True)


def render(d, fonts=True):
    W, H = 1200, 310
    lvl, lo, hi = d["level"]
    frac = (d["total"] - lo) / (hi - lo)
    langs = ""
    for i, (name, share) in enumerate(d["langs"]):
        y = 104 + i * 38
        col = RED if i % 2 == 0 else BLUE
        langs += (f'<text x="660" y="{y + 16}" class="b" font-weight="700" font-size="15" fill="{INK}">{esc(name)}</text>'
                  f'<rect x="800" y="{y}" width="300" height="20" fill="#fff" stroke="{INK}" stroke-width="2"/>'
                  f'<rect x="801" y="{y + 1}" width="{max(3, 298 * share):.0f}" height="18" fill="{col}"/>'
                  f'<text x="1160" y="{y + 16}" class="b" font-weight="700" font-size="15" text-anchor="end" fill="{INK}">{share * 100:.0f}%</text>')
    stats = ""
    for i, (num, lab, col) in enumerate([(d["total"], "CONTRIBUTIONS", RED), (d["streak"], "DAY STREAK", BLUE),
                                         (d["repos"], "PUBLIC REPOS", RED)]):
        x = 38 + i * 136
        stats += (f'<rect x="{x}" y="196" width="124" height="86" fill="#fff" stroke="{INK}" stroke-width="2.5"/>'
                  f'<text x="{x + 12}" y="246" class="h" font-size="40" fill="{col}">{esc(num)}</text>'
                  f'<text x="{x + 12}" y="268" class="b" font-weight="700" font-size="11.5" letter-spacing="1" fill="#444">{esc(lab)}</text>')
    drop = d["latest"]
    body = f"""
<rect x="2" y="2" width="{W-4}" height="{H-4}" fill="{PAPER}" stroke="{INK}" stroke-width="3"/>
<rect x="2" y="2" width="12" height="{H-4}" fill="{RED}"/>
<text x="38" y="46" class="b" font-weight="700" font-size="14" letter-spacing="2.5" fill="{RED}">PLAYER STATS · UPDATED {esc(d['updated'])}</text>
<text x="36" y="112" class="h" font-size="64" fill="{INK}">LEVEL {lvl:02d}</text>
<rect x="38" y="128" width="400" height="22" fill="#fff" stroke="{INK}" stroke-width="2.5"/>
<rect x="39.5" y="129.5" width="{max(4, 397 * frac):.0f}" height="19" fill="{BLUE}"/>
<text x="38" y="176" class="b" font-size="15" fill="#333">{d['total'] - lo} / {hi - lo} XP · {hi - d['total']} to level {lvl + 1} · best streak {d['best']} days</text>
{stats}
<line x1="610" y1="30" x2="610" y2="{H-30}" stroke="{INK}" stroke-width="2" stroke-dasharray="4 6"/>
<text x="660" y="76" class="b" font-weight="700" font-size="14" letter-spacing="2.5" fill="{BLUE}">TOP LANGUAGES</text>
{langs}
<text x="660" y="278" class="b" font-weight="700" font-size="14" letter-spacing="2.5" fill="{RED}">LATEST DROP</text>
<text x="800" y="281" class="h" font-size="30" fill="{INK}">{esc(drop.replace("-", " ").replace("_", " ").upper())}</text>
"""
    style = (font_css() if fonts else "") + (
        ".h{font-family:'Bangers',Impact,'Arial Black',sans-serif;letter-spacing:1.5px}"
        ".b{font-family:'SG','Helvetica Neue',Arial,sans-serif}")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" '
            f'aria-label="GitHub stats: level {lvl}, {d["total"]} contributions, {d["streak"]}-day streak">'
            f"<style>{style}</style>{body}</svg>")


def update_readme(lines):
    with open(README, encoding="utf-8") as f:
        s = f.read()
    block = "<!--RECENT:START-->\n" + "\n".join(lines) + "\n<!--RECENT:END-->"
    s2, n = re.subn(r"<!--RECENT:START-->.*?<!--RECENT:END-->", block, s, flags=re.S)
    if n == 0:
        print("README has no <!--RECENT:START--> / <!--RECENT:END--> markers; skipped the list")
        return
    with open(README, "w", encoding="utf-8") as f:
        f.write(s2)


def main():
    demo = "--demo" in sys.argv
    if demo:
        user = {"repositories": {"totalCount": 11, "nodes": [
            {"name": "calm-call", "languages": {"edges": [{"size": 420000, "node": {"name": "TypeScript"}}]}},
            {"name": "AccessLens", "languages": {"edges": [{"size": 300000, "node": {"name": "JavaScript"}},
                                                           {"size": 140000, "node": {"name": "Python"}}]}},
            {"name": "Collaborative-E-Commerce", "languages": {"edges": [{"size": 120000, "node": {"name": "PHP"}}]}},
            {"name": "PORTFOLIO", "languages": {"edges": [{"size": 90000, "node": {"name": "TypeScript"}},
                                                          {"size": 60000, "node": {"name": "HTML"}}]}}]}}
        today = dt.date.today()
        days = {(today - dt.timedelta(days=i)).isoformat(): (3 if i < 7 else 0) for i in range(60)}
        total, events = 213, []
    else:
        if not TOKEN:
            raise SystemExit("GITHUB_TOKEN is not set")
        user, days, total, events = fetch()

    repos = [r for r in user["repositories"]["nodes"] if r["name"] != USER]
    cur, best = streaks(days)
    data = {
        "total": total, "streak": cur, "best": best,
        "repos": len(repos),
        "level": level_for(total), "langs": top_languages(repos),
        "latest": repos[0]["name"] if repos else "-",
        "updated": dt.date.today().strftime("%-d %b %Y").upper(),
    }
    with open(SVG_OUT, "w", encoding="utf-8") as f:
        f.write(render(data, fonts=True))
    print("wrote", SVG_OUT, data["level"], data["total"], data["streak"])
    if not demo:
        update_readme(recent_lines(events))
        print("updated README recent list")


if __name__ == "__main__":
    main()
