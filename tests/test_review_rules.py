import re
from pathlib import Path

from rules import check_completeness, check_sensitive

# ---- 完整性（4 例）----
def test_completeness_all_fields_and_100_chars():
    fm = "type: concept\ntitle: 示例监测产品\nstatus: pending\nsource: RAW/a.md\n"
    body = "示例监测产品" * 40  # 120 字
    assert check_completeness(fm, body) == "pass"

def test_completeness_missing_type():
    fm = "title: x\nstatus: pending\nsource: RAW/a.md\n"
    assert check_completeness(fm, "正文" * 50) == "incomplete"

def test_completeness_short_body():
    fm = "type: concept\ntitle: x\nstatus: pending\nsource: RAW/a.md\n"
    assert check_completeness(fm, "正文") == "insufficient"

def test_completeness_missing_three_fields():
    fm = "title: x\n"
    assert check_completeness(fm, "正文" * 50) == "insufficient"

# ---- 敏感信息（6 例）----
def test_sensitive_id_card():
    assert check_sensitive("身份证 110101199001011234") == "blocked"

def test_sensitive_phone():
    assert check_sensitive("电话 13812345678") == "warning"

def test_sensitive_api_key():
    assert check_sensitive("key=sk-proj-abc123") == "blocked"

def test_sensitive_password():
    assert check_sensitive("password: hunter2") == "blocked"

def test_sensitive_internal_mark():
    assert check_sensitive("本文件为机密") == "blocked"

def test_sensitive_clean():
    assert check_sensitive("示例监测产品部署手册内容") == "pass"

# ---- 敏感信息补全项（2 例：邮箱/大额金额，覆盖 review_prompt 维度五全表）----
def test_sensitive_email():
    assert check_sensitive("联系 support@example.com") == "warning"

def test_sensitive_large_amount():
    assert check_sensitive("合同金额 3,527,891 元") == "warning"

# ---- 中文紧邻无空格回归（2 例，评审 Critical-1）----
def test_sensitive_id_card_no_space():
    assert check_sensitive("身份证号110101199001011234") == "blocked"

def test_sensitive_phone_no_space():
    assert check_sensitive("手机号13812345678") == "warning"
