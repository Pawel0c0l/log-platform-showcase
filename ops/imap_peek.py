import os, re
import imaplib
from email import message_from_bytes
from email.header import decode_header
from email.policy import default as default_policy
from datetime import datetime, timedelta, timezone

URL_PATTERN = re.compile(r"https?://[^\s<>'\"`]+", re.IGNORECASE)
ALLOWED_EXT = {".xls", ".xlsx", ".csv"}

def dec(v):
    if not v:
        return ""
    out=[]
    for chunk, enc in decode_header(v):
        if isinstance(chunk, bytes):
            out.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out).strip()

def imap_since(days:int)->str:
    target = datetime.now(timezone.utc) - timedelta(days=days)
    return target.strftime("%d-%b-%Y")

def extract_urls(msg):
    urls=[]
    parts = msg.walk() if msg.is_multipart() else [msg]
    for p in parts:
        if p.is_multipart():
            continue
        ctype = (p.get_content_type() or "").lower()
        if ctype not in ("text/plain","text/html"):
            continue
        payload = p.get_payload(decode=True) or b""
        if not payload:
            continue
        charset = p.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        urls += [u.rstrip(").,;\"'<>") for u in URL_PATTERN.findall(text)]
    # unique, keep order
    seen=set()
    out=[]
    for u in urls:
        if u and u not in seen:
            seen.add(u); out.append(u)
    return out

def count_attachments(msg):
    n=0
    files=[]
    for part in msg.walk():
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True) or b""
        if not payload:
            continue
        fn = dec(part.get_filename() or "")
        if not fn:
            continue
        lower = fn.lower()
        if any(lower.endswith(ext) for ext in ALLOWED_EXT):
            n += 1
            files.append(fn)
    return n, files

def main():
    host = os.environ["IMAP_HOST"]
    user = os.environ.get("IMAP_USER","")
    pwd  = os.environ["IMAP_PASSWORD"]
    port = int(os.environ.get("IMAP_PORT","993"))
    mailbox = os.environ.get("IMAP_MAILBOX","INBOX.Raporty FleetWeb")
    sender = os.environ.get("SENDER_FILTER","reportingpl@fleetmail.telematics-provider.example")
    since_days = int(os.environ.get("SINCE_DAYS","30"))

    imap = imaplib.IMAP4_SSL(host, port)
    imap.login(user, pwd)
    st, _ = imap.select(f'"{mailbox}"')
    if st != "OK":
        raise SystemExit(f"IMAP SELECT failed: {st}")

    since = imap_since(since_days)
    st, data = imap.uid("SEARCH", None, "FROM", f'"{sender}"', "SINCE", since)
    if st != "OK":
        raise SystemExit(f"IMAP SEARCH failed: {st}")

    uids = data[0].split() if data and data[0] else []
    print(f"mailbox={mailbox} sender={sender} since={since} uids_total={len(uids)}")
    print("showing up to last 25 uids (sorted asc):")
    uids_sorted = sorted(int(u) for u in uids)
    tail = uids_sorted[-25:]

    for uid in tail:
        st, fetched = imap.uid("FETCH", str(uid), "(RFC822)")
        if st != "OK" or not fetched:
            print(f"UID {uid}: FETCH failed")
            continue
        msg_bytes = None
        for item in fetched:
            if isinstance(item, tuple) and len(item) >= 2:
                msg_bytes = item[1]
                break
        if not msg_bytes:
            print(f"UID {uid}: empty message")
            continue

        msg = message_from_bytes(msg_bytes, policy=default_policy)
        subj = dec(msg.get("Subject",""))
        date = dec(msg.get("Date",""))
        mid  = dec(msg.get("Message-ID",""))
        attach_n, attach_files = count_attachments(msg)
        urls = extract_urls(msg)
        rpt = [u for u in urls if "rptdownload.telematics-provider.example" in u.lower()]

        print("\n---")
        print(f"UID: {uid}")
        print(f"Date: {date}")
        print(f"Message-ID: {mid}")
        print(f"Subject: {subj}")
        print(f"Attachments (xls/xlsx/csv): {attach_n}")
        if attach_n:
            for f in attach_files[:10]:
                print(f"  - {f}")
            if attach_n > 10:
                print(f"  ... ({attach_n-10} more)")
        print(f"URLs found: {len(urls)} | rptdownload: {len(rpt)}")
        for u in rpt[:5]:
            print(f"  * {u}")

    imap.close()
    imap.logout()

if __name__ == "__main__":
    main()
