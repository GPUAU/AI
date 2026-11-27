"""PDF 단어 → 줄·칸 (설계: 텍스트 페이지)."""
from app.mapping.rules import map_row
from app.models import LineRole
from app.parsing import _make_row
from app.pdfread import Word, align_columns, is_text_page, row_cells, words_to_rows


def w(text, x, y, width=None, h=8):
    width = width if width is not None else 6 * len(text)
    return Word(text=text, x0=x, x1=x + width, top=y, bottom=y + h)


HEADER = [w("항목", 36, 100), w("수량", 200, 100), w("금액", 300, 100)]


def test_words_group_into_lines_and_cells():
    words = HEADER + [w("전력", 36, 114), w("사용료", 52, 114.5), w("1", 200, 114), w("5,000", 300, 114)]
    lines = words_to_rows(words)
    assert [row_cells(l) for l in lines] == [["항목", "수량", "금액"], ["전력 사용료", "1", "5,000"]]


def test_columns_align_to_header():
    words = HEADER + [w("합계", 36, 128), w("9,000", 300, 128)]
    assert align_columns([words_to_rows(words)])[-1] == ["합계", "", "9,000"]


def test_page_footer_is_not_a_cost_line():  # Review Focus 1
    rows = align_columns([words_to_rows(HEADER + [w("-", 400, 500, 4), w("1", 406, 500, 4), w("-", 412, 500, 4)])])
    r = _make_row("PDF", 2, rows[-1])
    assert r is not None and map_row(r, None).role == LineRole.IGNORE


def test_wrapped_label_keeps_amount_row():  # Review Focus 2
    words = HEADER + [w("고밀도 랙", 36, 114), w("상면료", 36, 124), w("4,200,000", 300, 124)]
    rows = align_columns([words_to_rows(words)])
    assert rows[-1] == ["상면료", "", "4,200,000"] and rows[-2] == ["고밀도 랙", "", ""]


def test_cells_keep_number_formats():  # Review Focus 4
    rows = align_columns([words_to_rows(HEADER + [w("할인", 36, 114), w("(500,000)", 300, 114),
                                                  w("회선료", 36, 128), w("1,200,000원", 300, 128)])])
    assert _make_row("PDF", 2, rows[1]).numbers == {2: -500000.0}
    assert _make_row("PDF", 3, rows[2]).numbers == {2: 1200000.0}


def test_page_without_header_reuses_previous_columns():
    p1 = words_to_rows(HEADER + [w("전력", 36, 114), w("5,000", 300, 114)])
    p2 = words_to_rows([w("회선료", 36, 40), w("3,000", 300, 40)])
    assert align_columns([p1, p2])[-1] == ["회선료", "", "3,000"]


def test_is_text_page_rule():
    assert is_text_page("가" * 20) and not is_text_page("가" * 19)
    assert not is_text_page("(cid:12)" * 10 + "가" * 30)
    assert not is_text_page("�" * 3 + "가" * 30)
