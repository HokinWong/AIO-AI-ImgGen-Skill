# -*- coding: utf-8 -*-
"""imggen 离线回归测试：python tests/test_offline.py  （零网络、零计费）"""
import argparse
import base64
import os
import sys
import tempfile
import json
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

from imggen import (parse_size, gemini_aspect, gemini_image_size, map_size_for,
                    load_env_file, redact, build_multipart)

# ---- size 解析与映射 ----
cases = [("1080x1920", (1080, 1920)), ("portrait", (1080, 1920)),
         ("square", (1024, 1024)), ("auto", None), ("tall", (1024, 1536))]
for s, want in cases:
    got = parse_size(s)
    assert got == want, (s, got)
assert map_size_for("openai", (1080, 1920))["size"] == "1080x1920"
m = map_size_for("gemini", (1080, 1920))
assert m["aspectRatio"] == "9:16" and m["imageSize"] == "2K", m
m2 = map_size_for("gemini", (1024, 1024))
assert m2["aspectRatio"] == "1:1" and m2["imageSize"] == "1K"
m3 = map_size_for("gemini", (3840, 2160))
assert m3["aspectRatio"] == "16:9" and m3["imageSize"] == "4K"

# ---- redact ----
assert redact(None) == "(unset)"
r = redact("sk-abcdef1234567890")
assert r.startswith("sk-abc") and r.endswith("7890") and "…" in r
assert len(redact("short")) <= 6

# ---- multipart 构造 ----
body, ctype = build_multipart(
    [("model", "gpt-image-2"), ("prompt", "hi")],
    [("image[]", "A.PNG", b"\x89PNG\r\n\x1a\n", "image/png"),
     ("mask", "M.PNG", b"\x89PNG\r\n\x1a\n", "image/png")])
assert ctype.startswith("multipart/form-data; boundary=----imggen")
assert b'name="image[]"' in body and b'name="mask"' in body
assert b'name="prompt"' in body and b"--" + ctype.split("=")[1].encode() + b"--\r\n" in body

# ---- .env 加载 ----
d = pathlib.Path(tempfile.mkdtemp())
envf = d / "test.env"
envf.write_text("# comment line\nCODEX_API_KEY=abc123\nEMPTY=\n"
                'QUOTED="x y"\nSINGLE=\'z z\'\n', encoding="utf-8")
os.environ["IMGGEN_ENV_FILE"] = str(envf)
os.environ["EXISTING"] = "keep"
envf.write_text(envf.read_text(encoding="utf-8") + "EXISTING=overwrite-me\n",
                encoding="utf-8")
n = load_env_file()
assert n == 3, n                      # CODEX_API_KEY / QUOTED / SINGLE；EMPTY 与注释跳过
assert os.environ["CODEX_API_KEY"] == "abc123"
assert os.environ["QUOTED"] == "x y"
assert os.environ["SINGLE"] == "z z"
assert os.environ["EXISTING"] == "keep"   # 不覆盖已有变量
for k in ("CODEX_API_KEY", "QUOTED", "SINGLE", "EXISTING"):
    os.environ.pop(k, None)
# 隔离：显式指向不存在的 .env，避免读到本机真实 ~/.imggen/.env
os.environ["IMGGEN_ENV_FILE"] = str(d / "nonexistent.env")
assert isinstance(load_env_file(), int)   # 无文件路径安全返回

# ---- CLI 端到端（dry-run，脱敏检查）----
here = pathlib.Path(__file__).resolve().parent.parent
tmp_cfg = d / "config.json"
cfg = {
    "default_provider": "relay",
    "providers": {"relay": {"kind": "openai",
                            "base_url": "https://apinebula.com/v1",
                            "model": "gpt-image-2",
                            "api_key": "sk-secret-value-987654"}},
}
tmp_cfg.write_text(json.dumps(cfg), encoding="utf-8")
os.environ["IMGGEN_CONFIG"] = str(tmp_cfg)
import io
from contextlib import redirect_stdout
sys.argv = ["imggen.py"]
import imggen
buf = io.StringIO()
with redirect_stdout(buf):
    rc = imggen.main(["gen", "-p", "smoke", "-s", "portrait", "--dry-run"])
out = buf.getvalue()
assert rc == 0
assert '"mode": "text-to-image"' in out
assert "apinebula.com" in out and '"size": "1080x1920"' in out
assert "sk-secret-value" not in out and "987654" not in out   # 脱敏！

# ---- edit 子命令防呆 ----
# 1) edit 缺 --ref 必须拒绝（rc=2），防止编辑被降级成重新生成
buf2 = io.StringIO()
with redirect_stdout(buf2):
    rc_missing = imggen.main(["edit", "-p", "change bg"])
assert rc_missing == 2, rc_missing
# 2) edit 带 ref 时 dry-run 报告 mode=image-edit 且走 edits 端点
ref_png = d / "r.png"
ref_png.write_bytes(b"\x89PNG\r\n\x1a\n")
buf3 = io.StringIO()
with redirect_stdout(buf3):
    rc_edit = imggen.main(["edit", "-p", "smoke edit", "-r", str(ref_png),
                           "--dry-run"])
out3 = buf3.getvalue()
assert rc_edit == 0 and '"mode": "image-edit"' in out3
assert '"endpoint_kind": "images/edits (multipart)"' in out3

# ---- 审查修复回归：magic 嗅探 / 非ASCII参考图名 / edit透传size / 并发命名 ----
from imggen import sniff_ext, read_ref, write_outputs

assert sniff_ext(b"\x89PNG\r\n\x1a\n" + b"x") == ".png"
assert sniff_ext(b"\xff\xd8\xff\xe0" + b"x") == ".jpg"
assert sniff_ext(b"RIFF\x24\x00\x00\x00WEBPVP8 x") == ".webp"
assert sniff_ext(b"\x00\x00\x00\x00") == ".png"          # 未知回退

cn_png = d / "中文参考图.png"
cn_png.write_bytes(b"\x89PNG\r\n\x1a\n")
fname, _, _mime = read_ref(str(cn_png))
assert fname.isascii() and fname.lower().endswith(".png"), fname

captured: dict = {}
_real_post = imggen.http_post


def fake_post(url, body, headers, timeout, what):
    captured["url"] = url
    captured["body"] = body
    return {"data": [{"b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()}]}


imggen.http_post = fake_post
try:
    ns = argparse.Namespace(
        prompt="edit smoke", provider=None, model=None, size="1080x1920",
        quality=None, n=None, ref=[str(ref_png)], mask=None, format=None,
        out=str(d / "edit-out.png"), out_dir=None, timeout=10, dry_run=False)
    epaths = imggen.do_generate(cfg, ns)
    body_text = captured["body"].decode("utf-8", errors="replace")
    assert captured["url"].endswith("/images/edits"), captured["url"]
    assert 'name="size"' in body_text and "1080x1920" in body_text   # fix: edits漏size
    assert epaths[0].suffix == ".png"
finally:
    imggen.http_post = _real_post

wo = d / "wo"
o1 = write_outputs([("1.bin", b"\xff\xd8\xff\xaa")], None, wo, "relay")
o2 = write_outputs([("1.bin", b"\xff\xd8\xff\xaa")], None, wo, "relay")
assert o1[0] != o2[0] and o1[0].suffix == ".jpg"          # fix: 同秒并发防覆盖+嗅探

# ---- base_url 自动探测：变体生成 / 路由错误分类 ----
from imggen import GenError as _GenErr, base_url_variants, is_route_error

assert base_url_variants("https://api.x.com") == [
    "https://api.x.com", "https://api.x.com/v1"]
assert base_url_variants("https://api.x.com/v1") == [
    "https://api.x.com/v1", "https://api.x.com"]
assert base_url_variants("https://api.x.com/v1/") == [
    "https://api.x.com/v1", "https://api.x.com"]          # 尾斜杠规范化
assert base_url_variants("https://api.x.com/v1beta") == [
    "https://api.x.com/v1beta", "https://api.x.com"]
assert base_url_variants("") == [""]

assert is_route_error(_GenErr("x: 服务端返回空响应体"))
assert is_route_error(_GenErr("x: HTTP 404 — not found"))
assert is_route_error(_GenErr("响应不是合法 JSON：..."))
for _s in ("HTTP 401", "HTTP 400", "HTTP 429", "HTTP 503"):
    assert not is_route_error(_GenErr(f"x: {_s} — y"))    # 换路径无意义

# ---- 端到端：错误 base_url 自动补 /v1 并写回配置 ----
probe_cfg_path = d / "probe-config.json"
probe_cfg = {"default_provider": "probe",
             "providers": {"probe": {"kind": "openai",
                                     "base_url": "https://relay.test",
                                     "model": "gpt-image-2",
                                     "api_key": "sk-probe-key-123456"}}}
probe_cfg_path.write_text(json.dumps(probe_cfg), encoding="utf-8")
os.environ["IMGGEN_CONFIG"] = str(probe_cfg_path)

probe_calls: list[str] = []
_real_post2 = imggen.http_post


def probe_post(url, body, headers, timeout, what):
    probe_calls.append(url)
    if "/v1/images/" not in url:
        raise imggen.GenError(f"{what}: 服务端返回空响应体（HTTP 层成功）")
    return {"data": [{"b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()}]}


imggen.http_post = probe_post
try:
    ns2 = argparse.Namespace(
        prompt="probe", provider=None, model=None, size=None, quality=None,
        n=None, ref=[], mask=None, format=None, out=str(d / "probe.png"),
        out_dir=None, timeout=10, dry_run=False)
    ppaths = imggen.do_generate(probe_cfg, ns2)
    assert ppaths and ppaths[0].suffix == ".png"
    assert probe_calls == ["https://relay.test/images/generations",
                           "https://relay.test/v1/images/generations"], probe_calls
    written = json.loads(probe_cfg_path.read_text(encoding="utf-8"))
    assert written["providers"]["probe"]["base_url"] == "https://relay.test/v1"
finally:
    imggen.http_post = _real_post2

# ---- 401 等鉴权错误不触发变体重试 ----
probe_calls.clear()
_unauth_post = imggen.http_post


def unauthorized_post(url, body, headers, timeout, what):
    probe_calls.append(url)
    raise imggen.GenError(f"{what}: HTTP 401 — invalid key")


imggen.http_post = unauthorized_post
try:
    ns3 = argparse.Namespace(prompt="p", provider=None, model=None, size=None,
                             quality=None, n=None, ref=[], mask=None,
                             format=None, out=None, out_dir=str(d / "wo2"),
                             timeout=10, dry_run=False)
    try:
        imggen.do_generate(probe_cfg, ns3)
        raise AssertionError("401 should raise GenError")
    except imggen.GenError as exc:
        assert "401" in str(exc)
    assert len(probe_calls) == 1, probe_calls   # 只打一次，不换路径
finally:
    imggen.http_post = _unauth_post

print("imggen offline regression: ALL PASS")
