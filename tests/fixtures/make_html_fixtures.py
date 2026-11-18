"""Regenerate the offline HTML fixtures:  python tests/fixtures/make_html_fixtures.py"""
from pathlib import Path

OUT = Path(__file__).parent / "html"
OUT.mkdir(exist_ok=True)


def w(name: str, html: str) -> None:
    (OUT / name).write_text(html, encoding="utf-8")


# ---------- nav_heavy.html : ~15k tokens of junk around a few hundred tokens of signal
menu = "".join(
    f'<li><a href="/solutions/item-{i}">Solution area {i}</a><ul>'
    + "".join(f'<li><a href="/solutions/item-{i}/sub-{j}">Sub item {j} for area {i}</a></li>' for j in range(12))
    + "</ul></li>" for i in range(30))
scripts = "".join(
    f"<script>window.__DATA_{i}__ = {{" + ",".join(f'"k{k}":"{"x" * 40}"' for k in range(40)) + "};</script>"
    for i in range(12))
styles = "<style>" + " ".join(f".c{i}{{margin:{i}px;padding:{i}px;color:#{i:06x}}}" for i in range(800)) + "</style>"
cookie = ('<div id="onetrust-banner-sdk" class="cookie-consent"><p>We use cookies to improve your experience on our '
          'website and for marketing purposes. By clicking accept you agree to our use of cookies as described in our '
          'cookie policy.</p><button>Accept all</button></div>')
repeated = ('<section class="cta"><p>Join thousands of teams who already run their workflows on Ledgerly every '
            'single day.</p></section>') * 6
w("nav_heavy.html", f"""<!doctype html><html><head><title>Ledgerly | Spend management for finance teams</title>
<meta name="description" content="Ledgerly is a cloud spend-management platform that finance teams at mid-market B2B companies use to control vendor spend.">
{styles}{scripts}</head><body>
{cookie}
<header><nav><ul>{menu}</ul></nav></header>
<main>
<h1>Spend management for modern finance teams</h1>
<p>Ledgerly is a cloud subscription platform that helps finance teams at mid-market and enterprise B2B companies approve, track and control vendor spend in one place.</p>
<h2>Built for enterprise procurement</h2>
<p>Enterprise customers like Northwind Logistics use Ledgerly to route every purchase through approval workflows and security reviews before a vendor is onboarded.</p>
<p>We are preparing for our SOC 2 Type II audit, which is scheduled for the first quarter of 2027, after several enterprise prospects asked for the report.</p>
<h2>Pricing</h2>
<p>Plans start at $12 per user per month, with an Enterprise plan that adds SSO, audit logs and a dedicated success manager.</p>
<p>Ledgerly raised a $24M Series B led by Example Ventures to expand into enterprise accounts across Europe and North America.</p>
{repeated}
<div style="display:none"><p>Hidden promotional text that should never be included in the cleaned output for the model.</p></div>
</main>
<footer><nav><a href="/careers">Careers</a> <a href="/security">Security</a> <a href="/privacy">Privacy</a></nav>
<p>Copyright 2026 Ledgerly Inc. All rights reserved. Made with care in Bengaluru and London for finance teams.</p>
<img src="/gdpr.png" alt="GDPR compliant"></footer>
{scripts}
</body></html>""")

# ---------- thin homepage (~80 words, no stage, no compliance, no security link)
w("thin_homepage.html", """<!doctype html><html><head><title>Acme Thin | Invoice automation</title></head><body>
<header><nav><a href="/">Acme Thin</a> <a href="/careers">Careers</a> <a href="/login">Log in</a></nav></header>
<main>
<h1>Invoice automation for B2B finance teams</h1>
<p>Acme Thin is a cloud subscription product that reads supplier invoices, matches them to purchase orders and syncs approved bills to your accounting system.</p>
<p>Finance teams at growing B2B software companies use Acme Thin to close their books faster without adding headcount to accounts payable.</p>
<p>Connect your inbox, choose your approval rules and start processing invoices in an afternoon with our self-serve setup.</p>
</main>
<footer><p>Acme Thin Inc.</p></footer></body></html>""")

w("careers_compliance.html", """<!doctype html><html><head><title>Careers at Acme Thin</title></head><body>
<header><nav><a href="/">Home</a></nav></header>
<main>
<h1>Join Acme Thin</h1>
<p>We are a Series A company backed by Example Capital, growing fast as enterprise customers adopt our invoice automation platform.</p>
<h2>Open roles (7)</h2>
<ul>
<li><a href="/careers/head-of-compliance">Head of Compliance</a> <span>Remote, Europe</span> <time datetime="2026-10-20">Posted Oct 20, 2026</time></li>
<li>Senior Backend Engineer <time datetime="2026-10-02">Posted Oct 2, 2026</time></li>
<li>Product Designer <time datetime="2026-09-28">Posted Sep 28, 2026</time></li>
<li>Account Executive, Enterprise <time datetime="2026-10-11">Posted Oct 11, 2026</time></li>
<li>Customer Success Manager <time datetime="2026-09-15">Posted Sep 15, 2026</time></li>
<li>Data Engineer <time datetime="2026-10-05">Posted Oct 5, 2026</time></li>
<li>Frontend Engineer <time datetime="2026-10-09">Posted Oct 9, 2026</time></li>
</ul>
<h2>Head of Compliance</h2>
<p>You will own our SOC 2 and ISO 27001 programme end to end and lead our first SOC 2 Type II audit, working with engineering to answer enterprise security questionnaires.</p>
</main></body></html>""")

w("cloudflare_challenge.html", """<!doctype html><html><head><title>Just a moment...</title></head><body>
<div id="cf-browser-verification"><h1>Checking your browser before accessing the site.</h1>
<div class="cf-turnstile" data-sitekey="x"></div></div><script>1</script></body></html>""")

w("access_denied.html", """<!doctype html><html><head><title>Access denied</title></head><body><h1>Access denied</h1>
<p>You have been blocked. Reference #18.abc</p></body></html>""")

w("login_wall.html", """<!doctype html><html><head><title>Sign in</title></head><body><h1>Sign in to your account</h1>
<form><input type="email" name="email"><input type="password" name="password"><button>Log in</button></form></body></html>""")

w("spa_shell.html", """<!doctype html><html><head><title>App</title><script src="/a.js"></script><script src="/b.js"></script>
<script src="/c.js"></script></head><body><div id="root"></div></body></html>""")

w("parked.html", """<!doctype html><html><head><title>example-parked.com is for sale</title></head><body>
<h1>This domain is for sale!</h1><p>Buy this domain today. Fast and secure transfer via HugeDomains.</p></body></html>""")

w("consultancy_home.html", """<!doctype html><html><head><title>Brightpath Consulting | Digital transformation partner</title></head><body>
<header><nav><a href="/services">Services</a><a href="/careers">Careers</a></nav></header><main>
<h1>Your digital transformation partner</h1>
<p>Brightpath is an IT consultancy and outsourcing firm. Our 4,000 consultants deliver custom software projects, staff augmentation and managed services for banks and retailers.</p>
<p>We partner with clients on cloud migration, data engineering and application modernisation engagements across 20 countries.</p>
<p>Brightpath recently closed a Series B round to expand its delivery centres in Pune and Krakow.</p>
</main></body></html>""")

w("certified_saas_home.html", """<!doctype html><html><head><title>Schedulr | Scheduling infrastructure for everyone</title></head><body>
<header><nav><a href="/pricing">Pricing</a><a href="/security">Security</a><a href="/careers">Careers</a></nav></header><main>
<h1>Scheduling infrastructure for teams and enterprises</h1>
<p>Schedulr is an open-source scheduling platform sold as a cloud subscription to businesses, with an Enterprise plan for large organisations that need SSO and admin controls.</p>
<p>Thousands of companies, including enterprise customers in healthcare and finance, run their booking flows on Schedulr.</p>
</main><footer><p>Schedulr is SOC 2 Type II certified and HIPAA compliant.</p><img src="/soc2.png" alt="SOC 2 Type II"></footer></body></html>""")

if __name__ == "__main__":
    for p in sorted(OUT.glob("*.html")):
        print(f"{p.name:28s} {len(p.read_text(encoding='utf-8')) // 4:>6} tokens(est)")
