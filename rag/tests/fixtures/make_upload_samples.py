"""업로드 문서 시험용 합성 샘플을 만든다. 내용은 모두 지어낸 것이다. 만든 파일은 저장소에 함께 올린다
(다른 환경은 이 스크립트 없이 시험만 돌리면 된다).

  .venv/bin/python rag/tests/fixtures/make_upload_samples.py [글꼴.otf]

필요: fpdf2·pypdf (rag/requirements-dev.txt), python-docx (rag/requirements.txt), 한글 글꼴 (PDF 만). 기본은 Noto Sans CJK KR (OFL 라이선스, PDF 에는 쓴 글자만 들어간다).
참고: fpdf2 가 이 글꼴로 만든 PDF 는 글자 폭 정보가 어긋나 pypdf 로 읽으면 글자마다 띄어진다 (pdftotext·PDFium 은 정상).
그래서 RAG 는 PDFium(pypdfium2)으로 읽고, 이 샘플은 "표준에서 조금 벗어난 PDF" 시험도 겸한다.
  upload_report.pdf   4쪽 보고서. 1쪽 제목·문단, 2쪽 예산 표, 3쪽 빈 쪽(머리글도 없음), 4쪽 긴 문단.
                      1·2·4쪽에 같은 머리글·바닥글(쪽 번호) → 정제 시험(SCRUM-110)에도 쓴다
  upload_plan.pdf     상세 운영계획서 (내용은 plan_content.py). 일정·진행 순서·준비물·인원·예산·관련 법규·운영 수칙·
                      비상 대응·고려할 점. 표가 많고 글이 길다 → 검색 품질·처리 시간 측정(SCRUM-110)에 쓴다
  upload_plan.docx    upload_plan.pdf 와 같은 내용의 워드 (PDF·워드 검색 결과 비교용). 머리글·바닥글 있음
  upload_edge.docx    워드 경계 경우: 제목 전 글, 빈 문단, 제목 2단계, 병합된 표 칸, 빈 행, 자모로 쪼갠 한글·보이지 않는 문자
  upload_empty.docx   본문에 글이 없는 워드 (머리글만 있음)
  upload_scan.pdf     도형만 있고 글자는 없는 1쪽 (스캔한 PDF 대신)
  upload_locked.pdf   upload_report.pdf 에 암호를 건 것
"""
import sys
import unicodedata
from pathlib import Path

from docx import Document
from fpdf import FPDF
from pypdf import PdfReader, PdfWriter

from plan_content import PLAN

HERE = Path(__file__).parent
FONT_CANDIDATES = [Path.home() / "Library/Fonts/NotoSansCJKkr-Regular.otf", Path("/Library/Fonts/NotoSansCJKkr-Regular.otf"),
                   Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")]
HEADER = "나인아웃팅 합성 자료 · 내부 검토용"

LONG = ("행사 당일 운영은 접수, 진행, 안전 세 조로 나눈다. 접수 조는 오전 9시까지 등록 데스크를 준비하고 "
        "참가자 명단을 출력해 둔다. 진행 조는 발표 순서와 시간을 확인하고 발표자에게 10분 전에 알린다. "
        "안전 조는 비상구 위치와 소화기 상태를 점검하고 결과를 운영 대화방에 남긴다. ")


class Report(FPDF):
    def __init__(self, font):
        super().__init__()
        self.add_font("noto", "", str(font))
        self.add_font("noto", "B", str(font))   # 표 머리 행은 굵게 그리므로 같은 글꼴을 굵게로도 등록
        self.set_font("noto", size=11)
        self.plain_pages = set()                # 머리글·바닥글을 그리지 않을 쪽 번호 (빈 쪽)

    def header(self):
        if self.page_no() not in self.plain_pages:
            self.set_font("noto", size=8)
            self.cell(0, 6, HEADER, align="R", new_x="LMARGIN", new_y="NEXT")
            self.set_font("noto", size=11)

    def footer(self):
        if self.page_no() not in self.plain_pages:      # 바닥글은 다음 쪽을 넣을 때 그려지므로 쪽 번호로 판단
            self.set_y(-15)
            self.set_font("noto", size=8)
            self.cell(0, 6, f"- {self.page_no()} -", align="C")
            self.set_font("noto", size=11)

    def heading(self, text):
        self.set_font("noto", size=14)
        self.multi_cell(0, 9, text, new_x="LMARGIN", new_y="NEXT")
        self.set_font("noto", size=11)

    def para(self, text):
        self.multi_cell(0, 7, text, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def subheading(self, text):
        self.ln(1)
        self.set_font("noto", size=12)
        self.multi_cell(0, 8, text, new_x="LMARGIN", new_y="NEXT")
        self.set_font("noto", size=11)

    def bullets(self, items):
        for item in items:
            self.multi_cell(0, 7, f"- {item}", new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def grid(self, widths, rows):
        self.set_font("noto", size=9)
        with self.table(col_widths=widths, line_height=6) as table:
            for row in rows:
                r = table.row()
                for cell in row:
                    r.cell(cell)
        self.set_font("noto", size=11)
        self.ln(3)


def make_report(font, path):
    pdf = Report(font)
    pdf.add_page()
    pdf.heading("2026년 4분기 고객 초청 행사 운영 계획 (합성 자료)")
    pdf.heading("1. 개요")
    pdf.para("고객 초청 행사는 11월 20일(목)부터 이틀 동안 강남 행사장에서 연다. 참가 예정 인원은 150명이다. "
             "행사 목적은 신규 기능 소개와 고객 의견 수집이다.")
    pdf.add_page()
    pdf.heading("2. 예산 계획")
    pdf.para("총 예산은 2,470만 원이며 항목별 금액은 아래 표와 같다.")
    with pdf.table(col_widths=(40, 40, 80)) as table:
        for row in (("항목", "금액", "비고"), ("마케팅", "12,000,000원", "온라인 광고 포함"),
                    ("장소 대관", "8,500,000원", "2일"), ("식음료", "4,200,000원", "참가자 150명 기준")):
            r = table.row()
            for cell in row:
                r.cell(cell)
    pdf.plain_pages.add(3)
    pdf.add_page()                               # 3쪽: 아무것도 없는 쪽
    pdf.add_page()
    pdf.heading("3. 운영 조와 할 일")
    pdf.para(LONG * 8)
    pdf.output(str(path))


def make_plan(font, path):
    pdf = Report(font)
    pdf.add_page()
    draw = {"title": lambda t: (pdf.set_font("noto", size=16), pdf.multi_cell(0, 10, t, new_x="LMARGIN", new_y="NEXT"),
                                pdf.set_font("noto", size=11)),
            "h1": lambda t: (pdf.ln(3), pdf.heading(t)), "h2": pdf.subheading, "p": pdf.para, "list": pdf.bullets,
            "page": lambda _: pdf.add_page()}
    for kind, *args in PLAN:
        if kind == "table":
            pdf.grid(*args)
        else:
            draw[kind](*args)
    pdf.output(str(path))


def make_plan_docx(path):
    doc = Document()
    _header_footer(doc)
    for kind, *args in PLAN:
        if kind == "title":
            doc.add_heading(args[0], level=0)
        elif kind in ("h1", "h2"):
            doc.add_heading(args[0], level=1 if kind == "h1" else 2)
        elif kind == "p":
            doc.add_paragraph(args[0])
        elif kind == "list":
            for item in args[0]:
                doc.add_paragraph(item, style="List Bullet")
        elif kind == "table":
            _table(doc, args[1])
    doc.save(str(path))


def make_edge_docx(path):
    doc = Document()
    _header_footer(doc)
    doc.add_paragraph("표지: 워드 읽기 경계 시험 문서 (합성 자료)")       # 첫 제목 전 글
    doc.add_paragraph("")                                                  # 빈 문단
    doc.add_heading("1. 회의실 예약 규칙", level=1)
    doc.add_paragraph("회의실은 사내 예약 시스템으로 하루 전까지 예약한다.")
    doc.add_heading("1.1 예약 시간", level=2)
    doc.add_paragraph("예약은 30분 단위로 하며 한 번에 최대 2시간까지 잡을 수 있다.")
    t = _table(doc, [("구분", "시간", "비고"), ("평일", "09:00~12:00", "오전"), ("", "13:00~18:00", "오후"),
                     ("", "", ""), ("주말", "예약 불가", "")])
    t.cell(1, 0).merge(t.cell(2, 0))                                       # "평일" 칸을 두 행에 걸쳐 병합
    doc.add_heading("2. 정리 규칙", level=1)
    nfd = unicodedata.normalize("NFD", "회의가 끝나면 화이트보드를 지운다.")
    doc.add_paragraph(nfd[:3] + "\u200b" + nfd[3:])                       # 자모로 쪼갠 한글 + 폭 없는 공백
    doc.save(str(path))


def make_empty_docx(path):
    doc = Document()
    _header_footer(doc)
    doc.add_paragraph("")
    doc.add_paragraph("   ")
    doc.save(str(path))


def _header_footer(doc):
    """쪽마다 반복되는 머리글·바닥글. 워드는 본문과 따로 저장하므로 읽기에서 빠져야 한다."""
    section = doc.sections[0]
    section.header.paragraphs[0].text = HEADER
    section.footer.paragraphs[0].text = "나인아웃팅 고객성공팀"


def _table(doc, rows):
    table = doc.add_table(rows=len(rows), cols=len(rows[0]))
    table.style = "Table Grid"
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    return table


def make_scan(path):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_fill_color(220, 220, 220)
    for i in range(6):                           # 글자 대신 회색 막대: 스캔 이미지처럼 글을 꺼낼 수 없는 쪽
        pdf.rect(20, 30 + i * 25, 170, 12, style="F")
    pdf.output(str(path))


def make_locked(src, path):
    writer = PdfWriter(clone_from=PdfReader(str(src)))
    writer.encrypt(user_password="1234", algorithm="RC4-128")
    with open(path, "wb") as f:
        writer.write(f)


def main():
    font = Path(sys.argv[1]) if len(sys.argv) > 1 else next((p for p in FONT_CANDIDATES if p.exists()), None)
    if font is None or not font.exists():
        sys.exit("한글 글꼴이 없음. Noto Sans CJK KR 등의 경로를 인자로 준다")
    make_report(font, HERE / "upload_report.pdf")
    make_plan(font, HERE / "upload_plan.pdf")
    make_plan_docx(HERE / "upload_plan.docx")
    make_edge_docx(HERE / "upload_edge.docx")
    make_empty_docx(HERE / "upload_empty.docx")
    make_scan(HERE / "upload_scan.pdf")
    make_locked(HERE / "upload_report.pdf", HERE / "upload_locked.pdf")
    for name in ("upload_report.pdf", "upload_plan.pdf", "upload_scan.pdf", "upload_locked.pdf",
                 "upload_plan.docx", "upload_edge.docx", "upload_empty.docx"):
        print(f"{name}: {(HERE / name).stat().st_size:,} 바이트")


if __name__ == "__main__":
    main()
