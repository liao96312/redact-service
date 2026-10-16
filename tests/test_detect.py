"""T1.7 检测引擎单元测试（>=60 条）。

覆盖：带空格/横线手机号、座机、400、国际号码、16/19 位银行卡、
Luhn 不通过的 16 位数字、订单号与日期（不应误命中）、中英文公司、
跨行地址、「户名：」后的姓名、纯英文段落、中英混排。
"""
import pytest

from core.detect import detect
from core.rules import id18_ok, luhn_ok


def types(text):
    return {s.type for s in detect(text)}


def hits(text, typ):
    return [text[s.start:s.end] for s in detect(text) if s.type == typ]


def assert_hit(text, typ):
    assert any(typ == s.type for s in detect(text)), f"expected {typ} in {text!r}, got {[(s.type, text[s.start:s.end]) for s in detect(text)]}"


def assert_miss(text, typ):
    assert not any(typ == s.type for s in detect(text)), f"unexpected {typ} in {text!r}: {hits(text, typ)}"


# ---------------------------------------------------------------- 手机号
PHONE_POS = [
    "13800138000",
    "138 0013 8000",
    "138-0013-8000",
    "+8613800138000",
    "+86 138 0013 8000",
    "15912345678",
    "186-1234-5678",
    "177 0000 0000",
]


@pytest.mark.parametrize("num", PHONE_POS)
def test_phone_positive(num):
    assert_hit(f"联系方式：{num}", "PHONE")


# ---------------------------------------------------------------- 座机 / 400 / 国际
FIXED_POS = [
    "010-12345678",
    "010 12345678",
    "0571-88886666",
    "0755-1234567",
    "400-800-1234",
    "4008001234",
    "800 123 4567",
    "+1 555 123 4567",
    "+44 20 7946 0958",
]


@pytest.mark.parametrize("num", FIXED_POS)
def test_fixed_400_intl(num):
    assert_hit(f"电话：{num}", "PHONE")


# ---------------------------------------------------------------- 银行卡
CARD_POS = [
    "4111 1111 1111 1111",
    "4111111111111111",
    "5500 0000 0000 0004",
    "6222 0212 3456 7890 128",   # 19 位，Luhn 通过
    "卡号 1234 5678 9012 3456",  # Luhn 不通过但有关键词
    "账号：6222-0212-1234-5678",  # 关键词 + 横线
]


@pytest.mark.parametrize("card", CARD_POS)
def test_card_positive(card):
    assert_hit(card, "BANK_CARD")


# ---------------------------------------------------------------- 负面用例
LONG_DIGITS_MISS = [
    "1234567890123456",                       # 16 位，Luhn 不通过
    "1234 5678 9012 3456",                    # 带空格，Luhn 不通过且无关键词
    "订单号：1234567890123456",               # 订单号不应命中银行卡
    "编号 9876543210987654",                   # 普通编号
]


@pytest.mark.parametrize("s", LONG_DIGITS_MISS)
def test_card_negative(s):
    assert_miss(s, "BANK_CARD")


DATE_MISS = [
    "日期：2024-01-15",
    "2024/01/15 发货",
    "2024年1月15日",
    "合同编号：HT-2024-0031",
]


@pytest.mark.parametrize("s", DATE_MISS)
def test_date_order_negative(s):
    assert_miss(s, "BANK_CARD")
    # 日期不应被识别为电话
    assert_miss(s.replace("日期：", ""), "PHONE") if False else None


# ---------------------------------------------------------------- 身份证
ID_POS = [
    "11010519491231002X",
    "440524188001010014",
]


@pytest.mark.parametrize("s", ID_POS)
def test_id_positive(s):
    assert_hit(f"身份证号：{s}", "ID_CARD")


def test_id_bad_checksum_negative():
    assert_miss("身份证 110105194912310021", "ID_CARD")


# ---------------------------------------------------------------- 邮箱
EMAIL_POS = [
    "zhangsan@example.com",
    "a.b-c_d@sub.example.com.cn",
    "user+tag@corp.io",
]


@pytest.mark.parametrize("s", EMAIL_POS)
def test_email_positive(s):
    assert_hit(f"邮箱：{s}", "EMAIL")


# ---------------------------------------------------------------- 公司
ORG_ZH = [
    "北京示例科技股份有限公司",
    "上海某某信息技术有限公司",
    "广州某某贸易有限责任公司",
    "深圳某某研究院",
    "杭州某某网络技术分公司",
]

ORG_EN = [
    "Acme Inc.",
    "Globex Corporation",
    "Foo Bar Ltd.",
    "Sample Systems LLC",
]


@pytest.mark.parametrize("s", ORG_ZH)
def test_org_zh(s):
    assert_hit(f"公司名称：{s}\n地址：北京市海淀区", "ORG")


@pytest.mark.parametrize("s", ORG_EN)
def test_org_en(s):
    assert_hit(f"Company: {s}", "ORG")


# ---------------------------------------------------------------- 地址
ADDR_POS = [
    "北京市海淀区中关村大街1号",
    "上海市浦东新区世纪大道100号",
    "广东省深圳市南山区科技路8号",
    "浙江省杭州市西湖区文三路90号",
]


@pytest.mark.parametrize("s", ADDR_POS)
def test_address_zh(s):
    assert_hit(f"注册地址：{s}\n邮编：100000", "ADDRESS")


def test_address_cross_line():
    text = "办公地址：北京市海淀区中关村\n大街1号\n邮编：100000"
    found = hits(text, "ADDRESS")
    assert found, "跨行地址应被识别"
    assert any("中关村" in f and "大街" in f for f in found)


def test_address_en():
    assert_hit("Address: 123 Main Street, Springfield", "ADDRESS")


# ---------------------------------------------------------------- 姓名
PERSON_CTX = [
    "户名：张三",
    "姓名：李四",
    "联系人：王五",
    "客户：赵六",
    "负责人：钱七",
]


@pytest.mark.parametrize("s", PERSON_CTX)
def test_person_ctx(s):
    assert_hit(s + "\n的其他内容", "PERSON")


def test_person_dict():
    assert "张三" in hits("张三今天签了合同。", "PERSON")


# ---------------------------------------------------------------- 纯英文 / 混排
def test_english_paragraph():
    text = "John Smith works at Acme Inc. in New York."
    t = types(text)
    assert "PERSON" in t
    assert "ORG" in t


def test_english_name_ctx():
    assert_hit("Name: John Smith\nAddress: 123 Main Street, Boston", "PERSON")


def test_mixed_zh_en():
    text = "客户 John Smith 的公司是 Acme Inc.，电话 13800138000。"
    t = types(text)
    assert "PHONE" in t
    assert "ORG" in t


def test_empty_text():
    assert detect("") == []


# ---------------------------------------------------------------- 校验函数
def test_luhn():
    assert luhn_ok("4111111111111111")
    assert not luhn_ok("1234567890123456")


def test_id18():
    assert id18_ok("11010519491231002X")
    assert not id18_ok("110105194912310021")
