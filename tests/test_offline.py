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
    # 修复：/vX 版本段 → 原样、去版本段、补 /v1 三种候选
    "https://api.x.com/v1beta", "https://api.x.com",
    "https://api.x.com/v1"]
assert base_url_variants("https://api.x.com/v2") == [
    "https://api.x.com/v2", "https://api.x.com", "https://api.x.com/v1"]
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

# ---- 本轮审查修复回归：大写X分隔符 / doctor只读无副作用 / --fix计数 ----
from imggen import parse_size as _parse_size
assert _parse_size("1080X1920") == (1080, 1920)        # 大写 X 分隔符

dr_cfg = {"default_provider": "dr",
          "output_dir": str(d / "nope-dr-output"),     # 绝不能被 doctor 创建
          "providers": {"dr": {"kind": "openai",
                               "base_url": "https://dr.test",
                               "model": "gpt-image-2",
                               "api_key": "sk-dr-key-abcdef"}}}
dr_cfg_path = d / "dr-config.json"
dr_cfg_path.write_text(json.dumps(dr_cfg), encoding="utf-8")
os.environ["IMGGEN_CONFIG"] = str(dr_cfg_path)

_real_probe = imggen.probe_base


def fake_probe(base, kind, key, timeout=15):
    return (True, "200 · 2 models") if "/v1" in base else (False, "empty body")


imggen.probe_base = fake_probe
try:
    # 无 --fix：报告问题且不写回，返回 1
    rc1 = imggen.cmd_doctor(imggen.load_config(require=False),
                            argparse.Namespace(fix=False))
    assert rc1 == 1
    wrote1 = json.loads(dr_cfg_path.read_text(encoding="utf-8"))
    assert wrote1["providers"]["dr"]["base_url"] == "https://dr.test"
    # doctor 是只读体检：不得创建 output_dir
    assert not (d / "nope-dr-output").exists()
    # --fix：修正并写回 /v1，问题清零返回 0
    rc2 = imggen.cmd_doctor(imggen.load_config(require=False),
                            argparse.Namespace(fix=True))
    assert rc2 == 0
    wrote2 = json.loads(dr_cfg_path.read_text(encoding="utf-8"))
    assert wrote2["providers"]["dr"]["base_url"] == "https://dr.test/v1"
finally:
    imggen.probe_base = _real_probe

# ---- 审查修复回归 2：SSRF 校验 / 错误体脱敏 / 模型列表错误透传 / 原子写 ----
from imggen import (validate_download_url as _vdu, scrub_secrets as _scrub,
                    atomic_write_text as _awt, env_path as _env_path)

# SSRF：拒绝非 http/https 与私网/环回，放行公网
for bad in ("file:///etc/passwd", "ftp://x/y", "http://127.0.0.1:8080/a",
            "http://10.0.0.5/img.png", "http://169.254.169.254/latest/meta",
            "http://[::1]/x", "http://localhost:8000/x", "https://x.localhost/a"):
    try:
        _vdu(bad)
        raise AssertionError(f"应拒绝: {bad}")
    except imggen.GenError:
        pass
_vdu("https://img.example.com/a.png")   # 公网正常放行

# 通用脱敏：sk- / AIza 前缀的 Key 被替换
assert "sk-***" in _scrub('key="sk-abcdef1234567890" end')
assert "sk-secret" not in _scrub("sk-secret9876543210")
assert "AIza***" in _scrub("token=AIzaSyDc123456789")
assert "sk-***" in _scrub("http://h/sk-abc12345")   # 路径/URL 中同样脱敏

# 原子写：内容完整写入且成功替换
_atomic_path = d / "atomic.json"
_atomic_path.write_text("old", encoding="utf-8")
_awt(_atomic_path, '{"ok": true}')
assert json.loads(_atomic_path.read_text(encoding="utf-8")) == {"ok": True}

# fetch_remote_models 错误透传：网络错误给原因，不再静默 None
class _FakeOpenerErr:
    def open(self, req, timeout=None):
        raise imggen.urllib.error.URLError("boom")


class _FakeResp:
    def __init__(self, payload):
        self._p = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpenerOk:
    def open(self, req, timeout=None):
        return _FakeResp({"data": [{"id": "gpt-image-2"}, {"id": "x-embed"}]})


_real_opener = imggen._OPENER
try:
    imggen._OPENER = _FakeOpenerErr()
    ids, err = imggen.fetch_remote_models(
        {"kind": "openai", "base_url": "https://r.test"}, "k")
    assert ids is None and "boom" in err, (ids, err)
    imggen._OPENER = _FakeOpenerOk()
    ids2, err2 = imggen.fetch_remote_models(
        {"kind": "openai", "base_url": "https://r.test"}, "k")
    assert ids2 == ["gpt-image-2", "x-embed"] and err2 == ""
finally:
    imggen._OPENER = _real_opener

# ---- 审查修复回归 3：Retry-After HTTP-date / 并发配置原子写 ----
import time as _time
import concurrent.futures as _cf

# HTTP-date 格式：未来 ~60s → 服从服务端（60 上下，封顶 120）
_future = _time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                         _time.gmtime(_time.time() + 60))
_d_future = imggen._retry_delay(0, {"Retry-After": _future})
assert 50 <= _d_future <= 120, _d_future
# 过去时间 → max(0,...) 归零
_past = _time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                       _time.gmtime(_time.time() - 60))
assert imggen._retry_delay(0, {"Retry-After": _past}) == 0.0
# 非法值 → 回退指数退避（[2,4,8]+抖动 0~1）
_d_bad = imggen._retry_delay(0, {"Retry-After": "garbage"})
assert 2.0 <= _d_bad <= 3.0, _d_bad

# 并发原子写：50 线程并发写同一文件，最终必须为完整合法 JSON（无半截/混合）
_conc = d / "conc.json"
_conc.write_text("{}", encoding="utf-8")


def _conc_writer(i):
    imggen.atomic_write_text(_conc,
                             json.dumps({"i": i, "pad": "x" * 500}))


with _cf.ThreadPoolExecutor(max_workers=8) as _ex:
    list(_ex.map(_conc_writer, range(50)))
_conc_data = json.loads(_conc.read_text(encoding="utf-8"))  # 可解析即无损坏
assert isinstance(_conc_data, dict) and "i" in _conc_data
assert not [p for p in d.glob(".*conc.json.tmp-*")]         # 无残留临时文件

# ---- setup 交互分支：模拟 TTY 全流程（打桩 isatty/input/getpass/probe）----
import builtins as _b
import getpass as _gp

setup_cfg_path = d / "setup-interactive.json"
setup_env_path = d / "setup-interactive.env"
os.environ["IMGGEN_CONFIG"] = str(setup_cfg_path)
os.environ["IMGGEN_ENV_FILE"] = str(setup_env_path)

_real_stdin, _real_input, _real_getpass = imggen.sys.stdin, _b.input, _gp.getpass
_real_fetch = imggen.fetch_remote_models
_real_probe2 = imggen.probe_base

# 模拟用户输入序列（对应 cmd_setup 逐个 ask）：
#   kind=2(Gemini) / base_url / profile 名 / env 名 / 模型序号 1 / 设为默认 y
_answers = iter(["2", "https://nano.test", "nano", "NANO_API_KEY", "1", "y"])
imggen.sys.stdin = type("_TTY", (), {"isatty": lambda self: True})()
_b.input = lambda prompt="": next(_answers)
_gp.getpass = lambda prompt="": "sk-test-gemini-key-123456"   # 不回显输入
imggen.fetch_remote_models = lambda prov, key, timeout=30: (
    ["gemini-3-pro-image-preview", "gemini-2.5-flash-image", "text"], "")
imggen.probe_base = lambda base, kind, key, timeout=15: (
    (True, "200 · 2 models") if "/v1" in base else (False, "HTTP 404"))
try:
    rc = imggen.cmd_setup(imggen.load_config(require=False),
                          argparse.Namespace(provider=None, kind=None,
                                             base_url=None, api_key_env=None,
                                             model=None, yes=False, force=False))
    assert rc == 0
    scfg = json.loads(setup_cfg_path.read_text(encoding="utf-8"))
    prov = scfg["providers"]["nano"]
    assert prov == {"kind": "gemini", "base_url": "https://nano.test/v1",
                    "model": "gemini-3-pro-image-preview",
                    "api_key_env": "NANO_API_KEY"}, prov
    assert scfg["default_provider"] == "nano"
    # Key 写入统一 env_path()（IMGGEN_ENV_FILE 指向处），且不触碰真实 .env
    assert setup_env_path.is_file()
    assert "NANO_API_KEY=sk-test-gemini-key-123456" in \
        setup_env_path.read_text(encoding="utf-8")
    assert not imggen.DEFAULT_ENV_PATH.is_file() or \
        "NANO_API_KEY" not in imggen.DEFAULT_ENV_PATH.read_text(
            encoding="utf-8", errors="replace")
finally:
    imggen.sys.stdin, _b.input, _gp.getpass = _real_stdin, _real_input, _real_getpass
    imggen.fetch_remote_models, imggen.probe_base = _real_fetch, _real_probe2
    os.environ.pop("IMGGEN_ENV_FILE", None)

print("imggen offline regression: ALL PASS")
