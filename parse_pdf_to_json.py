"""Turn KNF exam PDFs into JSON files the upload page accepts.

Usage:
    python parse_pdf_to_json.py "jakis_test.pdf"      # one file
    python parse_pdf_to_json.py tests                 # every PDF in a folder

Writes <same-name>.json next to each PDF, then you pick those files in
index.html under "Wgraj inny test".

The answer key at the end of these PDFs comes in three different layouts, so
the key is found by shape (rows of number + letter) rather than by any header
text, which varies between years.

Requires pdftotext (from poppler) on PATH.
"""
import io
import json
import os
import re
import subprocess
import sys

MONTHS = {
    "stycznia": 1, "lutego": 2, "marca": 3, "kwietnia": 4,
    "maja": 5, "czerwca": 6, "lipca": 7, "sierpnia": 8,
    "wrzesnia": 9, "września": 9, "pazdziernika": 10, "października": 10,
    "listopada": 11, "grudnia": 12,
}

# --- answer-key row shapes -------------------------------------------------
RE_PAIRS = re.compile(r"^(?:\d{1,3}\s+[A-D]\s*)+$")      # "1 D"  /  "9 B 39 C 69 A"
RE_MULTI = re.compile(r"^(\d{1,3})\s+([A-D])(?:\s+[A-D])+$")   # "110 D A A"
RE_ORPHAN = re.compile(r"^[A-D](?:\s+[A-D])+$")          # "D C A" - number lost by pdftotext


def pdf_to_text(pdf_path, mode="-layout"):
    txt_path = os.path.splitext(pdf_path)[0] + ".__tmp.txt"
    subprocess.check_call(["pdftotext", mode, "-enc", "UTF-8", pdf_path, txt_path])
    text = io.open(txt_path, encoding="utf-8").read()
    os.remove(txt_path)
    return text


def key_from_raw(pdf_path):
    """Second opinion on the answer key.

    Where a key row sits on a page boundary, -layout sometimes drops the
    letter and leaves a bare number. -raw linearises the table and usually
    keeps it. Multi-column tables come out scrambled in -raw though, so this
    is only ever used to fill gaps, and only after the two agree everywhere
    they overlap.
    """
    try:
        lines = pdf_to_text(pdf_path, "-raw").replace(chr(12), chr(10)).split(chr(10))
    except Exception:
        return {}
    ki = find_key_region(lines)
    if ki is None:
        return {}
    key, _ = parse_key(lines[ki:])
    return key


def is_key_line(s):
    return bool(RE_PAIRS.match(s) or RE_MULTI.match(s) or RE_ORPHAN.match(s))


def find_key_region(lines):
    """Return the index where the answer-key table starts.

    The table is dense: once it begins, almost every non-blank line is a row
    of numbers and letters. Question text never looks like that for long. So
    scan forward for the first place where a sustained run of key rows starts,
    which is layout-independent.
    """
    idx = [i for i, ln in enumerate(lines) if ln.strip()]
    flags = [is_key_line(lines[i].strip()) for i in idx]
    WIN, NEED = 15, 12
    for j in range(len(idx) - WIN + 1):
        if flags[j] and sum(flags[j:j + WIN]) >= NEED:
            return idx[j]
    return None


def parse_key(lines):
    """Read the answer key, making only inferences that can be checked.

    Rows come in three shapes across these PDFs: one pair per line, several
    pairs per line, and (for multi-version papers) a number followed by one
    letter per version, where the first column is the printed set.

    pdftotext sometimes loses the number column on a run of rows. Those are
    filled in ONLY when the run exactly fills a contiguous gap between two
    explicitly numbered rows - otherwise the numbers stay missing, because
    guessing here silently produces wrong answers.
    """
    entries = []
    multi_version = False
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        mm = RE_MULTI.match(s)
        if mm:
            entries.append((int(mm.group(1)), mm.group(2)))
            multi_version = True
        elif RE_ORPHAN.match(s):
            entries.append((None, s.split()[0]))
            multi_version = True
        elif RE_PAIRS.match(s):
            for n, letter in re.findall(r"(\d{1,3})\s+([A-D])", s):
                entries.append((int(n), letter))

    key = {}
    i = 0
    prev = None
    while i < len(entries):
        n, letter = entries[i]
        if n is not None:
            key.setdefault(n, letter)
            prev = n
            i += 1
            continue
        j = i
        while j < len(entries) and entries[j][0] is None:
            j += 1
        run = j - i
        nxt = entries[j][0] if j < len(entries) else None
        if prev is not None and nxt is not None and nxt - prev - 1 == run:
            for k in range(run):
                key.setdefault(prev + 1 + k, entries[i + k][1])
        i = j
    return key, multi_version


def choose_key(layout_key, raw_key, total):
    """Pick between the two extractions, or report that they conflict."""
    want = set(range(1, total + 1))
    lay_full = want <= set(layout_key)
    raw_full = want <= set(raw_key)

    if lay_full and raw_full:
        clash = [n for n in want if layout_key[n] != raw_key[n]]
        if clash:
            return None, ["klucz niejednoznaczny: -layout i -raw roznia sie "
                          "w %d miejscach (np. q%s)" % (len(clash), clash[0])]
        return layout_key, []
    if raw_full:
        return raw_key, []
    if lay_full:
        return layout_key, []

    # neither is complete - merge, but only if they never contradict
    shared = set(layout_key) & set(raw_key)
    clash = [n for n in shared if layout_key[n] != raw_key[n]]
    if clash:
        return None, ["klucz niejednoznaczny: -layout i -raw roznia sie "
                      "w %d miejscach (np. q%s)" % (len(clash), clash[0])]
    merged = dict(raw_key)
    merged.update(layout_key)
    return merged, []


def parse_questions(lines):
    blocks, cur, expect = [], None, 1
    for s in lines:
        m = re.match(r"^(\d{1,3})\s*\.\s*(.+)$", s)
        # a digit straight after the dot means a number like "2.455 PLN",
        # not a question label
        if m and int(m.group(1)) == expect and not m.group(2)[0].isdigit():
            if cur:
                blocks.append(cur)
            cur = {"num": expect, "lines": [m.group(2)]}
            expect += 1
        elif cur is not None:
            cur["lines"].append(s)
    if cur:
        blocks.append(cur)

    def join(parts):
        return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()

    out = []
    for b in blocks:
        stem, opts, letter, buf = [], {}, None, []
        for s in b["lines"]:
            # tolerate "B .", "C .-0,43" and "____ D." - these PDFs use all
            # three, plus underscore rule-lines glued to the option letter
            # also tolerate a short ligature artifact glued to the front
            # ("ffl   C. ..."), which pdftotext emits for some fonts
            m = re.match(r"^(?:[^\sA-D]{1,4}\s+)?[_\s]*([A-D])\s*\.\s*(.+)$", s)
            if m:
                if letter:
                    opts[letter] = join(buf)
                letter, buf = m.group(1), [m.group(2)]
            elif letter:
                buf.append(s)
            else:
                stem.append(s)
        if letter:
            opts[letter] = join(buf)
        for L in "ABCD":
            # strip trailing punctuation and the underscore rules some PDFs use
            opts[L] = re.sub(r"[;.]?_*[;.]?\s*$", "", opts.get(L, "")).strip()
        out.append({"num": b["num"], "stem": join(stem), "opts": opts})
    return out


def find_date(raw, filename):
    m = re.search(r"(\d{1,2})\.(\d{2})\.(\d{4})", raw)
    if m:
        return "%s-%s-%02d" % (m.group(3), m.group(2), int(m.group(1)))
    m = re.search(r"(\d{1,2})[-.](\d{2})[-.](\d{4})", filename)
    if m:
        return "%s-%s-%02d" % (m.group(3), m.group(2), int(m.group(1)))
    m = re.search(r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})", raw + " " + filename)
    if m:
        return "%s-%02d-%02d" % (m.group(3), MONTHS[m.group(2)], int(m.group(1)))
    return ""


def find_set(raw, filename):
    m = re.search(r"[Zz]estaw(?:\s+numer)?[_\s]+(\d+)", filename)
    if m:
        return m.group(1)
    m = re.search(r"[Zz]estaw\s+(?:numer\s+)?(\d+)", raw[:3000])
    if m:
        return m.group(1)
    return "1"


def convert(pdf):
    name = os.path.basename(pdf)
    raw = pdf_to_text(pdf).replace("\x0c", "\n")
    all_lines = raw.split("\n")

    ki = find_key_region(all_lines)
    if ki is None:
        return name, None, ["nie znaleziono klucza odpowiedzi"]

    layout_key, multi = parse_key(all_lines[ki:])
    raw_key = key_from_raw(pdf)

    body = [ln.strip() for ln in all_lines[:ki]
            if not re.fullmatch(r"\d{1,3}", ln.strip())]
    start = next((n for n, s in enumerate(body) if re.match(r"^1\s*\.\s", s)), None)
    if start is None:
        return name, None, ["nie znaleziono pytania nr 1"]

    parsed = parse_questions(body[start:])

    problems = []
    key, key_problems = choose_key(layout_key, raw_key, len(parsed))
    problems.extend(key_problems)
    if key is None:
        key = {}


    questions = []
    removed = []
    broken = []
    for p in parsed:
        # withdrawn questions carry no options and no key entry - that is
        # expected, not a parse failure, so drop them quietly
        if re.search(r"usuni[eę]t", p["stem"], re.I) and not any(p["opts"].values()):
            removed.append(p["num"])
            continue
        ans = key.get(p["num"])
        why = []
        if not p["stem"]:
            why.append("pusta tresc")
        if not ans:
            why.append("brak odpowiedzi w kluczu")
        empty = [L for L in "ABCD" if not p["opts"][L]]
        if empty:
            why.append("pusta opcja " + "/".join(empty))
        if why:
            # a half-parsed question is worse than a missing one: it would be
            # unanswerable in the quiz. Leave it out and say so.
            broken.append((p["num"], "; ".join(why)))
            continue
        questions.append({
            "id": "q%03d" % p["num"],
            "number": p["num"],
            "question": p["stem"],
            "options": p["opts"],
            "answer": ans,
            "answerText": p["opts"].get(ans, "") if ans else "",
        })

    if removed:
        print("       (pominieto pytania usuniete przez KNF: %s)"
              % ", ".join(str(n) for n in removed))
    if broken:
        problems.append("pominieto %d pytan nieczytelnych w PDF: %s"
                        % (len(broken), ", ".join(str(n) for n, _ in broken)))
        for n, w in broken:
            problems.append("  q%d - %s" % (n, w))

    date = find_date(raw, name)
    if not date:
        problems.append("nie znaleziono daty egzaminu")

    meta = {
        "source": name,
        "exam": "Egzamin na Maklera Papierow Wartosciowych",
        "set": "Zestaw numer " + find_set(raw, name),
        "date": date,
        "language": "pl",
        "count": len(questions),
        "multiVersionKey": multi,
    }
    return name, {"meta": meta, "questions": questions}, problems


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    target = sys.argv[1]
    if os.path.isdir(target):
        pdfs = sorted(os.path.join(target, f) for f in os.listdir(target)
                      if f.lower().endswith(".pdf"))
    else:
        pdfs = [target]
    if not pdfs:
        raise SystemExit("Nie znaleziono plikow PDF.")

    ok = bad = 0
    for pdf in pdfs:
        name, data, problems = convert(pdf)
        short = name[:52]
        if data is None:
            print("[BLAD] %-54s %s" % (short, "; ".join(problems)))
            bad += 1
            continue
        tag = "[OK]  " if not problems else "[UWAGA]"
        extra = " (klucz wielowersyjny - kolumna 1)" if data["meta"]["multiVersionKey"] else ""
        print("%s %-54s %3d pytan | %s | %s%s"
              % (tag, short, len(data["questions"]),
                 data["meta"]["date"] or "brak daty", data["meta"]["set"], extra))
        if problems:
            for p in problems[:6]:
                print("          - %s" % p)
            if len(problems) > 6:
                print("          - ... i %d wiecej" % (len(problems) - 6))
            bad += 1
        else:
            ok += 1
        out_path = os.path.splitext(pdf)[0] + ".json"
        json.dump(data, io.open(out_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

    print("\nGotowe: %d bez uwag, %d z uwagami/bledami." % (ok, bad))
    write_bundle(target)


def write_bundle(folder):
    """Collect every parsed test into alltests.js for the web page.

    The page is opened straight from disk, where fetch() is blocked, so the
    tests have to arrive as a plain script rather than as JSON files.
    """
    paths = []
    if os.path.isdir(folder):
        paths += [os.path.join(folder, f) for f in sorted(os.listdir(folder))
                  if f.lower().endswith(".json")]
    if os.path.exists("questions.json"):
        paths.append("questions.json")

    tests, seen = [], set()
    for path in paths:
        try:
            data = json.load(io.open(path, encoding="utf-8"))
        except Exception:
            continue
        meta = data.get("meta", {})
        sm = re.search(r"[0-9]+", meta.get("set", "1"))
        exam_id = "%s-z%s" % (str(meta.get("date", "")).replace("-", ""),
                              sm.group(0) if sm else "1")
        if exam_id in seen:
            continue
        seen.add(exam_id)
        meta["examId"] = exam_id
        tests.append({"meta": meta, "questions": data.get("questions", [])})

    tests.sort(key=lambda t: t["meta"].get("date", ""))
    with io.open("alltests.js", "w", encoding="utf-8") as f:
        f.write("// Auto-generated by parse_pdf_to_json.py - do not edit.\n")
        f.write("// Plain script (not a module) so index.html works from disk.\n")
        f.write("var ALL_TESTS = " + json.dumps(tests, ensure_ascii=False, indent=1) + ";\n")
    total = sum(len(t["questions"]) for t in tests)
    print("alltests.js: %d testow, %d pytan lacznie." % (len(tests), total))


if __name__ == "__main__":
    main()
