"""D1.4 compress_output — log error-extraction + Python skeletonization."""

from features.compress_output import compress_log, compress_output, skeletonize_python


def test_log_keeps_errors_and_header():
    log = "$ pytest -q\nPASSED test_a\nPASSED test_b\nFAILED test_c - assert 1 == 2\nERROR at teardown of test_d\nPASSED test_e"
    out = compress_log(log)
    lines = out.splitlines()
    assert lines[0] == "$ pytest -q"  # header kept
    assert any("FAILED" in line for line in lines) and any("ERROR" in line for line in lines)
    assert "PASSED test_a" not in out


def test_log_caps_lines():
    log = "\n".join(f"FAILED line_{i}" for i in range(100))
    out = compress_log(log, max_lines=10)
    assert len(out.splitlines()) == 11  # 10 + truncation notice
    assert "truncated" in out


def test_skeleton_drops_bodies_keeps_sigs():
    src = "def handler(a, b=2):\n    x = compute(a)\n    return x + b\n\nclass Repo:\n    def fetch(self):\n        return 42\n"
    sk = skeletonize_python(src)
    assert "def handler(a, b=2):" in sk
    assert "def fetch(self):" in sk
    assert "compute" not in sk and "42" not in sk
    assert "..." in sk


def test_auto_mode_python():
    res = compress_output("def f():\n    secret_body = 1", mode="auto")
    assert res["mode"] == "code"
    assert "secret_body" not in res["text"]


def test_auto_mode_log_fallback():
    log = "build started\nerror: link failed\nerror: link failed\nerror: link failed"
    res = compress_output(log, mode="auto")
    assert res["mode"] == "log"
    # consecutive duplicates collapsed to one
    assert res["text"].count("error: link failed") == 1
    assert res["original_lines"] == 4 and res["kept_lines"] == 2


def test_code_mode_syntax_error_falls_back():
    res = compress_output("def broken(:\n    pass", mode="code")
    assert res["mode"] == "code->log"
