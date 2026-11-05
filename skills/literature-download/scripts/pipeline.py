# -*- coding: utf-8 -*-
"""检索 PubMed，仅交付已验证全文；固定输出规范见 references/output-contract.md。"""
import argparse
import csv
import difflib
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/"
PMC_S3 = "https://pmc-oa-opendata.s3.amazonaws.com/"
UA = {"User-Agent": "literature-download-skill/2.0"}
WORKERS = 10
# 批量预取的 PMID→PMCID 映射，运行期填充；fetch_one 兜底时回写。
PMCid_CACHE = {}
CSV_FIELDS = ["获取状态", "pmid", "年份", "期刊", "标题", "doi", "文件", "检索来源"]
STATUSES = {"全文(原版PDF)": "原版全文", "全文(XML渲染)": "全文"}
OUTPUT_NAMES = {"文献清单.md", "文献索引.csv", "论文"}
LEGACY_MARKER = "<!-- literature-download-v2 "
REPORT_SECTIONS = ["研究范围", "检索记录", "全文汇总", "全文文献", "说明"]
REPORT_NOTES = [
    "仅收录实际取得、通过正文与身份检查并去重的全文；一篇文献保留一份 PDF。",
    "XML 渲染件保留受支持的正文和表格；不能可靠保留的内容不纳入交付。",
    "自动检查不能证明全文绝对完整，使用前应核对关键内容。",
]


def require_pdf_reader():
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("缺少 pypdf 全文验证依赖，请在当前 Python 环境安装后重试。") from exc
    return PdfReader


def get(url, binary=False, timeout=25, retries=2):
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = response.read()
            return data if binary else data.decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            # 4xx 属于确定性失败（无权限、不存在），重试只会浪费时间。
            if exc.code < 500 or attempt == retries:
                raise
            time.sleep(0.5)
        except Exception:
            if attempt == retries:
                raise
            time.sleep(0.5)


def jget(url, timeout=25):
    return json.loads(get(url, timeout=timeout))


def find_chrome():
    for path in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    ):
        if Path(path).is_file():
            return path
    return None


CHROME = find_chrome()


def normalize_doi(value):
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "",
                  str(value or "").strip(), flags=re.I).strip().lower()


def words(value):
    return re.findall(r"\w+", unicodedata.normalize("NFKC", str(value)).lower())


def title_matches(expected, actual):
    wanted = words(expected)
    available = words(actual)
    if not wanted:
        return False
    if len(wanted) < 3:
        return " ".join(wanted) in " ".join(available)
    matches = difflib.SequenceMatcher(None, wanted, available, autojunk=False).get_matching_blocks()
    return (sum(match.size for match in matches) / len(wanted) >= 0.8
            and max(match.size for match in matches) >= min(5, len(wanted)))


def year_of(meta):
    match = re.search(r"(?:19|20)\d{2}", meta.get("pubdate", "") or "")
    return match.group(0) if match else ""


def safe_name(value):
    text = re.sub(r'[\x00-\x1f\\/:*?"<>|]+', " ", value or "")
    return re.sub(r"\s+", " ", text).strip().strip(".")[:60].rstrip() or "untitled"


def pdf_name(meta, kind):
    return "[%s]_%s_%s_%s.pdf" % (
        kind, meta["pmid"], year_of(meta) or "0000", safe_name(meta["title"]))


def validate_pdf(path, meta, expected_body=None):
    """保守核对可解析正文与身份；不以页数、文件大小代替全文判断。"""
    reader_class = require_pdf_reader()
    try:
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            return False
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                return False
            stream.seek(0)
            reader = reader_class(stream, strict=False)
            if reader.is_encrypted or not reader.pages:
                return False
            pages = [page.extract_text() or "" for page in reader.pages]
            text = "\n".join(pages)
        if len(re.sub(r"\s", "", text)) < 800:
            return False
        doi = normalize_doi(meta.get("doi", ""))
        # 身份检查集中在首页，不能拿参考文献中引用的题名/DOI 证明身份。
        header = pages[0][:2500]
        doi_tokens = {normalize_doi(value).rstrip(".,;") for value in re.findall(
            r"(?i)\b10\.\d{4,9}/[^\s<>\"']+", header)}
        header_dois = re.findall(
            r"(?i)(?:\bdoi\s*:\s*(?:https?://(?:dx\.)?doi\.org/)?|"
            r"https?://(?:dx\.)?doi\.org/)(10\.\d{4,9}/[^\s<>\"']+)", header)
        if doi and header_dois and doi not in {
                normalize_doi(value).rstrip(".,;") for value in header_dois}:
            return False
        header_pmids = re.findall(r"(?i)\bPMID\s*:\s*(\d+)", header)
        if header_pmids and str(meta["pmid"]) not in header_pmids:
            return False
        if not (title_matches(meta.get("title", ""), header)
                or (doi and doi in doi_tokens)):
            return False
        # 结构化摘要也可能有 Methods/Results，必须同时看到正文与参考文献。
        body_heading = re.search(
            r"(?im)^\s*(?:\d+(?:\.\d+)*[.)]?\s*)?"
            r"(?:introduction|materials(?:\s+and\s+methods)?|methods|methodology|"
            r"results|discussion|case\s+(?:report|presentation))\b", text)
        references = re.search(
            r"(?im)^\s*(?:\d+[.)]?\s*)?(?:references|bibliography|literature cited)\s*[:.]?\s*$",
            text)
        if not body_heading or not references:
            return False
        if expected_body:
            wanted = {w for w in words(expected_body) if len(w) > 2}
            if not wanted or len(wanted & set(words(text))) / len(wanted) < 0.8:
                return False
        return True
    except Exception:
        return False


def build_query(topic):
    tokens = [w for w in re.split(r"[,，;；\s]+", topic.strip()) if w]
    if not tokens:
        raise ValueError("主题不能为空。")
    if any(re.search(r"[\[\]()\"]", token) for token in tokens):
        raise ValueError("--topic 只接受主题关键词，不接受完整布尔检索式。")
    return " AND ".join(
        "(%s[Title/Abstract] OR %s[MeSH Terms])" % (token, token) for token in tokens)


def search_topic(topic, max_n):
    query = build_query(topic)
    ids, start, total = [], 0, 0
    try:
        while True:
            params = urllib.parse.urlencode({
                "db": "pubmed", "term": query, "retmax": min(200, max_n - len(ids)),
                "retstart": start, "sort": "relevance", "retmode": "json"})
            result = jget(EUTILS + "esearch.fcgi?" + params)["esearchresult"]
            total = int(result.get("count", 0))
            batch = result.get("idlist", [])
            ids.extend(batch)
            if not batch or len(ids) >= min(total, max_n):
                break
            start += len(batch)
            time.sleep(0.35)
        meta = {}
        for offset in range(0, len(ids), 100):
            params = urllib.parse.urlencode({
                "db": "pubmed", "id": ",".join(ids[offset:offset + 100]), "retmode": "json"})
            result = jget(EUTILS + "esummary.fcgi?" + params)["result"]
            for pmid in result.get("uids", []):
                if not re.fullmatch(r"\d+", str(pmid)):
                    continue
                item = result[pmid]
                doi = next((x["value"] for x in item.get("articleids", [])
                            if x.get("idtype") == "doi"), "")
                meta[pmid] = {
                    "pmid": pmid, "title": item.get("title", "").rstrip("."),
                    "journal": item.get("source", ""), "pubdate": item.get("pubdate", ""),
                    "doi": normalize_doi(doi)}
            time.sleep(0.35)
        return query, total, meta
    except Exception as exc:
        raise RuntimeError("PubMed 检索请求未成功，可检查网络后使用相同参数恢复。") from exc


def prefetch_pmcids(pmids, chunk=50):
    """批量获取 PMID→PMCID 映射，仅用于提前筛掉无 PMC 全文的候选。

    Europe PMC 一条 OR 查询可携带约 50 个 PMID，比逐篇查询快一个数量级。
    批量接口不校验 DOI，因此命中后仍由 fetch_one 复核身份。
    """
    mapping = {}
    for offset in range(0, len(pmids), chunk):
        batch = [p for p in pmids[offset:offset + chunk] if p not in mapping]
        if not batch:
            continue
        query = " OR ".join("EXT_ID:%s" % p for p in batch)
        url = EPMC + "search?" + urllib.parse.urlencode({
            "query": query, "format": "json", "resultType": "lite", "pageSize": len(batch) + 10})
        try:
            data = jget(url, timeout=30)
        except Exception:
            continue  # 单批失败不阻塞任务；相关文献由 fetch_one 单篇兜底
        for result in (data.get("resultList") or {}).get("result") or []:
            pmid = str(result.get("id", ""))
            pmcid = str(result.get("pmcid", "")).upper()
            if pmid and re.fullmatch(r"PMC\d+", pmcid):
                mapping[pmid] = pmcid
        time.sleep(0.2)
    return mapping


def parse_xml(value):
    root = ET.fromstring(value)
    # JATS 可能有默认命名空间；属性命名空间保留供 xlink 使用。
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    return root


def txt_of(node):
    return re.sub(r"\s+", " ", " ".join(node.itertext())).strip() if node is not None else ""


def xml_identity_matches(root, meta, pmcid):
    article_meta = root.find("./front/article-meta")
    if article_meta is None:
        return False
    matched = False
    expected = {"pmid": str(meta["pmid"]), "pmcid": pmcid.upper(),
                "doi": normalize_doi(meta.get("doi", ""))}
    for node in article_meta.findall("article-id"):
        kind = node.get("pub-id-type", "").lower()
        kind = {"pubmed": "pmid", "pmc": "pmcid"}.get(kind, kind)
        if kind not in expected or not expected[kind]:
            continue
        actual = txt_of(node)
        if kind == "doi":
            actual = normalize_doi(actual)
        if kind == "pmcid":
            actual = "PMC" + actual.upper().removeprefix("PMC")
        if actual != expected[kind]:
            return False
        matched = True
    return matched or title_matches(meta.get("title", ""), txt_of(article_meta.find(".//article-title")))


CSS = """@page{size:A4;margin:18mm 15mm}
body{font:10.5pt/1.6 Georgia,"Times New Roman",serif;color:#111}
h1{font-size:16pt}h2{font-size:13pt}h3{font-size:11pt}
p{margin:0 0 7pt}table{border-collapse:collapse;width:100%;font-size:9pt}
td,th{border:1px solid #999;padding:4pt;vertical-align:top}
.meta{font-size:9pt;color:#444}.abstract{background:#f5f5f5;padding:8pt}
li{margin-bottom:4pt}"""

SUPPORTED_BODY_TAGS = {
    "body", "sec", "title", "p", "italic", "bold", "i", "b", "em", "strong",
    "sup", "sub", "underline", "sc", "monospace", "ext-link", "uri", "email",
    "xref", "named-content", "styled-content", "break", "list", "list-item",
    "label", "def-list", "def-item", "term", "def", "disp-quote", "attrib",
    "table-wrap", "caption", "table", "thead", "tbody", "tfoot", "tr", "td",
    "th", "table-wrap-foot", "fn", "fn-group", "boxed-text",
}


def render_node(node, level=2):
    tag = node.tag
    content = html.escape(node.text or "")
    for child in node:
        content += render_node(child, min(level + (tag == "sec"), 4))
        content += html.escape(child.tail or "")
    mapping = {
        "body": "div", "sec": "section", "title": "h%d" % level, "p": "p",
        "italic": "em", "i": "em", "em": "em", "bold": "strong", "b": "strong",
        "strong": "strong", "sup": "sup", "sub": "sub", "underline": "u",
        "monospace": "code", "list-item": "li", "disp-quote": "blockquote",
        "table": "table", "thead": "thead", "tbody": "tbody", "tfoot": "tfoot",
        "tr": "tr", "td": "td", "th": "th",
    }
    if tag == "break":
        return "<br>"
    if tag == "list":
        wrapper = "ol" if node.get("list-type") in ("order", "ordered", "alpha-lower",
                                                   "alpha-upper", "roman-lower", "roman-upper") else "ul"
    else:
        wrapper = mapping.get(tag, "div" if tag in (
            "table-wrap", "caption", "table-wrap-foot", "fn", "fn-group", "boxed-text",
            "def-list", "def-item", "def") else "span")
    attributes = ""
    if tag in ("td", "th"):
        for key in ("rowspan", "colspan"):
            value = node.get(key, "")
            if value.isdigit():
                attributes += ' %s="%s"' % (key, value)
    return "<%s%s>%s</%s>" % (wrapper, attributes, content, wrapper)


def render_xml_to_html(value, meta, pmcid):
    try:
        root = parse_xml(value)
        if root.tag != "article" or not xml_identity_matches(root, meta, pmcid):
            return None
        if any(child.tag not in {"front", "body", "back"} for child in root):
            return None
        body = root.find("body")
        if body is None or len(re.sub(r"\s", "", txt_of(body))) < 400:
            return None
        # 未支持的图、公式、媒体和结构不得静默丢弃后冒充完整正文。
        if any(node.tag not in SUPPORTED_BODY_TAGS for node in body.iter()):
            return None
        back = root.find("back")
        unsupported = {"graphic", "inline-graphic", "inline-formula", "disp-formula",
                       "math", "media", "alternatives", "chem-struct"}
        if any(node.tag in unsupported for node in root.iter()):
            return None
        identifiers = {node.get("id") for node in root.iter() if node.get("id")}
        for link in body.iter("xref"):
            if link.get("ref-type") in ("fig", "table", "disp-formula"):
                if not link.get("rid") or any(rid not in identifiers for rid in link.get("rid").split()):
                    return None
        title = txt_of(root.find("./front/article-meta//article-title")) or meta["title"]
        abstract_html = "".join('<div class="abstract">%s</div>' % html.escape(txt_of(abstract))
                                for abstract in root.findall("./front/article-meta/abstract"))
        refs = root.findall("./back//ref")
        references = ("<h2>References</h2><ol>" + "".join(
            "<li>%s</li>" % html.escape(txt_of(ref)) for ref in refs) + "</ol>") if refs else ""
        # 致谢、附录、利益冲突等正文后内容也需保留。
        back_html = "".join(render_node(child) for child in back
                            if child.tag != "ref-list") if back is not None else ""
        return ('<!doctype html><html><head><meta charset="utf-8"><style>%s</style>'
                '</head><body><h1>%s</h1><p class="meta">%s | %s | PMID %s | DOI %s'
                ' | XML full text from Europe PMC</p>%s%s%s</body></html>') % (
                    CSS, html.escape(title), html.escape(meta.get("journal", "")),
                    html.escape(meta.get("pubdate", "")), html.escape(str(meta["pmid"])),
                    html.escape(meta.get("doi", "")), abstract_html, render_node(body),
                    back_html + references)
    except (ET.ParseError, ValueError, TypeError):
        return None


def chrome_pdf(html_path, out_pdf):
    if not CHROME:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix="litdl-chrome-") as profile:
            result = subprocess.run([
                CHROME, "--headless", "--disable-gpu", "--no-sandbox",
                "--user-data-dir=" + profile, "--no-pdf-header-footer",
                "--print-to-pdf=" + str(Path(out_pdf).resolve()),
                Path(html_path).resolve().as_uri(),
            ], timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0 and Path(out_pdf).is_file()
    except (OSError, subprocess.TimeoutExpired):
        return False


def s3_pdf_urls(pmcid, meta):
    """PMC 官方公开 S3 元数据映射；不猜版本号，不信任元数据中的外部主机。"""
    prefixes, token = [], ""
    for _ in range(10):
        params = {"list-type": "2", "prefix": pmcid + ".", "delimiter": "/"}
        if token:
            params["continuation-token"] = token
        root = ET.fromstring(get(PMC_S3 + "?" + urllib.parse.urlencode(params)))
        ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
        prefixes.extend(node.text for node in root.findall("s:CommonPrefixes/s:Prefix", ns)
                        if node.text and re.fullmatch(re.escape(pmcid) + r"\.\d+/", node.text))
        if root.findtext("s:IsTruncated", "false", ns).lower() != "true":
            break
        token = root.findtext("s:NextContinuationToken", "", ns)
        if not token:
            break
    matches = []
    for prefix in sorted(set(prefixes), key=lambda x: int(x.rstrip("/").split(".")[-1]), reverse=True)[:30]:
        try:
            data = jget(PMC_S3 + "metadata/" + prefix.rstrip("/") + ".json")
            is_open = str(data.get("is_pmc_openaccess", "")).lower() in ("true", "yes")
            is_manuscript = str(data.get("is_manuscript", "")).lower() in ("true", "yes")
            if (str(data.get("pmid", "")) != str(meta["pmid"])
                    or str(data.get("pmcid", "")).upper() != pmcid
                    or not (is_open or is_manuscript)):
                continue
            if meta.get("doi") and data.get("doi") and normalize_doi(data["doi"]) != normalize_doi(meta["doi"]):
                continue
            parsed = urllib.parse.urlsplit(data.get("pdf_url", ""))
            if parsed.scheme != "s3" or parsed.netloc != "pmc-oa-opendata":
                continue
            if not parsed.path.startswith("/" + prefix) or not parsed.path.lower().endswith(".pdf"):
                continue
            checksum = urllib.parse.parse_qs(parsed.query).get("md5", [""])[0]
            if not re.fullmatch(r"[0-9a-fA-F]{32}", checksum):
                continue
            matches.append((is_manuscript, PMC_S3.rstrip("/") + parsed.path + "?md5=" + checksum.lower()))
        except Exception:
            continue
    return [url for _, url in sorted(matches, key=lambda pair: pair[0])]


def fetch_one(item):
    pmid, meta, tmpdir, paperdir = item
    info = {"pmid": pmid, "status": "", "kind": "", "file": ""}
    pdf_path = Path(paperdir) / (pmid + ".pdf")
    try:
        pmcid = PMCid_CACHE.get(pmid, "")
        if not pmcid:
            query = urllib.parse.urlencode({
                "query": "EXT_ID:%s AND SRC:MED" % pmid, "format": "json", "resultType": "core"})
            results = (jget(EPMC + "search?" + query, timeout=20).get("resultList") or {}).get("result") or []
            result = next((r for r in results if str(r.get("id", r.get("pmid", ""))) == pmid
                           and r.get("source") == "MED"), None)
            if not result:
                return info
            if meta.get("doi") and result.get("doi") and normalize_doi(meta["doi"]) != normalize_doi(result["doi"]):
                return info
            pmcid = result.get("pmcid", "").upper()
            if not re.fullmatch(r"PMC\d+", pmcid):
                PMCid_CACHE[pmid] = ""
                return info
            PMCid_CACHE[pmid] = pmcid
        info["pmcid"] = pmcid
        try:
            urls = s3_pdf_urls(pmcid, meta)
        except Exception:
            urls = []
        for url in urls:
            try:
                data = get(url, binary=True, timeout=45)
                checksum = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["md5"][0]
                if hashlib.md5(data).hexdigest() != checksum:
                    continue
                pdf_path.write_bytes(data)
                if validate_pdf(pdf_path, meta):
                    return dict(info, status="全文(原版PDF)", kind="全文", file=pdf_path.name)
            except Exception:
                pass
            pdf_path.unlink(missing_ok=True)
        if CHROME:
            xml = get(EPMC + pmcid + "/fullTextXML", timeout=45)
            rendered = render_xml_to_html(xml, meta, pmcid)
            if rendered:
                html_path = Path(tmpdir) / (pmid + ".html")
                html_path.write_text(rendered, encoding="utf-8")
                try:
                    if chrome_pdf(html_path, pdf_path) and validate_pdf(
                            pdf_path, meta, expected_body=txt_of(parse_xml(xml).find("body"))):
                        return dict(info, status="全文(XML渲染)", kind="全文", file=pdf_path.name)
                finally:
                    html_path.unlink(missing_ok=True)
    except Exception:
        pass
    pdf_path.unlink(missing_ok=True)
    return info


def csv_row(record):
    return [
        record["status"], str(record["pmid"]), year_of(record), record.get("journal", ""),
        record["title"], normalize_doi(record.get("doi", "")), "论文/" + record["file"],
        "；".join(record["sources"]),
    ]


def md_cell(value):
    return html.escape(str(value)).replace("|", "&#124;").replace("\r", " ").replace("\n", " ")


def render_markdown(records, report):
    config = report["config"]
    lines = ["# 文献清单", "", "## 研究范围", "",
             md_cell(config["scope"] or "；".join(config["topics"])), "", "## 检索记录", "",
             "检索日期：%s（本地时间）" % report["date"],
             "数据库：PubMed；全文来源：PMC 官方开放全文、Europe PMC。",
             "每个主题候选上限：%d；该上限不是保证取得的全文数量。" % config["max_n"], "",
             "| 主题 | 实际检索式 |", "|---|---|"]
    for query in report["queries"]:
        lines.append("| %s | %s |" % (md_cell(query["topic"]), md_cell(query["query"])))
    original = sum(r["status"] == "全文(原版PDF)" for r in records)
    lines += ["", "## 全文汇总", "", "| 指标 | 数量 |", "|---|---|",
              "| 全文总数 | %d |" % len(records), "| 原版 PDF | %d |" % original,
              "| XML 渲染全文 | %d |" % (len(records) - original), "",
              "## 全文文献", "", "| " + " | ".join(CSV_FIELDS) + " |",
              "|" + "|".join("---" for _ in CSV_FIELDS) + "|"]
    for record in records:
        row = [md_cell(value) for value in csv_row(record)]
        row[6] = "[PDF](%s)" % urllib.parse.quote("论文/" + record["file"], safe="/")
        lines.append("| " + " | ".join(row) + " |")
    if not records:
        lines += ["", "本次获得全文 0 篇。"]
    lines += ["", "## 说明", ""] + REPORT_NOTES
    for note in report.get("notes", []):
        lines.append(md_cell(note))
    return "\n".join(lines) + "\n"


def read_report_sections(outdir):
    """仅读取可见章节；兼容跳过旧注释，不解析或依赖其中的 JSON。"""
    text = (Path(outdir) / "文献清单.md").read_text(encoding="utf-8-sig").lstrip("\n")
    if text.startswith(LEGACY_MARKER):
        first, newline, text = text.partition("\n")
        if not newline or not first.endswith(" -->"):
            raise ValueError("旧清单注释不完整，请核对文献清单。")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "# 文献清单":
        raise ValueError("文献清单应以标题开始，并包含固定的可读章节。")
    headings = list(re.finditer(r"(?m)^##[ \t]+(.+?)[ \t]*$", text))
    if [match.group(1) for match in headings] != REPORT_SECTIONS:
        raise ValueError("文献清单的章节或顺序不符合固定规范。")
    return {
        match.group(1): text[match.end():headings[index + 1].start()
                            if index + 1 < len(headings) else len(text)].strip()
        for index, match in enumerate(headings)
    }


def read_table(section, fields):
    lines = [line.strip() for line in section.splitlines() if line.strip().startswith("|")]
    rows = []
    for line in lines:
        if not line.endswith("|"):
            raise ValueError("清单表格格式不完整。")
        rows.append([html.unescape(cell.strip()) for cell in line[1:-1].split("|")])
    if (len(rows) < 2 or rows[0] != fields or len(rows[1]) != len(fields)
            or any(not re.fullmatch(r":?-{3,}:?", cell) for cell in rows[1])):
        raise ValueError("清单表格字段不符合固定规范。")
    if any(len(row) != len(fields) for row in rows[2:]):
        raise ValueError("清单表格包含缺失或多余字段。")
    return rows[2:]


def read_report(outdir):
    sections = read_report_sections(outdir)
    search = sections["检索记录"]
    date = re.search(r"(?m)^检索日期[：:]\s*(.+?)(?:（本地时间）)?[ \t]*$", search)
    maximum = re.search(r"(?m)^每个主题候选上限[：:]\s*(\d+)", search)
    queries = [{"topic": row[0], "query": row[1]}
               for row in read_table(search, ["主题", "实际检索式"])]
    if (not date or not maximum or int(maximum.group(1)) < 1 or not queries
            or any(not query["topic"] or not query["query"] for query in queries)
            or len({query["topic"] for query in queries}) != len(queries)):
        raise ValueError("清单缺少有效的检索日期、候选上限或检索记录。")
    notes = [html.unescape(line.strip()) for line in sections["说明"].splitlines()
             if line.strip() and html.unescape(line.strip()) not in REPORT_NOTES]
    return {"scope": html.unescape(sections["研究范围"]), "date": date.group(1),
            "max_n": int(maximum.group(1)), "queries": queries, "notes": notes}


def read_index(outdir):
    """CSV 是已交付题录的唯一来源；不从 Markdown 的隐藏副本恢复文献。"""
    with (Path(outdir) / "文献索引.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    if not rows or rows[0] != CSV_FIELDS:
        raise ValueError("文献索引字段或顺序不符合固定规范。")
    records = []
    for row in rows[1:]:
        if len(row) != len(CSV_FIELDS):
            raise ValueError("文献索引存在缺失或多余字段。")
        status, pmid, year, journal, title, doi, relative, source = row
        if year and not re.fullmatch(r"(?:19|20)\d{2}", year):
            raise ValueError("文献索引的发表年份格式不符合规范。")
        if not relative.startswith("论文/"):
            raise ValueError("文献路径必须相对于任务内的论文目录。")
        filename = relative[len("论文/"):]
        if not filename or Path(filename).name != filename or "\\" in filename or "/" in filename:
            raise ValueError("文献路径不符合固定规范。")
        sources = source.split("；") if source else []
        if (not title.strip() or any(not item.strip() for item in sources)
                or len(set(sources)) != len(sources)):
            raise ValueError("文献题名或检索来源不符合规范。")
        record = {"status": status, "pmid": pmid, "pubdate": year, "journal": journal,
                  "title": title, "doi": normalize_doi(doi), "file": filename, "sources": sources}
        if csv_row(record) != row:
            raise ValueError("文献索引的数据格式不符合固定规范。")
        records.append(record)
    return records


def validate_report(outdir, records):
    sections = read_report_sections(outdir)
    report = read_report(outdir)
    actual = read_table(sections["全文文献"], CSV_FIELDS)
    if len(actual) != len(records):
        raise ValueError("清单全文条数与文献索引不一致。")
    known_topics = {query["topic"] for query in report["queries"]}
    for row, record in zip(actual, records):
        link = re.fullmatch(r"\[[^\]]+\]\(([^)]+)\)", row[6])
        if not link or urllib.parse.unquote(link.group(1)) != "论文/" + record["file"]:
            raise ValueError("清单中的 PDF 链接与文献索引不一致。")
        row[6] = urllib.parse.unquote(link.group(1))
        normalize = lambda value: re.sub(r"\s+", " ", str(value)).strip()
        if [normalize(value) for value in row] != [normalize(value) for value in csv_row(record)]:
            raise ValueError("清单中的全文题录与文献索引不一致。")
        if not set(record["sources"]).issubset(known_topics):
            raise ValueError("文献检索来源缺少对应的可读检索记录。")
    summary = read_table(sections["全文汇总"], ["指标", "数量"])
    original = sum(record["status"] == "全文(原版PDF)" for record in records)
    expected = {"全文总数": str(len(records)), "原版 PDF": str(original),
                "XML 渲染全文": str(len(records) - original)}
    if len(summary) != len(expected) or dict(summary) != expected:
        raise ValueError("清单汇总数量与成功全文记录不一致。")
    return report


def validate_output(outdir):
    outdir = Path(outdir)
    try:
        if outdir.is_symlink() or {p.name for p in outdir.iterdir()} != OUTPUT_NAMES:
            raise ValueError("交付目录必须只含文献清单、文献索引和论文目录。")
        if any((outdir / name).is_symlink() for name in OUTPUT_NAMES):
            raise ValueError("交付目录不允许符号链接。")
        records = read_index(outdir)
        seen_pmids, seen_dois, filenames = set(), set(), set()
        for record in records:
            pmid = str(record["pmid"])
            doi = normalize_doi(record.get("doi", ""))
            if not re.fullmatch(r"\d+", pmid) or pmid in seen_pmids or (doi and doi in seen_dois):
                raise ValueError("索引存在重复或无效的文献标识。")
            if record["status"] not in STATUSES or not record["sources"]:
                raise ValueError("成功记录的类型或检索来源不符合规范。")
            expected_name = pdf_name(record, STATUSES[record["status"]])
            if record["file"] != expected_name:
                raise ValueError("PDF 文件名不符合固定规范。")
            if not validate_pdf(outdir / "论文" / expected_name, record):
                raise ValueError("交付中存在未通过全文验证的 PDF。")
            seen_pmids.add(pmid)
            if doi:
                seen_dois.add(doi)
            filenames.add(expected_name)
        papers = outdir / "论文"
        if not papers.is_dir() or {p.name for p in papers.iterdir()} != filenames:
            raise ValueError("PDF 与成功记录未一一对应。")
        validate_report(outdir, records)
        return records
    except (OSError, KeyError, TypeError, json.JSONDecodeError, IndexError) as exc:
        raise ValueError("已有产物不符合固定规范，请使用新的输出目录。") from exc


def checkpoint_dir(outdir):
    digest = hashlib.sha256(os.path.normcase(str(Path(outdir).resolve())).encode("utf-8")).hexdigest()[:24]
    return Path(tempfile.gettempdir()) / "literature-download-v2" / digest


def save_state(workspace, state):
    temporary = workspace / "state.json.part"
    temporary.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, workspace / "state.json")


def remove_workspace(workspace):
    base = (Path(tempfile.gettempdir()) / "literature-download-v2").resolve()
    if workspace.is_symlink() or workspace.resolve().parent != base:
        raise ValueError("临时目录路径异常。")
    shutil.rmtree(workspace)


def remove_publication_dir(path, outdir):
    path, outdir = Path(path), Path(outdir)
    if (path.is_symlink() or path.resolve().parent != outdir.resolve().parent
            or not path.name.startswith(".literature-download-")):
        raise ValueError("发布临时目录路径异常。")
    if path.exists():
        shutil.rmtree(path)


def recover_publication(outdir, workspace, state):
    publication = state.get("publication")
    if not publication:
        return
    backup = Path(publication["backup"])
    stage = Path(publication["stage"])
    # 检查计算后的绝对路径，再做任何移动或递归清理。
    for path in (backup, stage):
        if (path.is_symlink() or path.resolve().parent != outdir.resolve().parent
                or not path.name.startswith(".literature-download-")):
            raise ValueError("发布检查点路径异常。")
    if backup.exists():
        if outdir.exists():
            validate_output(outdir)
            remove_publication_dir(backup, outdir)
        else:
            if any(backup.iterdir()):
                validate_output(backup)
            backup.rename(outdir)
    if stage.exists():
        remove_publication_dir(stage, outdir)
    state.pop("publication", None)
    save_state(workspace, state)


def publish(outdir, workspace, state, records, report):
    outdir.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    stage = outdir.parent / (".literature-download-stage-" + token)
    backup = outdir.parent / (".literature-download-backup-" + token)
    state["publication"] = {"stage": str(stage), "backup": str(backup)}
    save_state(workspace, state)
    try:
        (stage / "论文").mkdir(parents=True)
        for record in records:
            shutil.copy2(workspace / "papers" / record["cache_file"], stage / "论文" / record["file"])
        (stage / "文献清单.md").write_text(render_markdown(records, report), encoding="utf-8")
        with (stage / "文献索引.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(CSV_FIELDS)
            writer.writerows(csv_row(record) for record in records)
        validate_output(stage)
        if outdir.exists():
            if any(outdir.iterdir()):
                validate_output(outdir)
            outdir.rename(backup)
        stage.rename(outdir)
        if backup.exists():
            remove_publication_dir(backup, outdir)
        state.pop("publication", None)
        save_state(workspace, state)
    except BaseException:
        if backup.exists() and not outdir.exists():
            backup.rename(outdir)
        if stage.exists():
            remove_publication_dir(stage, outdir)
        raise


def dedupe_records(records):
    # 全部全文候选先通过验证；按类型优先级选择，同一 DOI 的其他 PMID 可作为获取后备。
    ordered = sorted(records, key=lambda r: (
        r["status"] != "全文(原版PDF)", r["pmid"]))
    chosen, doi_map, pmid_map = [], {}, {}
    for record in ordered:
        doi = normalize_doi(record.get("doi", ""))
        existing = pmid_map.get(record["pmid"]) or (doi_map.get(doi) if doi else None)
        if existing is not None:
            existing["sources"] = list(dict.fromkeys(existing["sources"] + record["sources"]))
            continue
        chosen.append(dict(record, sources=list(record["sources"])))
        pmid_map[record["pmid"]] = chosen[-1]
        if doi:
            doi_map[doi] = chosen[-1]
    return sorted(chosen, key=lambda r: (year_of(r), r["pmid"]), reverse=True)


def fetch_group(items):
    """同 DOI 候选按顺序尝试；原版成功后停止，避免重复下载。"""
    best = None
    for item in items:
        info = fetch_one(item)
        filename = info.get("file", "")
        if (info.get("kind") != "全文" or info.get("status") not in STATUSES
                or not filename or Path(filename).name != filename
                or not validate_pdf(Path(item[3]) / filename, item[1])):
            continue
        if info["status"] == "全文(原版PDF)":
            return info
        best = best or info
    return best or {"pmid": "", "status": "", "kind": "", "file": ""}


def run(topics, max_n, outdir, scope="", resume=False):
    require_pdf_reader()
    if isinstance(topics, str):
        topics = [topics]
    topics = list(dict.fromkeys(str(topic).strip() for topic in topics if str(topic).strip()))
    if not topics or max_n < 1:
        raise ValueError("至少提供一个主题，且候选上限必须大于 0。")
    for topic in topics:
        build_query(topic)
    outdir = Path(outdir).absolute()
    if outdir.is_symlink():
        raise ValueError("输出目录不能是符号链接。")
    config = {"topics": topics, "max_n": max_n, "scope": scope, "outdir": str(outdir.resolve())}
    workspace = checkpoint_dir(outdir)
    state_path = workspace / "state.json"
    state = None
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state["config"] != config:
            raise ValueError("检查点参数与本次任务不一致，请使用原参数恢复或选择新的输出目录。")
        if not resume:
            raise ValueError("存在未完成任务，请使用相同参数并添加 --resume。")
        recover_publication(outdir, workspace, state)
    existing, prior_report = [], {}
    if outdir.exists():
        if not outdir.is_dir():
            raise ValueError("输出路径不是目录。")
        if any(outdir.iterdir()):
            existing = validate_output(outdir)
            prior_report = read_report(outdir)
            if resume and not state:
                print("任务已完成，现有全文、索引和清单已重新核对。")
                return existing
    if resume and not state:
        raise ValueError("没有找到本次任务的临时检查点。")
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "papers").mkdir(exist_ok=True)
    (workspace / "html").mkdir(exist_ok=True)
    if state is None:
        state = {"config": config, "date": time.strftime("%Y-%m-%d %H:%M"),
                 "queries": [], "prior_queries": prior_report.get("queries", []), "successes": {}}
        save_state(workspace, state)
    # 已交付成功全文可复用；所有身份和文件再次通过验证后才进入新任务。
    reusable = {}
    for record in existing:
        cache_file = record["pmid"] + ".pdf"
        shutil.copy2(outdir / "论文" / record["file"], workspace / "papers" / cache_file)
        reusable[record["pmid"]] = dict(record, cache_file=cache_file)
    for topic in topics:
        if not any(q["topic"] == topic for q in state["queries"]):
            query, total, meta = search_topic(topic, max_n)
            state["queries"].append({"topic": topic, "query": query, "count": total, "meta": meta})
            save_state(workspace, state)
    candidates = {}
    for query in state["queries"]:
        for pmid, meta in query["meta"].items():
            if not re.fullmatch(r"\d+", str(pmid)):
                continue
            if pmid not in candidates:
                candidates[pmid] = dict(meta, pmid=str(pmid), sources=[])
            if query["topic"] not in candidates[pmid]["sources"]:
                candidates[pmid]["sources"].append(query["topic"])
    # 同一任务目录的补充检索保留已经交付的全文及其来源。
    for record in existing:
        pmid = record["pmid"]
        if pmid not in candidates:
            candidates[pmid] = {key: record[key] for key in (
                "pmid", "title", "journal", "pubdate", "doi", "sources")}
        else:
            candidates[pmid]["sources"] = list(dict.fromkeys(
                record["sources"] + candidates[pmid]["sources"]))
    successes = {}
    for pmid, meta in candidates.items():
        old = state["successes"].get(pmid) or reusable.get(pmid)
        if (old and old.get("status") in STATUSES
                and Path(old.get("cache_file", "")).name == old.get("cache_file")
                and validate_pdf(workspace / "papers" / old["cache_file"], meta)):
            successes[pmid] = dict(meta, status=old["status"], cache_file=old["cache_file"])
    state["successes"] = successes
    save_state(workspace, state)
    groups = {}
    for pmid, meta in candidates.items():
        key = "doi:" + normalize_doi(meta["doi"]) if meta.get("doi") else "pmid:" + pmid
        groups.setdefault(key, []).append((pmid, meta, str(workspace / "html"),
                                          str(workspace / "papers")))
    pending = []
    for items in groups.values():
        if any(successes.get(item[0], {}).get("status") == "全文(原版PDF)" for item in items):
            continue
        missing = [item for item in items if item[0] not in successes]
        if missing:
            pending.append(missing)
    pending_pmids = [item[0] for items in pending for item in items]
    if pending_pmids:
        # 先批量筛掉无 PMC 收录的候选，避免逐篇发起注定失败的取全文请求。
        preloaded = set(PMCid_CACHE)
        PMCid_CACHE.update(prefetch_pmcids(sorted(set(pending_pmids) - preloaded)))
        print("候选 %d 篇待取全文；批量预取完成，其中 %d 篇有 PMC 收录。" % (
            len(pending_pmids), sum(1 for p in pending_pmids if PMCid_CACHE.get(p))), flush=True)
    print("检索完成，正在获取并验证全文。", flush=True)
    fetched, total_pending = 0, len(pending)
    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        for info in executor.map(fetch_group, pending):
            fetched += 1
            pmid = info.get("pmid")
            filename = info.get("file", "")
            if (pmid in candidates and info.get("kind") == "全文"
                    and info.get("status") in STATUSES and filename
                    and Path(filename).name == filename
                    and validate_pdf(workspace / "papers" / filename, candidates[pmid])):
                successes[pmid] = dict(candidates[pmid], status=info["status"],
                                      cache_file=filename)
                state["successes"] = successes
                save_state(workspace, state)
            if fetched % 10 == 0 or fetched == total_pending:
                print("进度 %d/%d 组，已获全文 %d 篇。" % (fetched, total_pending, len(successes)), flush=True)
    records = dedupe_records(list(successes.values()))
    # DOI 相同但全文不可得的其他候选也贡献检索来源，不生成额外文献条目。
    by_doi = {}
    for meta in candidates.values():
        doi = normalize_doi(meta.get("doi", ""))
        if doi:
            by_doi[doi] = list(dict.fromkeys(by_doi.get(doi, []) + meta["sources"]))
    for record in records:
        doi = normalize_doi(record.get("doi", ""))
        record["sources"] = list(dict.fromkeys(record["sources"] + by_doi.get(doi, [])))
        record["file"] = pdf_name(record, STATUSES[record["status"]])
    queries = {q["topic"]: {"topic": q["topic"], "query": q["query"]}
               for q in state.get("prior_queries", []) + state["queries"]}
    notes = list(dict.fromkeys(prior_report.get("notes", []) + (
        [] if CHROME else ["当前环境缺少 Chrome/Edge，仅尝试交付原版全文 PDF。"])))
    report = {
        "config": dict(config, scope=scope or prior_report.get("scope", "")), "date": state["date"],
        "queries": list(queries.values()),
        "notes": notes,
    }
    publish(outdir, workspace, state, records, report)
    result = validate_output(outdir)
    remove_workspace(workspace)
    print("已交付全文 %d 篇；文献清单、索引和 PDF 已核对一致。" % len(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topic", action="append", required=True, help="主题关键词，可重复指定")
    parser.add_argument("--max", type=int, default=100, help="每个主题的最大候选篇数")
    parser.add_argument("--scope", default="", help="研究范围，可用中文")
    parser.add_argument("--out", help="任务根目录")
    parser.add_argument("--resume", action="store_true", help="使用相同参数恢复未完成任务")
    parser.add_argument("--skip", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.skip is not None:
        parser.error("--skip 已停用，请使用相同任务参数和 --resume；旧版产物请另行迁移。")
    outdir = args.out or str(Path.cwd() / (time.strftime("%Y%m%d_%H%M%S") + "_相关文献"))
    try:
        run(args.topic, args.max, outdir, scope=args.scope, resume=args.resume)
    except (Exception, KeyboardInterrupt) as exc:
        # 不输出含候选标识符的网络堆栈或逐篇失败名单。
        message = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "任务中断，检查后使用相同参数和 --resume 恢复。"
        print("任务未完成：" + message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
