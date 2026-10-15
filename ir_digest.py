import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET
import smtplib
import base64
import html
import json
import csv
import os
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta

# ── CONFIG ──────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
GMAIL_ADDRESS      = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
SHEET_ID           = os.environ["SHEET_ID"]
SHEET_URL          = os.environ["SHEET_URL"]

# Test mode: if TEST_EMAIL is set, the digest goes ONLY to that address
# and the PMID history is NOT updated (so the real Monday run is unaffected).
TEST_EMAIL         = os.environ.get("TEST_EMAIL", "").strip()

CLAUDE_MODEL       = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
PUBMED_RETMAX      = 250       # max candidate papers pulled from PubMed per week
MAX_ABSTRACT_CHARS = 6000      # safety cap only; normal abstracts are never cut
HTTP_TIMEOUT       = 60        # seconds
# ────────────────────────────────────────────────────────


# ── JOURNALS ────────────────────────────────────────────
# All names are the official MEDLINE (NLM) title abbreviations, searched with
# the [ta] tag. Each was checked against PubMed to confirm it returns results.

# Every paper from these is a candidate (no keyword filter).
IR_JOURNALS = [
    "J Vasc Interv Radiol",
    "Cardiovasc Intervent Radiol",
    "Semin Intervent Radiol",
    "Tech Vasc Interv Radiol",
    "CVIR Endovasc",
]

# Papers from these must also match an IR keyword.
OTHER_JOURNALS = [
    # Radiology
    "Radiology", "Eur Radiol", "AJR Am J Roentgenol", "J Am Coll Radiol",
    "Eur J Radiol", "Abdom Radiol (NY)", "Diagn Interv Imaging",
    "Korean J Radiol", "Radiol Imaging Cancer", "Diagn Interv Radiol",
    # Hepatology / GI
    "J Hepatol", "Hepatology", "JHEP Rep", "Hepatol Int", "Liver Cancer",
    "Gut", "Gastroenterology", "Clin Gastroenterol Hepatol",
    "Lancet Gastroenterol Hepatol",
    # Oncology
    "J Clin Oncol", "Ann Oncol", "Lancet Oncol", "JAMA Oncol", "Cancer",
    # Surgery / vascular / venous
    "Ann Surg", "J Vasc Surg", "J Vasc Surg Venous Lymphat Disord",
    "Eur J Vasc Endovasc Surg",
    # Cardiopulmonary / thrombosis (PE, DVT)
    "Circulation", "Eur Heart J", "Chest", "J Thromb Haemost",
    # Dialysis access
    "J Vasc Access",
    # Urology (PAE, renal ablation)
    "Eur Urol", "J Urol",
    # General medicine
    "N Engl J Med", "Lancet", "JAMA", "BMJ", "Nat Med",
]
# ────────────────────────────────────────────────────────


def http_request(url, data=None, headers=None, method=None, retries=4, label="request"):
    """HTTP call with a timeout and retries on rate limits, server errors and network errors."""
    headers = headers or {}
    delay = 10
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            retryable = e.code in (408, 429, 500, 502, 503, 504, 529)
            print("  " + label + " failed (HTTP " + str(e.code) + "): " + body)
            if not retryable or attempt == retries:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            print("  " + label + " failed (network): " + str(e))
            if attempt == retries:
                raise
        print("  Retrying " + label + " in " + str(delay) + "s (attempt " + str(attempt + 1) + "/" + str(retries) + ")...")
        time.sleep(delay)
        delay *= 2


def fetch_subscribers():
    if TEST_EMAIL:
        print("TEST MODE: sending only to " + TEST_EMAIL)
        return [TEST_EMAIL.lower()]
    print("Fetching subscribers from Google Sheet...")
    csv_url = "https://docs.google.com/spreadsheets/d/" + SHEET_ID + "/export?format=csv"
    content = http_request(csv_url, label="Google Sheet").decode("utf-8")
    reader = csv.reader(content.splitlines())
    next(reader, None)
    emails = set()
    for row in reader:
        if row and "@" in row[0]:
            emails.add(row[0].strip().lower())
    print("Subscribers found: " + str(len(emails)))
    return sorted(emails)


def build_query(date_from_str, date_to_str):
    journal_or = lambda names: "(" + " OR ".join('"' + n + '"[ta]' for n in names) + ")"

    ir_keywords_query = (
        '("Radiology, Interventional"[MeSH Terms] OR "embolization"[Title/Abstract] '
        'OR "ablation"[Title/Abstract] OR "endovascular"[Title/Abstract] '
        'OR "TIPS"[Title/Abstract] OR "transhepatic"[Title/Abstract] '
        'OR "percutaneous"[Title/Abstract] OR "thrombectomy"[Title/Abstract] '
        'OR "chemoembolization"[Title/Abstract] OR "radioembolization"[Title/Abstract] '
        'OR "Y90"[Title/Abstract] OR "TACE"[Title/Abstract] OR "TARE"[Title/Abstract] '
        'OR "catheter"[Title/Abstract] OR "angioplasty"[Title/Abstract] '
        'OR "image-guided"[Title/Abstract] OR "transarterial"[Title/Abstract])'
    )

    # Note: bare "aneurysm" is NOT excluded, so visceral/splenic/renal artery
    # aneurysm embolization stays in. Neuro aneurysms are caught by
    # "cerebral"/"intracranial"/"subarachnoid"; aortic repair by EVAR/TEVAR terms.
    exclusions_query = (
        'NOT ("cerebral"[Title] OR "intracranial"[Title] OR "stroke"[Title] '
        'OR "subarachnoid"[Title] '
        'OR "pediatric"[Title] OR "paediatric"[Title] OR "children"[Title] '
        'OR "neonatal"[Title] OR "congenital"[Title] '
        'OR "neurointervention"[Title/Abstract] '
        'OR "meningeal"[Title] OR "dural"[Title] '
        'OR "cerebrovascular"[Title] '
        'OR "arteriovenous malformation"[Title] OR "AVM"[Title] '
        'OR "carotid stenting"[Title] OR "carotid artery stenting"[Title] '
        'OR "EVAR"[Title] OR "TEVAR"[Title] OR "endovascular aneurysm repair"[Title] '
        'OR "coronary"[Title] OR "transcatheter aortic"[Title] '
        'OR "TAVR"[Title] OR "TAVI"[Title] '
        'OR "percutaneous coronary"[Title] '
        'OR "atrial fibrillation"[Title] OR "cardiac ablation"[Title] '
        'OR "electrophysiology"[Title] OR "valvuloplasty"[Title] '
        'OR "mitral valve"[Title] OR "aortic valve"[Title] '
        'OR "left atrial appendage"[Title] OR "WATCHMAN"[Title] '
        'OR "patent foramen"[Title] OR "septal defect"[Title] '
        'OR "cardiac catheterization"[Title])'
    )

    # EDAT = the date the record was added to PubMed. This is "new this week",
    # unlike the publication date, which is often just a month (counted as the 1st).
    date_query = '("' + date_from_str + '"[EDAT] : "' + date_to_str + '"[EDAT])'

    return (
        "(" + journal_or(IR_JOURNALS) +
        " OR (" + journal_or(OTHER_JOURNALS) + " AND " + ir_keywords_query + ")) "
        + exclusions_query + " AND " + date_query
    )


def element_text(el):
    """All text inside an element, including nested tags like <i>, <sup>. Entities are already decoded."""
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def fetch_papers():
    print("Step 1: Fetching papers from PubMed...")

    date_to   = datetime.now() - timedelta(days=1)
    date_from = datetime.now() - timedelta(days=7)
    date_to_str   = date_to.strftime("%Y/%m/%d")
    date_from_str = date_from.strftime("%Y/%m/%d")

    query = build_query(date_from_str, date_to_str)
    search_body = urllib.parse.urlencode({
        "db": "pubmed", "term": query, "retmax": PUBMED_RETMAX,
        "retmode": "json", "sort": "pub_date",
    }).encode()
    result = json.loads(http_request(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        data=search_body, label="PubMed search"))["esearchresult"]

    ids   = result.get("idlist", [])
    total = int(result.get("count", 0))
    print("PubMed search complete. Total matches: " + str(total) + ", retrieved: " + str(len(ids)))
    if total > len(ids):
        print("  WARNING: " + str(total - len(ids)) + " matches not retrieved (cap is " +
              str(PUBMED_RETMAX) + "). Consider raising PUBMED_RETMAX.")
    if result.get("errorlist") or result.get("warninglist"):
        print("  PubMed notes: " + json.dumps({k: result.get(k) for k in ("errorlist", "warninglist")}))

    if not ids:
        return None, None, None

    fetch_body = urllib.parse.urlencode({
        "db": "pubmed", "id": ",".join(ids), "rettype": "xml", "retmode": "xml",
    }).encode()
    xml_data = http_request(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        data=fetch_body, label="PubMed fetch")

    print("XML fetched. Parsing articles...")
    root = ET.fromstring(xml_data)

    papers = []
    for art in root.iter("PubmedArticle"):
        pmid = element_text(art.find("MedlineCitation/PMID"))
        article = art.find("MedlineCitation/Article")
        if not pmid or article is None:
            continue

        title   = element_text(article.find("ArticleTitle"))
        journal = element_text(article.find("Journal/Title")) or element_text(article.find("Journal/ISOAbbreviation"))

        # Keep section labels (BACKGROUND, RESULTS, ...) so Claude can see the structure.
        parts = []
        for ab in article.findall("Abstract/AbstractText"):
            text = element_text(ab)
            label = ab.get("Label")
            if text:
                parts.append((label.upper() + ": " if label else "") + text)
        abstract = " ".join(parts)
        if len(abstract) < 100:
            continue  # letters, errata, comments etc.

        pub_types = [element_text(pt) for pt in article.findall("PublicationTypeList/PublicationType")]

        papers.append({
            "pmid":      pmid,
            "title":     title,
            "journal":   journal,
            "pub_types": [t for t in pub_types if t and t != "Journal Article"],
            "abstract":  abstract[:MAX_ABSTRACT_CHARS],
            "url":       "https://pubmed.ncbi.nlm.nih.gov/" + pmid + "/",
        })

    print("Step 1 complete. Papers with abstracts: " + str(len(papers)))
    return papers, date_from_str, date_to_str


def github_headers():
    return {
        "Authorization": "token " + os.environ.get("GITHUB_TOKEN", ""),
        "User-Agent":    "IR-Digest",
        "Accept":        "application/vnd.github+json",
    }


def load_sent_pmids():
    """Load previously sent PMIDs from GitHub via the API."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not os.environ.get("GITHUB_TOKEN") or not repo:
        print("No GitHub token/repo found, skipping PMID history.")
        return set()
    url = "https://api.github.com/repos/" + repo + "/contents/sent_pmids.txt"
    try:
        data = json.loads(http_request(url, headers=github_headers(), label="Load PMIDs"))
        content = base64.b64decode(data["content"]).decode("utf-8")
        pmids = set(line.strip() for line in content.splitlines() if line.strip())
        print("Loaded " + str(len(pmids)) + " previously sent PMIDs.")
        return pmids
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print("No PMID history file yet (first run).")
            return set()
        raise  # don't silently resend old papers if GitHub is having problems


def save_sent_pmids(pmids):
    """Save sent PMIDs back to the GitHub repository."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not os.environ.get("GITHUB_TOKEN") or not repo:
        print("No GitHub token/repo found, skipping PMID save.")
        return
    url = "https://api.github.com/repos/" + repo + "/contents/sent_pmids.txt"
    sha = None
    try:
        sha = json.loads(http_request(url, headers=github_headers(), label="Get PMID file")).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    payload = {
        "message": "Update sent PMIDs",
        "content": base64.b64encode("\n".join(sorted(pmids)).encode("utf-8")).decode("utf-8"),
    }
    if sha:
        payload["sha"] = sha
    http_request(url, data=json.dumps(payload).encode(), method="PUT",
                 headers=dict(github_headers(), **{"Content-Type": "application/json"}),
                 label="Save PMIDs")
    print("Saved " + str(len(pmids)) + " PMIDs to repository.")


# Claude must answer by calling this tool, so the reply is guaranteed to be
# structured JSON instead of free text that has to be split apart.
DIGEST_TOOL = {
    "name": "submit_digest",
    "description": "Submit the selected papers and editor's note for this week's IR digest.",
    "input_schema": {
        "type": "object",
        "properties": {
            "papers": {
                "type": "array",
                "minItems": 1,
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "pmid":        {"type": "string", "description": "PMID exactly as given in the input, digits only."},
                        "bottom_line": {"type": "string", "description": "One sentence: the single most important takeaway."},
                        "summary":     {"type": "string", "description": "4-5 sentences; exactly one sentence wrapped in <u></u>."},
                    },
                    "required": ["pmid", "bottom_line", "summary"],
                },
            },
            "editors_note": {"type": "string", "description": "2 sentences on the overall theme of this week's literature."},
        },
        "required": ["papers", "editors_note"],
    },
}


def summarize(papers):
    print("Step 2: Sending " + str(len(papers)) + " papers to Claude (" + CLAUDE_MODEL + ")...")

    paper_list = ""
    for i, p in enumerate(papers, 1):
        paper_list += (
            "\n[" + str(i) + "] PMID: " + p["pmid"] + "\n"
            "Title: " + p["title"] + "\n"
            "Journal: " + p["journal"] + "\n"
            "Publication type (from PubMed): " + (", ".join(p["pub_types"]) or "Journal Article") + "\n"
            "Abstract: " + p["abstract"] + "\n"
        )

    prompt = (
        "You are a clinical expert in Interventional Radiology writing a weekly digest for IR colleagues.\n\n"
        "From the papers below, select the 6-8 most clinically impactful using this priority order "
        "(use the PubMed publication type as well as the abstract to judge study design):\n"
        "1. HIGHEST priority: Randomized controlled trials (RCTs), meta-analyses, systematic reviews\n"
        "2. MEDIUM priority: Prospective cohort studies, large retrospective series (n>100)\n"
        "3. LOW priority: Small retrospective studies, technical notes\n"
        "4. EXCLUDE or only include if exceptionally novel: Case reports, case series (n<10)\n\n"
        "Exclude papers that are specific to neuro IR, pediatric IR, interventional cardiology or "
        "vascular surgery (such as EVAR).\n\n"
        "For each selected paper provide:\n"
        "- pmid: copied exactly from the input\n"
        "- bottom_line: one sentence, the single most important takeaway\n"
        "- summary: 4-5 sentences covering (1) study design and sample size, (2) patient population and "
        "intervention studied, (3) primary endpoint and key quantitative results with specific numbers, "
        "(4) statistical significance and any safety findings, (5) clinical takeaway for a practicing IR. "
        "Wrap exactly one sentence in <u> and </u>. The underlined sentence MUST be a quantitative result "
        "(numbers, percentages, hazard/odds ratios, p-values or confidence intervals), never a qualitative "
        "statement. If the abstract has no quantitative data, write 'No quantitative data reported.' and "
        "underline that.\n"
        "CORRECT: <u>The 1-year primary patency rate was 87% in the stent group vs 63% in the angioplasty "
        "group (p=0.002).</u>\n"
        "INCORRECT: <u>This study demonstrated that TACE was superior to best supportive care.</u>\n"
        "Use only numbers stated in the abstract. Do not use any other HTML.\n\n"
        "Then write an editors_note: 2 sentences on the overall theme of this week's literature.\n\n"
        "You MUST submit your answer by calling the submit_digest tool. Do not reply with plain text.\n\n"
        "Papers:\n" + paper_list
    )

    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 12000,
        "tools": [DIGEST_TOOL],
        # Newer models don't allow forcing a specific tool; "auto" plus the
        # prompt instruction works, and the text fallback below covers the rest.
        "tool_choice": {"type": "auto"},
        "messages": [{"role": "user", "content": prompt}],
    }).encode()

    raw = http_request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        retries=5,
        label="Claude API",
    )
    response = json.loads(raw)
    content = response.get("content", [])

    digest = None
    for block in content:
        if block.get("type") == "tool_use" and block.get("name") == "submit_digest":
            digest = block["input"]
            break

    if digest is None:
        # Fallback: Claude answered in text. Pull out the JSON object if there is one.
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                candidate = json.loads(text[start:end + 1])
                if isinstance(candidate, dict) and "papers" in candidate:
                    digest = candidate
                    print("  (Claude replied in text; parsed the JSON from it.)")
            except json.JSONDecodeError:
                pass

    if digest is None:
        raise RuntimeError("Claude did not return a digest. Stop reason: " +
                           str(response.get("stop_reason")) + ". Reply start: " +
                           json.dumps(content)[:500])

    print("Step 2 complete. Claude selected " + str(len(digest.get("papers", []))) + " papers.")
    return digest


def safe_summary(text):
    """Escape everything Claude wrote, then allow back only the <u></u> tags."""
    escaped = html.escape(text, quote=False)
    return escaped.replace("&lt;u&gt;", "<u>").replace("&lt;/u&gt;", "</u>")


def build_html_email(digest, papers, date_from, date_to):
    print("Step 3: Building HTML email...")

    by_pmid = {p["pmid"]: p for p in papers}
    entries, seen = [], set()
    for item in digest.get("papers", []):
        pmid = "".join(ch for ch in str(item.get("pmid", "")) if ch.isdigit())
        paper = by_pmid.get(pmid)
        if not paper:
            print("  Dropped an entry with PMID '" + str(item.get("pmid")) + "' (not in this week's candidates).")
            continue
        if pmid in seen or not item.get("summary"):
            continue
        seen.add(pmid)
        # Title, journal and link come from PubMed, not from Claude.
        entries.append({
            "pmid":        pmid,
            "title":       html.escape(paper["title"]),
            "journal":     html.escape(paper["journal"]),
            "url":         paper["url"],
            "bottom_line": html.escape(item.get("bottom_line", "")),
            "summary":     safe_summary(item["summary"]),
        })

    if not entries:
        raise RuntimeError("No valid papers in Claude's response; not sending an empty digest.")

    editors_note = html.escape(digest.get("editors_note", ""))

    fmt        = lambda d: datetime.strptime(d, "%Y/%m/%d").strftime("%B %d, %Y")
    date_range = fmt(date_from) + " to " + fmt(date_to)

    articles_html = ""
    for e in entries:
        articles_html += (
            '<div style="margin-bottom:28px;padding-bottom:24px;border-bottom:1px solid #e8e8e8;">'
            '<p style="margin:0 0 4px;font-size:12px;color:#888;text-transform:uppercase;letter-spacing:0.5px;">'
            + e["journal"] +
            '</p>'
            '<h2 style="margin:0 0 10px;font-size:17px;color:#1a3a5c;line-height:1.4;">'
            + e["title"] +
            '</h2>'
            '<p style="margin:0 0 12px;font-size:15px;color:#1a3a5c;line-height:1.5;'
            'font-style:italic;font-weight:600;border-left:3px solid #1a3a5c;padding-left:10px;">'
            + e["bottom_line"] +
            '</p>'
            '<p style="margin:0 0 12px;font-size:15px;color:#333;line-height:1.7;">'
            + e["summary"] +
            '</p>'
            '<a href="' + e["url"] + '" style="display:inline-block;background:#1a3a5c;color:white;'
            'text-decoration:none;padding:7px 16px;border-radius:5px;font-size:13px;font-weight:600;">'
            'View on PubMed &#8594;</a>'
            '</div>'
        )

    header = (
        '<html><body style="margin:0;padding:0;background:#f4f4f0;font-family:Georgia,serif;">'
        '<div style="max-width:660px;margin:32px auto;background:white;border-radius:10px;'
        'overflow:hidden;box-shadow:0 2px 12px rgba(0,0,0,0.08);">'
        '<div style="background:#1a3a5c;padding:28px 36px;">'
        '<p style="margin:0;color:#a8c4e0;font-size:13px;text-transform:uppercase;letter-spacing:1px;">'
        'Interventional Radiology</p>'
        '<h1 style="margin:6px 0 0;color:white;font-size:24px;">Weekly Literature Digest</h1>'
        '</div>'
        '<div style="padding:24px 36px 20px;border-bottom:2px solid #1a3a5c;">'
        '<p style="margin:0 0 10px;font-size:15px;color:#444;line-height:1.7;">'
        'Here is your weekly IR literature summary for papers added to PubMed between '
        '<strong>' + date_range + '</strong>. The following ' + str(len(entries)) +
        ' articles were selected from high-impact journals for their clinical relevance to general IR practice.'
        '</p>'
        '<p style="margin:0;font-size:14px;color:#888;line-height:1.7;font-style:italic;">'
        'This AI-generated summary is a work in progress and feedback is welcome. '
        'Please reply to this email with errors and suggestions for improvement.'
        '</p>'
        '</div>'
    )

    body = '<div style="padding:28px 36px;">' + articles_html + '</div>'

    footer = (
        '<div style="background:#f0f4f8;padding:20px 36px;border-top:2px solid #1a3a5c;">'
        '<p style="margin:0 0 6px;font-size:12px;color:#888;text-transform:uppercase;letter-spacing:0.5px;">'
        "Editor's Note</p>"
        '<p style="margin:0;font-size:14px;color:#333;line-height:1.7;font-style:italic;">'
        + editors_note +
        '</p></div>'
        '<div style="padding:20px 36px;text-align:center;border-top:1px solid #e8e8e8;">'
        '<p style="margin:0 0 12px;font-size:13px;color:#666;">'
        'To subscribe or unsubscribe from this newsletter, click below.</p>'
        '<a href="' + SHEET_URL + '" style="display:inline-block;background:#1a3a5c;color:white;'
        'text-decoration:none;padding:8px 20px;border-radius:5px;font-size:13px;font-weight:600;">'
        'Manage Subscription</a>'
        '<p style="margin:12px 0 0;font-size:11px;color:#aaa;">'
        'Add your email to the list to subscribe, or delete it to unsubscribe.'
        '</p></div>'
        '<div style="padding:16px 36px;text-align:center;">'
        '<p style="margin:0;font-size:12px;color:#aaa;">'
        'Papers sourced from PubMed · Summarized by Claude AI · For informational purposes only'
        '</p></div></div></body></html>'
    )

    print("Step 3 complete. " + str(len(entries)) + " papers in the email.")
    included_pmids = [e["pmid"] for e in entries]
    return header + body + footer, included_pmids


def make_message(recipient, subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html"))
    return msg


def send_email(html_body, date_from, date_to, subscribers):
    print("Step 4: Sending email to " + str(len(subscribers)) + " subscriber(s)...")

    fmt     = lambda d: datetime.strptime(d, "%Y/%m/%d").strftime("%b %d")
    subject = "IR Weekly Digest — " + fmt(date_from) + " to " + fmt(date_to) + ", " + date_to[:4]
    if TEST_EMAIL:
        subject = "[TEST] " + subject

    sent       = 0
    delivered  = set()
    retry_wait = 60  # seconds to wait if rate limited

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=HTTP_TIMEOUT) as s:
            s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            for recipient in subscribers:
                try:
                    s.send_message(make_message(recipient, subject, html_body))
                    delivered.add(recipient)
                    sent += 1
                    print("  Sent to " + recipient + " (" + str(sent) + "/" + str(len(subscribers)) + ")")
                    time.sleep(2)
                except smtplib.SMTPServerDisconnected:
                    raise
                except smtplib.SMTPException as e:
                    print("  Failed to send to " + recipient + ": " + str(e))
                    if "rate" in str(e).lower() or "421" in str(e) or "450" in str(e):
                        print("  Rate limit detected. Waiting " + str(retry_wait) + " seconds...")
                        time.sleep(retry_wait)
    except (smtplib.SMTPException, OSError) as e:
        print("  SMTP connection lost: " + str(e))

    # Everyone not yet delivered (individual failures or a dropped connection) gets one retry.
    failed = [r for r in subscribers if r not in delivered]
    if failed:
        print("Retrying " + str(len(failed)) + " failed recipients...")
        time.sleep(retry_wait)
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=HTTP_TIMEOUT) as s:
                s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                for recipient in failed:
                    try:
                        s.send_message(make_message(recipient, subject, html_body))
                        sent += 1
                        print("  Retry succeeded: " + recipient)
                        time.sleep(2)
                    except Exception as e:
                        print("  Retry failed: " + recipient + ": " + str(e))
        except Exception as e:
            print("  Retry connection failed: " + str(e))

    print("All done! Successfully sent to " + str(sent) + "/" + str(len(subscribers)) + " subscribers.")
    return sent


if __name__ == "__main__":
    print("Script started...")

    subscribers = fetch_subscribers()
    if not subscribers:
        print("No subscribers found in the Google Sheet. Add at least one email and try again.")
        raise SystemExit(0)

    papers, date_from, date_to = fetch_papers()
    if not papers:
        print("No papers found this week.")
        raise SystemExit(0)

    sent_pmids = load_sent_pmids()
    new_papers = [p for p in papers if p["pmid"] not in sent_pmids]
    if len(papers) > len(new_papers):
        print("Filtered out " + str(len(papers) - len(new_papers)) + " previously seen papers.")
    print("New papers to summarize: " + str(len(new_papers)))
    if not new_papers:
        print("No new papers this week after filtering duplicates.")
        raise SystemExit(0)

    digest = summarize(new_papers)
    html_email, included_pmids = build_html_email(digest, new_papers, date_from, date_to)
    sent = send_email(html_email, date_from, date_to, subscribers)

    # Only the papers that actually appeared in the email are recorded.
    if TEST_EMAIL:
        print("TEST MODE: PMID history not updated.")
    elif sent == 0:
        raise SystemExit("No emails were delivered; PMID history not updated so next run can retry.")
    else:
        print("Recording " + str(len(included_pmids)) + " sent PMIDs: " + ", ".join(included_pmids))
        save_sent_pmids(sent_pmids | set(included_pmids))
