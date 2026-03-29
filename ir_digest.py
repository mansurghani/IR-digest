import urllib.request
import urllib.parse
import smtplib
import json
import re
import csv
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta

# ── CONFIG ──────────────────────────────────────────────
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
GMAIL_ADDRESS      = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
SHEET_ID           = os.environ["SHEET_ID"]
SHEET_URL          = os.environ["SHEET_URL"]
# ────────────────────────────────────────────────────────


def fetch_subscribers():
    print("Fetching subscribers from Google Sheet...")
    csv_url = "https://docs.google.com/spreadsheets/d/" + SHEET_ID + "/export?format=csv"
    with urllib.request.urlopen(csv_url) as r:
        content = r.read().decode("utf-8")
    reader = csv.reader(content.splitlines())
    next(reader, None)
    emails = []
    for row in reader:
        if row and row[0].strip() and "@" in row[0]:
            emails.append(row[0].strip().lower())
    emails = list(set(emails))
    print("Subscribers found: " + str(len(emails)))
    return emails


def fetch_papers():
    print("Step 1: Fetching papers from PubMed...")

    date_to   = datetime.now() - timedelta(days=1)
    date_from = datetime.now() - timedelta(days=8)
    date_to_str   = date_to.strftime("%Y/%m/%d")
    date_from_str = date_from.strftime("%Y/%m/%d")

    ir_journals_query = (
        '("J Vasc Interv Radiol"[Journal] OR '
        '"Tech Vasc Interv Radiol"[Journal] OR '
        '"Semin Intervent Radiol"[Journal])'
    )

    other_journals_query = (
        '("Cardiovasc Intervent Radiol"[Journal] OR "European Radiology"[Journal] '
        'OR "Radiology"[Journal] OR "J Am Coll Radiol"[Journal] '
        'OR "European Journal of Radiology"[Journal] OR "Abdom Radiol (NY)"[Journal] '
        'OR "Diagn Interv Imaging"[Journal] OR "Annals of Surgery"[Journal] '
        'OR "Journal of Hepatology"[Journal] OR "Hepatology"[Journal] '
        'OR "Journal of Vascular Surgery"[Journal] OR "European Heart Journal"[Journal] '
        'OR "Circulation"[Journal] OR "J. Clin. Oncol."[Journal] '
        'OR "Ann. Oncol."[Journal] OR "Gut"[Journal] OR "Lancet"[Journal] '
        'OR "N. Engl. J. Med."[Journal] OR "JAMA"[Journal] OR "BMJ"[Journal] '
        'OR "Cancer"[Journal] OR "Liver Cancer"[Journal] '
        'OR "Hepatology International"[Journal])'
    )

    ir_keywords_query = (
        '("Radiology, Interventional"[MeSH Terms] OR "embolization"[Title/Abstract] '
        'OR "ablation"[Title/Abstract] OR "endovascular"[Title/Abstract] '
        'OR "TIPS"[Title/Abstract] OR "transhepatic"[Title/Abstract] '
        'OR "percutaneous"[Title/Abstract] OR "thrombectomy"[Title/Abstract] '
        'OR "chemoembolization"[Title/Abstract] OR "radioembolization"[Title/Abstract] '
        'OR "Y90"[Title/Abstract] OR "TACE"[Title/Abstract] OR "TARE"[Title/Abstract] '
        'OR "catheter"[Title/Abstract] OR "angioplasty"[Title/Abstract] '
        'OR "image-guided"[Title/Abstract] OR "microwave ablation"[Title/Abstract] '
        'OR "radiofrequency ablation"[Title/Abstract] '
        'OR "transarterial"[Title/Abstract])'
    )

    exclusions_query = (
        'NOT ("cerebral"[Title] OR "intracranial"[Title] OR "stroke"[Title] '
        'OR "pediatric"[Title] OR "paediatric"[Title] OR "children"[Title] '
        'OR "neonatal"[Title] OR "congenital"[Title] '
        'OR "neurointervention"[Title/Abstract])'
    )

    date_query = '("' + date_from_str + '"[PDAT] : "' + date_to_str + '"[PDAT])'

    query = (
        '(' + ir_journals_query +
        ' OR (' + other_journals_query + ' AND ' + ir_keywords_query + ')) '
        + exclusions_query +
        ' AND ' + date_query
    )

    search_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        "?db=pubmed&term=" + urllib.parse.quote(query) + "&retmax=120&retmode=json"
    )

    with urllib.request.urlopen(search_url) as r:
        ids = json.loads(r.read())["esearchresult"]["idlist"]

    print("PubMed search complete. IDs found: " + str(len(ids)))

    if not ids:
        return None, None, None

    fetch_url = (
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        "?db=pubmed&id=" + ",".join(ids) + "&rettype=xml&retmode=xml"
    )

    with urllib.request.urlopen(fetch_url) as r:
        xml_data = r.read().decode("utf-8")

    print("XML fetched. Parsing articles...")

    papers = []
    for article_xml in xml_data.split("<PubmedArticle>")[1:]:
        medline = article_xml.split("</MedlineCitation>")[0]

        pmid_match = re.search(r"<PMID[^>]*>(\d+)</PMID>", medline)
        pmid = pmid_match.group(1) if pmid_match else ""

        journal_match = re.search(r"<Title>(.*?)</Title>", medline, re.DOTALL)
        journal = journal_match.group(1).strip() if journal_match else ""

        iso_match = re.search(r"<ISOAbbreviation>(.*?)</ISOAbbreviation>", medline)
        journal_abbr = iso_match.group(1).strip() if iso_match else ""

        title_match = re.search(r"<ArticleTitle>(.*?)</ArticleTitle>", medline, re.DOTALL)
        title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip() if title_match else ""

        abstract_match = re.search(r"<AbstractText[^>]*>(.*?)</AbstractText>", medline, re.DOTALL)
        abstract = re.sub(r"<[^>]+>", "", abstract_match.group(1)).strip() if abstract_match else ""

        if not abstract or not pmid:
            continue

        papers.append({
            "pmid":     pmid,
            "title":    title,
            "journal":  journal or journal_abbr,
            "abstract": abstract[:800],
            "url":      "https://pubmed.ncbi.nlm.nih.gov/" + pmid + "/"
        })

    print("Step 1 complete. Papers found: " + str(len(papers)))
    return papers, date_from_str, date_to_str


def summarize(papers):
    print("Step 2: Sending papers to Claude...")

    paper_list = ""
    for i, p in enumerate(papers, 1):
        paper_list += (
            "\n[" + str(i) + "] PMID:" + p["pmid"] + "\n"
            "Title: " + p["title"] + "\n"
            "Journal: " + p["journal"] + "\n"
            "Abstract: " + p["abstract"] + "\n"
        )

    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 8000,
        "messages": [{
            "role": "user",
            "content": (
                "You are a clinical expert in Interventional Radiology writing a weekly digest for IR colleagues.\n\n"
                "From the papers below, select the 6-8 most clinically impactful using this priority order:\n"
                "1. HIGHEST priority: Randomized controlled trials (RCTs), meta-analyses, systematic reviews\n"
                "2. MEDIUM priority: Prospective cohort studies, large retrospective series (n>100)\n"
                "3. LOW priority: Small retrospective studies, technical notes\n"
                "4. EXCLUDE or only include if exceptionally novel: Case reports, case series (n<10)\n\n"
                "Exclude anything focused on neuro IR or pediatric IR.\n\n"
                "For each paper respond using EXACTLY this format with no deviation:\n\n"
                "PMID: [the PMID number only, e.g. 12345678]\n"
                "TITLE: [full title]\n"
                "JOURNAL: [journal name]\n"
                "BOTTOM_LINE: [One sentence: the single most important takeaway from this paper]\n"
                "KEY_STAT: [One sentence containing the single most important quantitative result "
                "with specific numbers, confidence intervals, or p-values if available. "
                "If no quantitative data exists write NONE]\n"
                "SUMMARY: [Write 4-5 sentences covering: (1) study design and sample size, "
                "(2) patient population and intervention studied, "
                "(3) primary endpoint and key quantitative results with specific numbers, "
                "(4) statistical significance and any safety findings, "
                "(5) clinical takeaway for a practicing IR]\n"
                "---\n\n"
                "After all papers, end with:\n"
                "EDITORS_NOTE: [2 sentences on the overall theme of this week's literature]\n\n"
                "Papers:\n" + paper_list
            )
        }]
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type":      "application/json",
            "x-api-key":         ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01"
        }
    )

    with urllib.request.urlopen(req) as r:
        response_text = json.loads(r.read())["content"][0]["text"]

    print("Step 2 complete. Claude responded.")
    return response_text


def build_html_email(claude_response, papers, date_from, date_to):
    print("Step 3: Building HTML email...")

    url_map = {p["pmid"]: p["url"] for p in papers}

    entries = []
    editors_note = ""
    blocks = claude_response.split("---")

    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if block.startswith("EDITORS_NOTE:"):
            editors_note = block.replace("EDITORS_NOTE:", "").strip()
            continue

        pmid = title = journal = bottom_line = key_stat = summary = ""
        for line in block.split("\n"):
            line = line.strip()
            if line.startswith("PMID:"):
                pmid = line.replace("PMID:", "").strip()
            elif line.startswith("TITLE:"):
                title = line.replace("TITLE:", "").strip()
            elif line.startswith("JOURNAL:"):
                journal = line.replace("JOURNAL:", "").strip()
            elif line.startswith("BOTTOM_LINE:"):
                bottom_line = line.replace("BOTTOM_LINE:", "").strip()
            elif line.startswith("KEY_STAT:"):
                key_stat = line.replace("KEY_STAT:", "").strip()
            elif line.startswith("SUMMARY:"):
                summary = line.replace("SUMMARY:", "").strip()

        if not title or not summary:
            continue

        url = url_map.get(pmid, "https://pubmed.ncbi.nlm.nih.gov/" + pmid + "/") if pmid else "#"
        entries.append({
            "title":       title,
            "journal":     journal,
            "bottom_line": bottom_line,
            "key_stat":    key_stat,
            "summary":     summary,
            "url":         url
        })

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
            + (
                '<p style="margin:0 0 12px;font-size:15px;color:#333;line-height:1.7;'
                'text-decoration:underline;font-weight:600;">'
                + e["key_stat"] +
                '</p>'
                if e.get("key_stat") and e["key_stat"] != "NONE" else ""
            )
            +
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
        'Here is your weekly IR literature summary for papers published between '
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

    print("Step 3 complete.")
    return header + body + footer


def send_email(html_body, date_from, date_to, subscribers):
    print("Step 4: Sending email to " + str(len(subscribers)) + " subscribers...")

    fmt     = lambda d: datetime.strptime(d, "%Y/%m/%d").strftime("%b %d")
    subject = "IR Weekly Digest — " + fmt(date_from) + " to " + fmt(date_to) + ", " + str(datetime.now().year)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        for recipient in subscribers:
            msg = MIMEMultipart("alternative")
            msg["From"]    = GMAIL_ADDRESS
            msg["To"]      = recipient
            msg["Subject"] = subject
            msg.attach(MIMEText(html_body, "html"))
            s.send_message(msg)
            del msg["To"]
            print("  Sent to " + recipient)

    print("All done! Email sent to all subscribers.")


if __name__ == "__main__":
    print("Script started...")

    subscribers = fetch_subscribers()
    if not subscribers:
        print("No subscribers found in the Google Sheet. Add at least one email and try again.")
    else:
        papers, date_from, date_to = fetch_papers()
        if not papers:
            print("No papers found this week.")
        else:
            claude_response = summarize(papers)
            html = build_html_email(claude_response, papers, date_from, date_to)
            send_email(html, date_from, date_to, subscribers)
