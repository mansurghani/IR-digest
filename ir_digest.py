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
FORM_URL           = os.environ["FORM_URL"]   # public link to the subscribe/unsubscribe Google Form

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
    "Radiol Med", "Interv Radiol (Higashimatsuyama)", "Int J Hyperthermia",
    # Hepatology / GI
    "J Hepatol", "Hepatology", "JHEP Rep", "Hepatol Int", "Liver Cancer",
    "Liver Int", "J Hepatocell Carcinoma", "Clin Mol Hepatol",
    "Gut", "Gastroenterology", "Clin Gastroenterol Hepatol",
    "Lancet Gastroenterol Hepatol", "Aliment Pharmacol Ther", "Am J Gastroenterol",
    # Oncology
    "J Clin Oncol", "Ann Oncol", "Lancet Oncol", "JAMA Oncol", "Cancer",
    # Surgery / vascular / venous
    "Ann Surg", "Br J Surg", "JAMA Surg",
    "J Vasc Surg", "J Vasc Surg Venous Lymphat Disord", "Eur J Vasc Endovasc Surg",
    "J Endovasc Ther", "Ann Vasc Surg", "Vasc Med",
    # Cardiopulmonary / thrombosis (PE, DVT)
    "Circulation", "Eur Heart J", "Chest", "J Thromb Haemost",
    "Res Pract Thromb Haemost", "Thromb Res",
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
    print("Fetching subscribers from Google Form responses...")
    csv_url = "https://docs.google.com/spreadsheets/d/" + SHEET_ID + "/export?format=csv"
    content = http_request(csv_url, label="Google Sheet").decode("utf-8")
    rows = list(csv.reader(content.splitlines()))
    if not rows:
        return []

    # Find the columns by their header text, so column order doesn't matter.
    header = [h.strip().lower() for h in rows[0]]
    email_col  = next((i for i, h in enumerate(header) if "email" in h), None)
    action_col = next((i for i, h in enumerate(header) if "subscribe" in h), None)
    if email_col is None:
        raise RuntimeError("No column with 'email' in its header found in the sheet: " + str(rows[0]))

    # Rows are in submission order, so each person's latest choice wins.
    status = {}
    for row in rows[1:]:
        if len(row) <= email_col:
            continue
        email = row[email_col].strip().lower()
        if "@" not in email:
            continue
        action = row[action_col].strip().lower() if action_col is not None and len(row) > action_col else "subscribe"
        status[email] = not action.startswith("unsubscribe")

    emails = sorted(e for e, subscribed in status.items() if subscribed)
    print("Subscribers found: " + str(len(emails)) +
          " (" + str(len(status) - len(emails)) + " unsubscribed)")
    return emails


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

    # Rule 3: high-level evidence on a core IR topic, from ANY journal, so a major
    # trial or meta-analysis isn't missed just because it ran somewhere unexpected.
    core_ir_topics_query = (
        '("Radiology, Interventional"[MeSH] OR "Embolization, Therapeutic"[MeSH] '
        'OR "Chemoembolization, Therapeutic"[MeSH] '
        'OR "Portasystemic Shunt, Transjugular Intrahepatic"[MeSH] OR "Vena Cava Filters"[MeSH] '
        'OR "chemoembolization"[tiab] OR "radioembolization"[tiab] OR "transarterial"[tiab] '
        'OR "embolization"[tiab] OR "embolisation"[tiab] OR "transjugular intrahepatic"[tiab] '
        'OR "microwave ablation"[tiab] OR "radiofrequency ablation"[tiab] OR "cryoablation"[tiab] '
        'OR "histotripsy"[tiab] OR "irreversible electroporation"[tiab] '
        'OR "catheter-directed"[tiab] OR "percutaneous thrombectomy"[tiab] '
        'OR "interventional radiology"[tiab] OR "interventional radiologist"[tiab] '
        'OR "hepatic arterial infusion"[tiab] OR "yttrium-90"[tiab] '
        'OR "selective internal radiation"[tiab] OR "retrograde transvenous"[tiab] '
        'OR "percutaneous transhepatic"[tiab] OR "vertebroplasty"[tiab] OR "kyphoplasty"[tiab])'
    )
    high_evidence_query = (
        '("Randomized Controlled Trial"[pt] OR "Meta-Analysis"[pt] OR "Systematic Review"[pt])'
    )

    return (
        "(" + journal_or(IR_JOURNALS) +
        " OR (" + journal_or(OTHER_JOURNALS) + " AND " + ir_keywords_query + ")"
        " OR (" + core_ir_topics_query + " AND " + high_evidence_query + ")) "
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


def call_claude(prompt, tool, max_tokens, required_key, _retry_truncated=True):
    """Ask Claude to answer through `tool`; returns the tool input as a dict."""
    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "tools": [tool],
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

    if response.get("stop_reason") == "max_tokens":
        # The reply was cut off, so the tool input is incomplete. Retry once with more room.
        if _retry_truncated:
            print("  Claude's reply was cut off at " + str(max_tokens) + " tokens; retrying with more room...")
            return call_claude(prompt, tool, max_tokens * 2, required_key, _retry_truncated=False)
        raise RuntimeError("Claude's reply was cut off even at " + str(max_tokens) + " tokens.")

    for block in content:
        if block.get("type") == "tool_use" and block.get("name") == tool["name"]:
            return block["input"]

    # Fallback: Claude answered in text. Pull out the JSON object if there is one.
    text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            candidate = json.loads(text[start:end + 1])
            if isinstance(candidate, dict) and required_key in candidate:
                print("  (Claude replied in text; parsed the JSON from it.)")
                return candidate
        except json.JSONDecodeError:
            pass

    raise RuntimeError("Claude did not return the expected answer. Stop reason: " +
                       str(response.get("stop_reason")) + ". Reply start: " +
                       json.dumps(content)[:500])


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

    digest = call_claude(prompt, DIGEST_TOOL, max_tokens=12000, required_key="papers")

    print("Step 2 complete. Claude selected " + str(len(digest.get("papers", []))) + " papers.")
    return digest


# ── FULL-TEXT ANALYSIS PAGES ────────────────────────────
# For selected papers that are in the PubMed Central Open Access subset (with a
# Creative Commons license, so figures may be republished with attribution), we
# fetch the full text and figures from NCBI's public open-data bucket, have
# Claude write a detailed analysis, and publish it as a page on GitHub Pages.
# Papers without open-access full text get no button.

PMC_S3          = "https://pmc-oa-opendata.s3.amazonaws.com/"
SITE_DIR        = "docs"                   # GitHub Pages serves this folder
MAX_FULLTEXT_CHARS = 150000
MAX_KEY_FIGURES    = 4
WEB_IMAGE_EXTS  = (".jpg", ".jpeg", ".png", ".gif", ".webp")
XLINK_HREF      = "{http://www.w3.org/1999/xlink}href"


def pages_base_url():
    """Public URL of the GitHub Pages site, e.g. https://owner.github.io/IR-digest/"""
    override = os.environ.get("PAGES_BASE_URL", "").strip()
    if override:
        return override.rstrip("/") + "/"
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return ""
    owner, name = repo.split("/", 1)
    return "https://" + owner.lower() + ".github.io/" + name + "/"


def pmids_to_pmcids(pmids):
    """Map PMIDs to numeric PMC IDs (only for papers that are in PubMed Central)."""
    if not pmids:
        return {}
    url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/elink.fcgi?dbfrom=pubmed&db=pmc"
           "&linkname=pubmed_pmc&retmode=json&id=" + "&id=".join(pmids))
    data = json.loads(http_request(url, label="PubMed->PMC link"))
    out = {}
    for ls in data.get("linksets", []):
        ids = ls.get("ids", [])
        for db in ls.get("linksetdbs", []) or []:
            if db.get("links") and ids:
                out[str(ids[0])] = str(db["links"][0])
    return out


def find_open_access_record(pmc_num):
    """Return (prefix, metadata) for the latest version in the PMC open-data bucket, or (None, None)."""
    listing = http_request(PMC_S3 + "?list-type=2&delimiter=/&prefix=PMC" + pmc_num + ".",
                           label="PMC open-data listing")
    root = ET.fromstring(listing)
    ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    prefixes = [p.text for p in root.findall("s3:CommonPrefixes/s3:Prefix", ns)]
    if not prefixes:
        return None, None
    prefix = max(prefixes, key=lambda p: int(p.rstrip("/").split(".")[-1]))   # e.g. PMC123.2/
    ver_id = prefix.rstrip("/")
    meta = json.loads(http_request(PMC_S3 + prefix + ver_id + ".json", label="PMC metadata"))
    return prefix, meta


def xml_text(el):
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def paragraph_text(el):
    """Paragraph text without any figures/tables nested inside it (PMC often nests them in <p>)."""
    parts = [el.text or ""]
    for child in el:
        if child.tag.split("}")[-1] not in ("fig", "table-wrap"):
            parts.append(paragraph_text(child))
        parts.append(child.tail or "")
    return " ".join("".join(parts).split())


def table_to_html(table_el):
    """Rebuild a JATS/XHTML table as plain, safe HTML (only structural tags kept)."""
    keep = {"table", "thead", "tbody", "tfoot", "tr", "th", "td"}
    inline = {"bold": "b", "italic": "i", "sup": "sup", "sub": "sub"}

    def walk(el):
        tag = el.tag.split("}")[-1]
        inner = html.escape(el.text or "")
        for child in el:
            inner += walk(child) + html.escape(child.tail or "")
        if tag in keep:
            attrs = ""
            for a in ("colspan", "rowspan"):
                v = el.get(a)
                if v and v.isdigit():
                    attrs += ' ' + a + '="' + v + '"'
            return "<" + tag + attrs + ">" + inner + "</" + tag + ">"
        if tag in inline:
            return "<" + inline[tag] + ">" + inner + "</" + inline[tag] + ">"
        if tag == "break":
            return "<br>"
        return inner

    return walk(table_el)


def parse_fulltext(xml_bytes, media_names):
    """Pull section text plus figures/tables out of a JATS article."""
    root = ET.fromstring(xml_bytes)
    body = root.find(".//body")
    if body is None:
        return None, []

    lines = []

    def walk(el, depth):
        tag = el.tag.split("}")[-1]
        if tag in ("fig", "table-wrap"):
            return                        # captured separately below
        if tag == "title":
            lines.append("\n" + "#" * min(depth + 1, 4) + " " + xml_text(el))
            return
        if tag == "p":
            lines.append(paragraph_text(el))
            return
        for child in el:
            walk(child, depth + (1 if tag == "sec" else 0))

    walk(body, 0)

    def media_for(href):
        if not href:
            return None
        base = href.split("/")[-1]
        for name in media_names:      # bucket file may carry an extension the XML omits
            if name == base or name.startswith(base + "."):
                return name
        return None

    visuals = []
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag not in ("fig", "table-wrap"):
            continue
        label = xml_text(el.find("label")) or ("Figure" if tag == "fig" else "Table")
        caption = xml_text(el.find("caption"))
        graphic = el.find(".//graphic")
        image = media_for(graphic.get(XLINK_HREF)) if graphic is not None else None
        if image and not image.lower().endswith(WEB_IMAGE_EXTS):
            image = None              # e.g. TIFF: browsers can't show it
        table_el = el.find(".//table") if tag == "table-wrap" else None
        table_html = table_to_html(table_el) if table_el is not None else ""
        table_text = ""
        if table_el is not None:
            rows = []
            for tr in table_el.iter():
                if tr.tag.split("}")[-1] == "tr":
                    rows.append(" | ".join(xml_text(c) for c in tr
                                           if c.tag.split("}")[-1] in ("td", "th")))
            table_text = "\n".join(rows)
        if not image and not table_html:
            continue
        visuals.append({
            "id": el.get("id") or label, "kind": tag, "label": label, "caption": caption,
            "image": image, "table_html": table_html, "table_text": table_text,
        })

    text = "\n".join(l for l in lines if l.strip())
    for v in visuals:
        text += "\n\n[" + v["label"] + "] " + v["caption"]
        if v["table_text"]:
            text += "\n" + v["table_text"]
    return text[:MAX_FULLTEXT_CHARS], visuals


ANALYSIS_TOOL = {
    "name": "submit_analysis",
    "description": "Submit the detailed analysis of one full-text paper.",
    "input_schema": {
        "type": "object",
        "properties": {
            "overview":         {"type": "string", "description": "3-4 sentence overview of the study and its main finding."},
            "study_design":     {"type": "string", "description": "Design, setting, enrollment period, randomization/blinding if any, sample size."},
            "population":       {"type": "string", "description": "Inclusion/exclusion criteria and baseline characteristics that matter."},
            "intervention":     {"type": "string", "description": "Intervention and comparator, including technical details useful to an IR (devices, embolics, doses, approach)."},
            "results": {
                "type": "array",
                "description": "Primary endpoint first, then key secondary endpoints, each with the exact numbers reported.",
                "items": {
                    "type": "object",
                    "properties": {"endpoint": {"type": "string"}, "finding": {"type": "string"}},
                    "required": ["endpoint", "finding"],
                },
            },
            "safety":           {"type": "string", "description": "Adverse events with grading system and rates."},
            "strengths":        {"type": "array", "items": {"type": "string"}},
            "limitations":      {"type": "array", "items": {"type": "string"},
                                 "description": "Include both author-acknowledged limitations and others a critical reader would raise (bias, confounding, power, follow-up, generalizability)."},
            "context":          {"type": "string", "description": "How the findings relate to prior evidence discussed in the paper."},
            "practice_implications": {"type": "string", "description": "What a practicing IR should take away, and how confident to be."},
            "key_figures": {
                "type": "array",
                "maxItems": MAX_KEY_FIGURES,
                "description": "The most informative figures/tables, chosen only from the IDs listed.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id":  {"type": "string", "description": "ID exactly as listed."},
                        "why": {"type": "string", "description": "1-2 sentences on what it shows and why it matters."},
                    },
                    "required": ["id", "why"],
                },
            },
        },
        "required": ["overview", "study_design", "results", "limitations", "practice_implications", "key_figures"],
    },
}


def visual_key(s):
    """Normalize 'F2', 'Fig. 2', 'Figure 2', 'fig2' -> 'fig2'; 'T3', 'Table 3' -> 'tab3'."""
    s = str(s or "").strip().lower()
    num = "".join(ch for ch in s if ch.isdigit())
    letters = "".join(ch for ch in s if ch.isalpha())
    if letters.startswith(("fig", "f")) and not letters.startswith("ft"):
        return "fig" + num
    if letters.startswith(("tab", "t")):
        return "tab" + num
    return s


def match_visual(ref, visuals):
    """Find the figure/table Claude meant, whether it used the ID or the label."""
    ref = str(ref or "").strip()
    for v in visuals:
        if ref == v["id"] or ref.lower() == v["label"].lower().rstrip("."):
            return v
    key = visual_key(ref)
    for v in visuals:
        if key and (key == visual_key(v["label"]) or key == visual_key(v["id"])):
            return v
    return None


def analysis_problems(analysis, visuals):
    """Return a list of reasons the analysis isn't good enough to publish."""
    problems = []
    # Core sections every paper should have; others (safety, intervention) may legitimately be thin.
    for field, minimum in (("overview", 200), ("study_design", 80), ("practice_implications", 120)):
        if len(str(analysis.get(field) or "").strip()) < minimum:
            problems.append(field + " is missing or too brief")
    results = [r for r in (analysis.get("results") or []) if isinstance(r, dict) and r.get("finding")]
    if len(results) < 2:
        problems.append("results should list the primary endpoint and the key secondary endpoints")
    if len(analysis.get("limitations") or []) < 3:
        problems.append("limitations should list at least three items")
    if visuals:
        matched = [kf for kf in (analysis.get("key_figures") or [])
                   if isinstance(kf, dict) and match_visual(kf.get("id"), visuals)]
        if not matched:
            problems.append("key_figures must use IDs from the list provided (e.g. " +
                            ", ".join(v["id"] for v in visuals[:3]) + ")")
    return problems


def analyze_fulltext(paper, text, visuals):
    visual_list = "\n".join('- ID "' + v["id"] + '" = ' + v["label"] + ": " + v["caption"][:400]
                            for v in visuals) or "(none)"
    prompt = (
        "You are an experienced interventional radiologist writing a detailed, critical analysis of a "
        "full-text paper for IR colleagues who have already read a short summary of it.\n\n"
        "Rules:\n"
        "- Use only information in the paper. Quote numbers exactly as reported (with CIs and p-values).\n"
        "- Be a critical appraiser, not a press release: weigh the strength of evidence honestly.\n"
        "- Plain text only, no HTML or markdown.\n"
        "- Be thorough: this page is for readers who want more than the abstract. Aim for roughly "
        "900-1400 words in total. Give the primary endpoint and every important secondary endpoint "
        "(survival, response, local control, subgroup or adjusted analyses), each with exact numbers, "
        "and at least three substantive limitations.\n"
        "- For key_figures, choose up to " + str(MAX_KEY_FIGURES) + " of the figures/tables listed that "
        "best show the main results (e.g. Kaplan-Meier curves, forest plots, primary outcome tables, "
        "flow diagrams for trials). In key_figures, put the quoted ID exactly as listed "
        "(for example \"" + (visuals[0]["id"] if visuals else "F1") + "\"), not the label.\n\n"
        "You MUST submit your answer by calling the submit_analysis tool.\n\n"
        "Title: " + paper["title"] + "\nJournal: " + paper["journal"] + "\n\n"
        "Figures and tables available:\n" + visual_list + "\n\n"
        "Full text:\n" + text
    )
    analysis = call_claude(prompt, ANALYSIS_TOOL, max_tokens=12000, required_key="overview")
    problems = analysis_problems(analysis, visuals)
    if problems:
        print("    First draft incomplete (" + "; ".join(problems) + "). Asking Claude to revise...")
        retry_prompt = (prompt + "\n\nA previous attempt was rejected because: " + "; ".join(problems) +
                        ". Write the complete analysis again, fixing these problems.")
        analysis = call_claude(retry_prompt, ANALYSIS_TOOL, max_tokens=12000, required_key="overview")
        problems = analysis_problems(analysis, visuals)
        if problems:
            raise RuntimeError("analysis still incomplete after retry: " + "; ".join(problems))
    return analysis


def render_analysis_page(paper, meta, analysis, visuals, image_files):
    e = lambda t: html.escape(str(t or ""))
    para = lambda t: "".join("<p>" + e(x) + "</p>" for x in str(t or "").split("\n") if x.strip())
    bullets = lambda items: "<ul>" + "".join("<li>" + e(i) + "</li>" for i in (items or []) if str(i).strip()) + "</ul>"
    sections = []
    def section(title, inner):
        if inner and inner.strip() and inner not in ("<ul></ul>",):
            sections.append("<h2>" + title + "</h2>" + inner)

    section("Overview", para(analysis.get("overview")))
    section("Study design", para(analysis.get("study_design")))
    section("Patients", para(analysis.get("population")))
    section("Intervention and comparator", para(analysis.get("intervention")))
    rows = "".join("<tr><th>" + e(r.get("endpoint")) + "</th><td>" + e(r.get("finding")) + "</td></tr>"
                   for r in analysis.get("results") or [] if isinstance(r, dict))
    section("Results", '<table class="results">' + rows + "</table>" if rows else "")

    figs_html = ""
    for kf in analysis.get("key_figures") or []:
        v = match_visual(kf.get("id") if isinstance(kf, dict) else None, visuals)
        if not v:
            continue
        if v["image"] and v["image"] in image_files:
            visual = '<img src="' + e(image_files[v["image"]]) + '" alt="' + e(v["label"]) + '" loading="lazy">'
        elif v["table_html"]:
            visual = '<div class="tablewrap">' + v["table_html"] + "</div>"
        else:
            continue
        figs_html += ("<figure>" + visual + "<figcaption><strong>" + e(v["label"]) + ".</strong> " +
                      e(v["caption"]) + '<span class="why"><strong>Why it matters:</strong> ' +
                      e(kf.get("why")) + "</span></figcaption></figure>")
    section("Key figures", figs_html)

    section("Safety", para(analysis.get("safety")))
    section("Strengths", bullets(analysis.get("strengths")))
    section("Limitations", bullets(analysis.get("limitations")))
    section("In context", para(analysis.get("context")))
    section("Implications for practice", para(analysis.get("practice_implications")))

    citation = meta.get("citation") or paper["journal"]
    doi = meta.get("doi")
    license_code = meta.get("license_code") or "a Creative Commons license"
    source_link = ("https://doi.org/" + doi) if doi else paper["url"]

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>" + e(paper["title"]) + " — IR Digest analysis</title><style>"
        ":root{--navy:#1a3a5c;--ink:#2b2b2b;--muted:#777;--rule:#e6e6e6;--bg:#f4f4f0;--card:#fff;--tint:#f0f4f8}"
        "@media (prefers-color-scheme:dark){:root{--navy:#9cc0e4;--ink:#e4e4e4;--muted:#a0a0a0;--rule:#333;--bg:#121417;--card:#1b1e22;--tint:#20262d}}"
        "*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.7 Georgia,serif}"
        ".wrap{max-width:820px;margin:0 auto;padding:16px}"
        "article{background:var(--card);border-radius:10px;padding:28px 32px;box-shadow:0 2px 12px rgba(0,0,0,.08)}"
        ".kicker{font:600 12px/1.4 -apple-system,Segoe UI,sans-serif;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:0 0 6px}"
        "h1{color:var(--navy);font-size:26px;line-height:1.3;margin:0 0 10px}"
        ".cite{color:var(--muted);font-size:14px;margin:0 0 18px}"
        "h2{color:var(--navy);font-size:19px;margin:30px 0 8px;padding-top:14px;border-top:1px solid var(--rule)}"
        "table{border-collapse:collapse;width:100%;font-size:15px}"
        "th,td{border-bottom:1px solid var(--rule);padding:8px 10px;text-align:left;vertical-align:top}"
        ".results th{width:34%;color:var(--navy)}"
        "figure{margin:18px 0 26px}figure img{display:block;max-width:100%;height:auto;margin:0 auto;background:#fff;border-radius:4px}"
        ".tablewrap{overflow-x:auto;font-size:13px;background:var(--tint);border-radius:6px;padding:4px}"
        "figcaption{font-size:14px;color:var(--muted);margin-top:8px;line-height:1.55}"
        ".why{display:block;margin-top:6px;color:var(--ink)}"
        "ul{padding-left:22px}li{margin:4px 0}"
        ".btn{display:inline-block;background:var(--navy);color:var(--card);text-decoration:none;padding:7px 16px;border-radius:5px;font:600 13px -apple-system,Segoe UI,sans-serif;margin:4px 8px 4px 0}"
        "footer{font-size:13px;color:var(--muted);margin-top:30px;padding-top:14px;border-top:1px solid var(--rule)}"
        "@media (max-width:600px){article{padding:20px 16px}h1{font-size:22px}body{font-size:16px}}"
        "</style></head><body><div class='wrap'><article>"
        '<p class="kicker">IR Weekly Literature Digest · Full-text analysis</p>'
        "<h1>" + e(paper["title"]) + "</h1>"
        '<p class="cite">' + e(citation) + "</p>"
        '<a class="btn" href="' + e(source_link) + '">Read the original article</a>'
        '<a class="btn" href="' + e(paper["url"]) + '">PubMed</a>'
        + "".join(sections) +
        "<footer>This analysis was generated by Claude AI from the open-access full text and may contain errors; "
        "check the original article before acting on it. Figures and tables are reproduced from "
        + e(citation) + " under " + e(license_code) +
        ' (<a href="' + e(source_link) + '">source</a>), via PubMed Central.</footer>'
        "</article></div></body></html>"
    )


def build_analysis_page(paper, pmc_num, run_dir):
    """Create docs/<date>/<pmid>/index.html. Returns the relative path, or None if not possible."""
    prefix, meta = find_open_access_record(pmc_num)
    if not meta:
        print("  PMID " + paper["pmid"] + ": not in the PMC open-access dataset.")
        return None
    license_code = (meta.get("license_code") or "").upper()
    if not meta.get("is_pmc_openaccess") or not (license_code.startswith("CC") or "PUBLIC" in license_code):
        print("  PMID " + paper["pmid"] + ": in PMC but not openly licensed (" + str(meta.get("license_code")) + ").")
        return None
    if meta.get("is_retracted"):
        return None

    ver_id = prefix.rstrip("/")
    media_names = [u.split("/")[-1].split("?")[0] for u in meta.get("media_urls") or []]
    xml_bytes = http_request(PMC_S3 + prefix + ver_id + ".xml", label="PMC full text")
    text, visuals = parse_fulltext(xml_bytes, media_names)
    if not text or len(text) < 2000:
        print("  PMID " + paper["pmid"] + ": full text too short to analyze.")
        return None

    print("  PMID " + paper["pmid"] + ": analyzing full text (" + str(len(text)) + " chars, " +
          str(len(visuals)) + " figures/tables)...")
    analysis = analyze_fulltext(paper, text, visuals)

    out_dir = os.path.join(run_dir, paper["pmid"])
    os.makedirs(out_dir, exist_ok=True)
    image_files = {}
    for kf in analysis.get("key_figures") or []:
        v = match_visual(kf.get("id") if isinstance(kf, dict) else None, visuals)
        if not v or not v["image"] or v["image"] in image_files:
            continue
        try:
            data = http_request(PMC_S3 + prefix + urllib.parse.quote(v["image"]), label="figure " + v["image"])
            with open(os.path.join(out_dir, v["image"]), "wb") as f:
                f.write(data)
            image_files[v["image"]] = v["image"]
        except Exception as ex:
            print("    Could not download " + v["image"] + ": " + str(ex))

    shown = [kf for kf in analysis.get("key_figures") or []
             if isinstance(kf, dict) and (lambda v: v and ((v["image"] in image_files) or v["table_html"]))(
                 match_visual(kf.get("id"), visuals))]
    if visuals and not shown:
        print("  PMID " + paper["pmid"] + ": no figures could be included; skipping the page.")
        return None
    words = sum(len(str(analysis.get(k) or "").split()) for k in
                ("overview", "study_design", "population", "intervention", "safety", "context", "practice_implications"))
    print("    Analysis: ~" + str(words) + " words of prose, " + str(len(analysis.get("results") or [])) +
          " results, " + str(len(shown)) + " figures/tables.")

    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_analysis_page(paper, meta, analysis, visuals, image_files))
    return os.path.relpath(out_dir, SITE_DIR).replace(os.sep, "/") + "/"


def publish_site(message):
    """Commit and push the docs/ folder so GitHub Pages serves the new pages."""
    import subprocess
    def git(*args):
        return subprocess.run(["git"] + list(args), capture_output=True, text=True)
    git("config", "user.name", "IR Digest Bot")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    git("add", SITE_DIR)
    if git("diff", "--cached", "--quiet").returncode == 0:
        return True                       # nothing new
    commit = git("commit", "-m", message)
    if commit.returncode != 0:
        print("  git commit failed: " + commit.stderr.strip())
        return False
    for attempt in range(3):
        push = git("push")
        if push.returncode == 0:
            print("  Published analysis pages.")
            return True
        print("  git push failed (attempt " + str(attempt + 1) + "): " + push.stderr.strip())
        git("pull", "--rebase")
        time.sleep(5)
    return False


def build_analysis_pages(digest, papers, date_to):
    """Returns {pmid: absolute URL} for selected papers that got a full-text page."""
    base = pages_base_url()
    if not base:
        print("Step 2b: skipped (no GitHub repository info, so no Pages URL).")
        return {}
    print("Step 2b: Building full-text analysis pages...")
    by_pmid = {p["pmid"]: p for p in papers}
    selected = []
    for item in digest.get("papers", []):
        pmid = "".join(ch for ch in str(item.get("pmid", "")) if ch.isdigit())
        if pmid in by_pmid and pmid not in selected:
            selected.append(pmid)

    try:
        pmcids = pmids_to_pmcids(selected)
    except Exception as ex:
        print("  Could not look up PMC IDs: " + str(ex))
        return {}
    print("  " + str(len(pmcids)) + " of " + str(len(selected)) + " selected papers are in PubMed Central.")

    run_dir = os.path.join(SITE_DIR, date_to.replace("/", "-"))
    built = {}
    for pmid in selected:
        if pmid not in pmcids:
            continue
        try:
            rel = build_analysis_page(by_pmid[pmid], pmcids[pmid], run_dir)
            if rel:
                built[pmid] = base + rel
        except Exception as ex:
            print("  PMID " + pmid + ": analysis page failed (" + str(ex) + "); no button for this paper.")

    if not built:
        print("  No full-text pages this week.")
        return {}
    os.makedirs(SITE_DIR, exist_ok=True)
    open(os.path.join(SITE_DIR, ".nojekyll"), "w").close()
    if not publish_site("Add full-text analysis pages for " + date_to):
        print("  Could not publish pages; sending without full-text buttons.")
        return {}
    print("  Full-text pages: " + str(len(built)))
    return built


def safe_summary(text):
    """Escape everything Claude wrote, then allow back only the <u></u> tags."""
    escaped = html.escape(text, quote=False)
    return escaped.replace("&lt;u&gt;", "<u>").replace("&lt;/u&gt;", "</u>")


def build_html_email(digest, papers, date_from, date_to, analysis_urls=None):
    analysis_urls = analysis_urls or {}
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
            "analysis":    analysis_urls.get(pmid),
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
            + (
                ' &nbsp;<a href="' + html.escape(e["analysis"]) + '" style="display:inline-block;background:#ffffff;'
                'color:#1a3a5c;border:1.5px solid #1a3a5c;text-decoration:none;padding:6px 15px;border-radius:5px;'
                'font-size:13px;font-weight:600;">Full-Text Analysis &amp; Figures &#8594;</a>'
                if e["analysis"] else ""
            ) +
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
        'Know a colleague who would enjoy this? Forward it along. To subscribe or unsubscribe, use the form below.</p>'
        '<a href="' + FORM_URL + '" style="display:inline-block;background:#1a3a5c;color:white;'
        'text-decoration:none;padding:8px 20px;border-radius:5px;font-size:13px;font-weight:600;">'
        'Manage Subscription</a>'
        '</div>'
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
    analysis_urls = build_analysis_pages(digest, new_papers, date_to)
    html_email, included_pmids = build_html_email(digest, new_papers, date_from, date_to, analysis_urls)
    sent = send_email(html_email, date_from, date_to, subscribers)

    # Only the papers that actually appeared in the email are recorded.
    if TEST_EMAIL:
        print("TEST MODE: PMID history not updated.")
    elif sent == 0:
        raise SystemExit("No emails were delivered; PMID history not updated so next run can retry.")
    else:
        print("Recording " + str(len(included_pmids)) + " sent PMIDs: " + ", ".join(included_pmids))
        save_sent_pmids(sent_pmids | set(included_pmids))
