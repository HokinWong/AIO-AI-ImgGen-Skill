#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""imggen — 统一多 provider 图像生成 CLI（零第三方依赖）

Providers:
  openai  OpenAI Images API（官方 https://api.openai.com/v1 或任意 OpenAI 兼容端点）
  gemini  Google Gemini generateContent（AI Studio API Key，Nano Banana 系列）
  mj      MidJourney 适配器预留位（暂未实现，见 references/providers.md）

Commands:
  generate    生成 / 编辑一张或多张图
  edit        图生图显式入口（强制 --ref）
  batch       从 JSONL 批量执行 generate 任务
  setup       交互式配置向导（渠道/Key/模型/连通测试）
  models      列出渠道可用模型（远端拉取 + 内置清单）
  doctor      渠道体检（Key/路由/输出目录），--fix 自动修正 base_url
  providers   列出已配置的渠道 profile（key 只显示 set/unset）
  init-config 写出默认配置模板到 ~/.imggen/config.json

Exit codes: 0 成功 | 1 生成失败 | 2 参数/配置错误 | 130 中断
注意：Gemini 多张时若部分轮次失败，已成功的图仍会写出并返回 0
（stderr 会明确警告"仅保留已完成的 N 张"）。"""

from __future__ import annotations

import argparse
import base64
import binascii
import email.utils
import io
import ipaddress
import json
import mimetypes
import os
import random
import re
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

VERSION = "0.1.0"
PROG = "imggen"

DEFAULT_TIMEOUT = 600          # 生图较慢，单请求上限放宽
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_DELAYS = [2, 4, 8]       # 基础退避（秒），叠加随机抖动，共 3 次重试
MAX_REF_BYTES = 20 * 1024 * 1024      # 单张参考图上限
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024  # URL 下载上限

SIZE_ALIASES = {
    "square":    (1024, 1024),
    "portrait":  (1080, 1920),   # 用户常用竖版预设
    "landscape": (1920, 1080),
    "tall":      (1024, 1536),
    "wide":      (1536, 1024),
}

# Gemini 支持的固定宽高比集合（Nano Banana / Pro 官方文档）
GEMINI_ASPECTS = [
    ("1:1", 1.0), ("2:3", 2 / 3), ("3:2", 1.5), ("3:4", 0.75), ("4:3", 4 / 3),
    ("4:5", 0.8), ("5:4", 1.25), ("9:16", 9 / 16), ("16:9", 16 / 9),
    ("16:21", 16 / 21), ("21:9", 21 / 9),
]

DEFAULT_CONFIG = {
    "default_provider": "openai",
    "output_dir": "output/imagegen",
    "providers": {
        "openai": {
            "kind": "openai",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-image-2",
            "api_key_env": "OPENAI_API_KEY",
        },
        "gemini": {
            "kind": "gemini",
            "base_url": "https://generativelanguage.googleapis.com/v1beta",
            "model": "gemini-3-pro-image-preview",
            "api_key_env": "GEMINI_API_KEY",
        },
        # 示例：第三方 OpenAI 兼容中转（复制一份改 base_url/key 即可）
        # "apinebula": {"kind": "openai", "base_url": "https://apinebula.com/v1",
        #               "model": "gpt-image-2", "api_key_env": "CODEX_API_KEY"},
    },
}

DEFAULT_ENV_PATH = Path.home() / ".imggen" / ".env"

# 内置图像模型知识库：models 子命令与 setup 向导的选择清单
KNOWN_IMAGE_MODELS = {
    "openai": ["gpt-image-2", "gpt-image-1", "dall-e-3", "dall-e-2"],
    "gemini": ["gemini-3-pro-image-preview", "gemini-2.5-flash-image"],
}
IMAGE_MODEL_HINTS = ("image", "dall-e", "dalle")


class UserError(Exception):
    """可预期的用户侧错误（配置缺失、参数非法等），exit code 2"""


class GenError(Exception):
    """生成失败（API 拒绝、安全拦截、网络最终失败等），exit code 1"""


# ---------------------------------------------------------------- utilities

def redact(key: str | None) -> str:
    if not key:
        return "(unset)"
    if len(key) <= 10:
        return key[:2] + "…" + key[-2:]
    return key[:6] + "…" + key[-4:]


def eprint(*args):
    print(*args, file=sys.stderr)


def load_json_bytes(data: bytes, url: str = "") -> dict:
    if not data.strip():
        raise GenError(
            f"服务端返回空响应体（HTTP 层成功）。常见原因：base_url 缺少 '/v1'"
            f"前缀导致请求落在无效路由上；请核对 profile 的 base_url"
            f"{f'（当前端点 {url}）' if url else ''}")
    try:
        return json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise GenError(f"响应不是合法 JSON：{exc}; "
                       f"body[:200]={scrub_secrets(repr(data[:200]))}")


# ---------------------------------------------------------------- secret scrubbing

_KEY_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{6,}|AIza[A-Za-z0-9_\-]{6,})")


def scrub_secrets(text: str) -> str:
    """通用脱敏：把形如 sk-xxx / AIza xxx 的 Key 值替换为前缀+***。
    用于服务端错误体等可能回显 Key 的文本，防止间接泄露。"""
    def rep(m: re.Match) -> str:
        g = m.group(1)
        return (g[:4] if g.startswith("AIza") else g[:3]) + "***"
    return _KEY_RE.sub(rep, text)


class _NoCrossHostRedirect(urllib.request.HTTPRedirectHandler):
    """第三方端点若 302 到其他主机，默认 urllib 会原样转发 Authorization /
    x-goog-api-key —— 存在鉴权头外泄风险。这里拒绝 API 请求的跨主机重定向。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        dest = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        src = urllib.parse.urlsplit(req.full_url)
        if (dest.scheme.lower(), dest.netloc.lower()) != \
                (src.scheme.lower(), src.netloc.lower()):
            raise GenError(
                f"端点重定向到不同主机（{src.netloc} → {dest.netloc}），"
                f"为防止鉴权头外泄已阻止；请检查 base_url 是否正确")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_NoCrossHostRedirect)


def validate_download_url(url: str) -> None:
    """下载 URL 安全校验（SSRF 防护）：仅 http/https，拒绝本机/私网/链路本地，
    并拒绝特殊 scheme。域名目标允许（DNS rebinding 属已知限制，单用户 CLI）。"""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise GenError(f"下载地址无法解析：{url[:120]}") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise GenError(f"拒绝下载非 http/https 地址：{url[:120]}")
    host = (parts.hostname or "").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise GenError(f"拒绝下载指向本机的地址：{url[:120]}")
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return                                       # 域名：DNS rebinding 已知限制
    if (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
        raise GenError(f"拒绝下载指向非公网 IP 的地址：{url[:120]}")


class _SafeDownloadRedirect(urllib.request.HTTPRedirectHandler):
    """下载链路上的重定向也要逐跳做 URL 许可校验。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        validate_download_url(target)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_DL_OPENER = urllib.request.build_opener(_SafeDownloadRedirect)


def _retry_delay(attempt: int, headers=None) -> float:
    """退避延迟：优先服从 Retry-After（支持秒数与 HTTP-date），
    否则指数退避 + 随机抖动。"""
    if headers is not None:
        ra = headers.get("Retry-After") if hasattr(headers, "get") else None
        if ra:
            try:
                return max(0.0, min(float(ra), 120.0))
            except (TypeError, ValueError):
                pass
            try:
                dt = email.utils.parsedate_to_datetime(ra)
                if dt:
                    return max(0.0, min(
                        (dt - datetime.now(timezone.utc)).total_seconds(),
                        120.0))
            except (TypeError, ValueError, OverflowError):
                pass
    return RETRY_DELAYS[attempt] + random.uniform(0, 1)


def http_post(url: str, body: bytes, headers: dict, timeout: int,
              what: str) -> dict:
    """POST with bounded retry on 429/5xx. Returns parsed JSON."""
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST")
        try:
            with _OPENER.open(req, timeout=timeout) as resp:
                return load_json_bytes(resp.read(), url)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            exc.close()
            status = exc.code
            if status in RETRY_STATUS and attempt < len(RETRY_DELAYS):
                delay = _retry_delay(attempt, exc.headers)
                attempt += 1
                eprint(f"[{PROG}] {what}: HTTP {status}，{delay:.0f}s 后重试"
                       f"（{attempt}/{len(RETRY_DELAYS)}）…")
                time.sleep(delay)
                continue
            detail = extract_error_message(payload, status)
            raise GenError(f"{what}: HTTP {status}{detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt < len(RETRY_DELAYS):
                delay = _retry_delay(attempt)
                attempt += 1
                eprint(f"[{PROG}] {what}: 网络错误 {exc}，{delay:.0f}s 后重试"
                       f"（{attempt}/{len(RETRY_DELAYS)}）…")
                time.sleep(delay)
                continue
            raise GenError(f"{what}: 网络请求最终失败：{exc}") from None


def extract_error_message(payload: bytes, status: int) -> str:
    """尽量提取 API 错误信息；绝不包含鉴权头，并对 body 做 Key 脱敏。"""
    try:
        obj = json.loads(payload.decode("utf-8"))
        err = obj.get("error", obj)
        if isinstance(err, dict):
            msg = err.get("message") or err.get("status") or ""
            if msg:
                return f" — {scrub_secrets(str(msg))}"
        return f" — {scrub_secrets(str(obj)[:300])}"
    except Exception:
        snippet = payload[:300].decode("utf-8", errors="replace")
        return f" — body[:300]={scrub_secrets(snippet)!r}"


def guess_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime not in ("image/png", "image/jpeg", "image/webp"):
        raise UserError(
            f"参考图 {path.name} 不是 png/jpeg/webp（检测到 {mime or 'unknown'}）")
    return mime


def decode_b64(data: str, what: str) -> bytes:
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise GenError(f"{what}：响应图像 base64 解码失败（{exc}）") from None


def read_ref(path_str: str) -> tuple[str, bytes, str]:
    path = Path(path_str).expanduser()
    if not path.is_file():
        raise UserError(f"参考图不存在：{path}")
    size = path.stat().st_size
    if size > MAX_REF_BYTES:
        raise UserError(f"参考图 {path.name} 过大"
                        f"（{size / 1048576:.0f}MB > 上限 "
                        f"{MAX_REF_BYTES // 1048576}MB）")
    name = path.name.upper()
    # multipart header 兼容性：非 ASCII 或含引号/控制字符时替换为安全占位名
    if (not name.isascii() or any(c in name for c in '"\r\n\x00')):
        name = f"REF-{uuid.uuid4().hex[:6]}{path.suffix.upper() or '.PNG'}"
    return name, path.read_bytes(), guess_mime(path)


# ---------------------------------------------------------------- base url probe

def base_url_variants(base: str) -> list[str]:
    """生成 base_url 探测变体：原样优先，其次去版本段 / 补 /v1（含尾斜杠规范化）。"""
    b = (base or "").rstrip("/")
    if not b:
        return [b]
    stripped = re.sub(r"/v\d+[a-z]*$", "", b,
                      flags=re.IGNORECASE) if re.search(
        r"/v\d+[a-z]*$", b, re.IGNORECASE) else None
    out = [b]
    if stripped and stripped not in out:
        out.append(stripped)
    v1 = (b if b.lower().endswith("/v1") else (stripped or b) + "/v1")
    if v1 != b and v1 not in out:
        out.append(v1)
    return out


def is_route_error(exc: GenError) -> bool:
    """判断错误是否为'路由/路径不对'——值得换 base_url 变体重试。
    鉴权（401/403）、参数（400）、限流（429）等换路径也无意义，不重试。"""
    msg = str(exc)
    return ("空响应体" in msg
            or "HTTP 404" in msg
            or "不是合法 JSON" in msg)   # HTML 错误页等


def atomic_write_text(path: Path, text: str) -> None:
    """原子写入：先写同目录临时文件再 os.replace，避免半截文件/并发覆盖。
    Windows 上 replace 可能被瞬时占用（杀软/索引器）拒绝，做短退避重试。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex[:6]}")
    tmp.write_text(text, encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise


_config_lock = threading.Lock()


def remember_base(prov_name: str, old_base: str, new_base: str) -> bool:
    """探测命中后备选 base_url 写回配置文件。成功返回 True；
    任何失败（读/写/JSON）都不抛异常，只 eprint 并返回 False——
    base_url 修正属于增强，绝不能阻断已成功的图片写出。"""
    with _config_lock:
        path = config_path()
        try:
            if not path.is_file():
                eprint(f"[{PROG}] 未找到配置文件 {path}，无法写回 base_url")
                return False
            cfg = json.loads(path.read_text(encoding="utf-8"))
            prov = cfg.get("providers", {}).get(prov_name)
            if not isinstance(prov, dict):
                return False
            if prov.get("base_url", "").rstrip("/") != old_base.rstrip("/"):
                return False                 # 配置已被他人修改，不覆盖
            prov["base_url"] = new_base
            atomic_write_text(path,
                              json.dumps(cfg, ensure_ascii=False, indent=2)
                              + "\n")
            eprint(f"[{PROG}] 已自动修正 profile '{prov_name}' base_url："
                   f"{old_base} → {new_base}（写回配置，下次直接生效）")
            return True
        except (OSError, json.JSONDecodeError) as exc:
            eprint(f"[{PROG}] 本次使用 {new_base}；写回配置失败：{exc}")
            return False


# ---------------------------------------------------------------- setup/doctor helpers

def probe_base(base: str, kind: str, key: str, timeout: int = 15) \
        -> tuple[bool, str]:
    """轻量连通探测：GET {base}/models（免费，不产生生成费用）。
    假路由的"空 200"会因 JSON 解析失败被判不可用。返回 (可用, 详情)。"""
    url = base.rstrip("/") + "/models"
    headers = ({"Authorization": f"Bearer {key}"} if kind == "openai"
               else {"x-goog-api-key": key})
    try:
        req = urllib.request.Request(url, headers=headers)
        with _OPENER.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        n = len(data.get("data") or data.get("models") or [])
        return True, f"200 · {n} models"
    except urllib.error.HTTPError as exc:
        try:
            exc.close()
        except Exception:
            pass
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)[:100]


def fetch_remote_models(prov: dict, key: str, timeout: int = 30) \
        -> tuple[list[str] | None, str]:
    """拉取渠道远端模型 id 列表（GET /models）。
    返回 (模型列表, "") 成功；失败 (None, 原因)，原因区分 HTTP/网络/解析。"""
    base = (prov.get("base_url") or "").rstrip("/")
    kind = prov.get("kind", "openai")
    if not base:
        return None, "base_url 为空"
    headers = ({"Authorization": f"Bearer {key}"} if kind == "openai"
               else {"x-goog-api-key": key})
    try:
        req = urllib.request.Request(base + "/models", headers=headers)
        with _OPENER.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            exc.close()
        except Exception:
            pass
        return None, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, f"网络错误 {exc}"
    except Exception as exc:
        return None, f"响应解析失败 {type(exc).__name__}: {exc}"
    ids: list[str] = []
    if kind == "gemini":
        for m in data.get("models") or []:
            name = (m.get("name") or "").removeprefix("models/")
            if name:
                ids.append(name)
    else:
        for m in data.get("data") or []:
            if isinstance(m, dict) and m.get("id"):
                ids.append(str(m["id"]))
    return ids, ""


def env_path() -> Path:
    """统一 .env 路径：IMGGEN_ENV_FILE 显式路径优先，否则默认 ~/.imggen/.env。
    setup 写入与 doctor 检查都必须走这里，保证读写一致。"""
    explicit = os.environ.get("IMGGEN_ENV_FILE")
    return (Path(explicit).expanduser() if explicit else DEFAULT_ENV_PATH)


def upsert_env_key(env_path_: Path, name: str, value: str) -> None:
    """向 .env 文件写入/更新一行 KEY=VALUE（原子写 + POSIX 仅属主可读写）。"""
    lines = (env_path_.read_text(encoding="utf-8").splitlines()
             if env_path_.is_file() else [])
    prefix = name + "="
    lines = [l for l in lines if not l.strip().startswith(prefix)]
    lines.append(f"{name}={value}")
    atomic_write_text(env_path_, "\n".join(lines) + "\n")
    try:
        os.chmod(env_path_, 0o600)     # POSIX：收紧为仅当前用户可读写
    except OSError:
        pass                           # Windows 无此语义，忽略


# ---------------------------------------------------------------- config

def config_path() -> Path:
    env = os.environ.get("IMGGEN_CONFIG")
    if env:
        return Path(env).expanduser()
    local = Path(".imggen.json")
    if local.is_file():
        return local
    return Path.home() / ".imggen" / "config.json"


def positive_int(text: str) -> int:
    """argparse 类型：严格正整数（拒绝 0/负数/浮点串）。"""
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"需要正整数，得到 '{text}'") from None
    if v < 1:
        raise argparse.ArgumentTypeError(f"需要 >=1 的整数，得到 {v}")
    return v


def load_config(require: bool = True) -> dict:
    path = config_path()
    if not path.is_file():
        if require:
            raise UserError(
                f"未找到配置文件 {path}\n"
                f"快速开始（三选一）：\n"
                f"  1) 交互式向导（推荐，自动填好一切）：\n"
                f"       python imggen.py setup\n"
                f"  2) 写出配置模板后手动编辑：\n"
                f"       python imggen.py init-config\n"
                f"  3) 只用环境变量提供 Key（配合默认 profile）：\n"
                f"       PowerShell: [Environment]::SetEnvironmentVariable(\n"
                f"         \"OPENAI_API_KEY\",\"<你的Key>\",\"User\")\n"
                f"配置格式详见 skill 的 references/config-and-keys.md")
        return {}
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise UserError(f"配置文件 {path} 解析失败：{exc}")
    if not isinstance(cfg.get("providers"), dict) or not cfg["providers"]:
        raise UserError(f"配置文件 {path} 缺少 providers 定义")
    for name, prov in cfg["providers"].items():
        if not isinstance(prov, dict):
            raise UserError(f"profile '{name}' 必须是对象")
        if not isinstance(prov.get("base_url"), str) or not prov["base_url"]:
            raise UserError(f"profile '{name}' 缺少 base_url")
    return cfg


def resolve_profile(cfg: dict, name: str | None) -> tuple[str, dict]:
    name = name or cfg.get("default_provider")
    if not name:
        raise UserError("未指定 --provider，且配置缺少 default_provider")
    provs = cfg["providers"]
    if name not in provs:
        raise UserError(f"profile '{name}' 不存在；可用: {', '.join(provs)}")
    return name, provs[name]


def resolve_key(prov_name: str, prov: dict) -> str:
    env_name = prov.get("api_key_env")
    key = os.environ.get(env_name, "") if env_name else ""
    if not key:
        key = prov.get("api_key", "")
    if not key:
        hint = f"环境变量 {env_name}" if env_name else "配置文件 api_key 字段"
        raise UserError(
            f"profile '{prov_name}' 缺少 API Key（期望来自 {hint}）。\n"
            f"请在本地设置后重试，不要把 Key 粘贴到聊天里。")
    return key


# ---------------------------------------------------------------- size

def parse_size(size: str | None) -> tuple[int, int] | None:
    """'WxH' / 别名 / 'auto'(None)。"""
    if size is None or size == "auto":
        return None
    low = size.lower()
    if low in SIZE_ALIASES:
        return SIZE_ALIASES[low]
    m = re.match(r"^(\d{3,4})\s*[xX×]\s*(\d{3,4})$", size.strip())
    if not m:
        raise UserError(f"--size 无效：'{size}'（示例：1080x1920 / portrait / auto）")
    w, h = int(m.group(1)), int(m.group(2))
    if not (256 <= w <= 4096 and 256 <= h <= 4096):
        raise UserError(f"--size 超出支持范围(256..4096)：{w}x{h}")
    return w, h


def gemini_aspect(w: int, h: int) -> str:
    ratio = w / h
    return min(GEMINI_ASPECTS, key=lambda item: abs(item[1] - ratio))[0]


def gemini_image_size(w: int, h: int) -> str:
    longest = max(w, h)
    if longest > 2048:
        return "4K"
    if longest > 1024:
        return "2K"
    return "1K"


def map_size_for(kind: str, wh: tuple[int, int] | None) -> dict:
    """各 provider 的尺寸映射结果（dry-run 也会展示）。"""
    if wh is None:
        return {"note": "provider 默认"}
    w, h = wh
    if kind == "openai":
        return {"size": f"{w}x{h}"}
    if kind == "gemini":
        return {"aspectRatio": gemini_aspect(w, h),
                "imageSize": gemini_image_size(w, h),
                "note": f"目标 {w}x{h} → 最近邻宽高比"}
    return {}


# ---------------------------------------------------------------- openai

def openai_headers(key: str, json_body: bool) -> dict:
    h = {"Authorization": f"Bearer {key}"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def build_multipart(fields: list[tuple[str, str]],
                    files: list[tuple[str, str, bytes, str]]) -> tuple[bytes, str]:
    boundary = "----imggen" + uuid.uuid4().hex
    buf = io.BytesIO()
    for name, value in fields:
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        buf.write(value.encode("utf-8"))
        buf.write(b"\r\n")
    for field, filename, data, mime in files:
        buf.write(f"--{boundary}\r\n".encode())
        buf.write((f'Content-Disposition: form-data; name="{field}"; '
                   f'filename="{filename}"\r\n').encode())
        buf.write(f"Content-Type: {mime}\r\n\r\n".encode())
        buf.write(data)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def openai_generate(prov_name: str, prov: dict, args) -> list[tuple[str, bytes]]:
    key = resolve_key(prov_name, prov)
    model = args.model or prov.get("model", "gpt-image-2")
    wh = parse_size(args.size)

    common: list[tuple[str, str]] = [("model", model), ("prompt", args.prompt)]
    if args.n and int(args.n) > 1:
        common.append(("n", str(int(args.n))))
    if args.quality:
        common.append(("quality", args.quality))
    if args.format:
        common.append(("output_format", args.format))
    if wh:
        common.append(("size", f"{wh[0]}x{wh[1]}"))

    # 请求体与鉴权只构建一次（与 base_url 无关），路径后缀按模式固定
    if args.ref:
        what, path_suffix = "images.edits", "/images/edits"
        files = [("image[]", *read_ref(p)) for p in args.ref]
        if args.mask:
            files.append(("mask", *read_ref(args.mask)))
        body, ctype = build_multipart(common, files)
        headers = openai_headers(key, json_body=False)
        headers["Content-Type"] = ctype
    else:
        what, path_suffix = "images.generations", "/images/generations"
        body = json.dumps(dict(common)).encode("utf-8")
        headers = openai_headers(key, json_body=True)

    # base_url 自动探测：配置值优先，路由类错误时依次尝试变体（补/去 /v1）
    bases = base_url_variants(prov.get("base_url", ""))
    for bi, base in enumerate(bases):
        try:
            resp = http_post(base + path_suffix, body, headers,
                             args.timeout, what)
            images = extract_openai_images(resp, args.timeout)
            if bi > 0:
                remember_base(prov_name, bases[0], base)
            return images
        except GenError as exc:
            if bi == len(bases) - 1 or not is_route_error(exc):
                raise
            eprint(f"[{PROG}] base_url '{base}' 路由不可用"
                   f"（{str(exc)[:140]}），尝试备选：{bases[bi + 1]} …")
    raise GenError("unreachable: base_url 探测循环异常退出")


def sniff_ext(data: bytes) -> str:
    """按内容魔数判定图像扩展名；无法识别时保守回退 .png"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return ".webp"
    return ".png"


def download_url(url: str, timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """下载中转站返回的图片 URL：带安全校验、退避重试、大小上限与友好报错。"""
    validate_download_url(url)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "imggen"})
            with _DL_OPENER.open(req, timeout=timeout) as r:
                data = r.read(MAX_DOWNLOAD_BYTES + 1)
            if len(data) > MAX_DOWNLOAD_BYTES:
                raise GenError(f"生成图超过下载上限"
                               f"（>{MAX_DOWNLOAD_BYTES // 1048576}MB）")
            return data
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRY_STATUS or attempt == 2:
                raise GenError(f"下载生成图失败：HTTP {exc.code}") from None
            time.sleep(_retry_delay(attempt, exc.headers))
        except GenError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == 2:
                raise GenError(f"下载生成图失败：{exc}") from None
            time.sleep(_retry_delay(attempt))
    raise GenError("下载生成图失败")     # 不可达，防御性兜底


def extract_openai_images(resp: dict, timeout: int) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for i, item in enumerate(resp.get("data") or []):
        if not isinstance(item, dict):
            continue
        if item.get("b64_json"):
            out.append((f"{i + 1}.bin", decode_b64(item["b64_json"],
                                                   f"data[{i}]")))
        elif item.get("url"):
            out.append((f"{i + 1}.bin", download_url(item["url"], timeout)))
        elif item.get("revised_prompt"):
            continue
    revised = next((d.get("revised_prompt") for d in resp.get("data") or []
                    if d.get("revised_prompt")), None)
    if revised:
        eprint(f"[{PROG}] 服务端改写后的 prompt: {revised}")
    if not out:
        raise GenError(f"响应中未找到图像数据：{str(resp)[:300]}")
    return out


# ---------------------------------------------------------------- gemini

def gemini_generate(prov_name: str, prov: dict, args) -> list[tuple[str, bytes]]:
    key = resolve_key(prov_name, prov)
    base = prov["base_url"].rstrip("/")
    model = args.model or prov.get("model", "gemini-3-pro-image-preview")
    endpoint = f"{base}/models/{model}:generateContent"
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}

    wh = parse_size(args.size)

    def build_body(include_size: bool) -> dict:
        parts: list[dict] = [{"text": args.prompt}]
        for p in args.ref or []:
            _, data, mime = read_ref(p)
            parts.append({"inline_data": {"mime_type": mime,
                                          "data": base64.b64encode(data).decode()}})
        gen_cfg: dict = {"responseModalities": ["TEXT", "IMAGE"]}
        img_cfg: dict = {}
        if wh and include_size:
            mapped = map_size_for("gemini", wh)
            img_cfg["aspectRatio"] = mapped["aspectRatio"]
            img_cfg["imageSize"] = mapped["imageSize"]
        if img_cfg:
            gen_cfg["imageConfig"] = img_cfg
        return {"contents": [{"parts": parts}], "generationConfig": gen_cfg}

    rounds = max(int(args.n or 1), 1)   # Gemini 单次一张，n 张 = n 次调用
    results: list[tuple[str, bytes]] = []
    size_degraded = False               # 端点不支持 imageConfig 时全轮记住
    for round_i in range(rounds):
        suffix = f"（第 {round_i + 1}/{rounds} 张）" if rounds > 1 else ""
        try:
            body_obj = build_body(include_size=not size_degraded)
            resp = http_post(endpoint, json.dumps(body_obj).encode("utf-8"),
                             headers, args.timeout, f"gemini:{model}{suffix}")
            results.extend(extract_gemini_images(resp, round_i, rounds))
            continue
        except GenError as exc:
            payload = exc
        # 兼容层不支持 imageConfig 时自动降级重试一次
        recovered = False
        if not size_degraded and (
                "imageconfig" in str(payload).lower()
                or "unknown name" in str(payload).lower()):
            size_degraded = True
            eprint(f"[{PROG}] 端点似乎不支持 imageConfig，"
                   f"本轮及后续轮次降级重试…")
            try:
                body_obj = build_body(include_size=False)
                resp = http_post(endpoint,
                                 json.dumps(body_obj).encode("utf-8"),
                                 headers, args.timeout,
                                 f"gemini:{model}(降级)")
                results.extend(extract_gemini_images(resp, round_i, rounds))
                recovered = True
            except GenError as exc2:
                payload = exc2
        if not recovered:
            if results:
                # 已有成功且已计费的轮次：不丢图，返回部分结果
                eprint(f"[{PROG}] 警告：第 {round_i + 1}/{rounds} 张失败"
                       f"（{str(payload)[:200]}）；仅保留已完成的 "
                       f"{len(results)} 张")
                return results
            raise GenError(str(payload)) from None
    return results


def extract_gemini_images(resp: dict, round_i: int, rounds: int) \
        -> list[tuple[str, bytes]]:
    block = (resp.get("promptFeedback") or {}).get("blockReason")
    if block:
        raise GenError(f"Gemini 安全拦截：{block}")
    cands = resp.get("candidates") or []
    if not cands:
        raise GenError(f"Gemini 响应无 candidates：{str(resp)[:300]}")
    texts: list[str] = []
    images: list[tuple[str, bytes]] = []
    for part in (cands[0].get("content") or {}).get("parts") or []:
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            ext = {"image/png": ".png", "image/jpeg": ".jpg",
                   "image/webp": ".webp"}.get(inline.get("mimeType")
                                              or inline.get("mime_type"),
                                              ".png")
            idx = round_i + 1 if rounds > 1 else ""
            images.append((f"{idx or '1'}{ext}",
                           decode_b64(inline["data"], "Gemini inlineData")))
        elif part.get("text"):
            texts.append(part["text"])
    if texts:
        joined = " ".join(texts).strip()
        if joined:
            eprint(f"[{PROG}] 模型附言: {joined[:400]}")
    if not images:
        finish = cands[0].get("finishReason", "?")
        raise GenError(f"Gemini 未返回图像（finishReason={finish}）："
                       f"{str(resp)[:300]}")
    return images


# ---------------------------------------------------------------- dispatch

def do_generate(cfg: dict, args) -> list[Path]:
    prov_name, prov = resolve_profile(cfg, args.provider)
    kind = prov.get("kind", prov_name)
    if kind == "mj":
        raise UserError(
            "MJ 适配器为预留位，尚未实现。接入计划见 references/providers.md "
            "§MidJourney（midjourney-proxy 规范）。")

    if not args.prompt:
        raise UserError("缺少 --prompt")
    unsupported: list[str] = []
    if args.mask:
        if not args.ref:
            raise UserError("--mask 需要至少一个 --ref 参考图")
        if kind == "gemini":
            unsupported.append("--mask 局部重绘")
    if kind == "gemini":
        if args.quality:
            unsupported.append("-q/--quality 质量档")
        if args.format:
            unsupported.append("-f/--format 输出格式")
    if unsupported:
        eprint(f"[{PROG}] 警告：gemini 渠道不支持 {'、'.join(unsupported)}，"
               f"相应参数将被忽略")

    if args.dry_run:
        dry_run_report(prov_name, prov, kind, args)
        return []

    eprint(f"[{PROG}] → {prov_name}:{args.model or prov.get('model')} · "
           f"size={args.size or 'auto'} · quality={args.quality or '-'} · "
           f"n={int(args.n or 1)} · refs={len(args.ref or [])}"
           + (" · mask" if args.mask else ""))
    t0 = time.time()
    if kind == "openai":
        images = openai_generate(prov_name, prov, args)
    elif kind == "gemini":
        images = gemini_generate(prov_name, prov, args)
    else:
        raise UserError(f"未知 provider kind：'{kind}'（支持 openai/gemini/mj）")

    out_dir = Path(args.out_dir or cfg.get("output_dir", "output/imagegen"))
    paths = write_outputs(images, args.out, out_dir, prov_name)
    dt = time.time() - t0
    for p in paths:
        print(str(p.resolve()))
    eprint(f"[{PROG}] 完成 {len(paths)} 张 · {prov_name}:"
           f"{args.model or prov.get('model')} · {dt:.1f}s")
    return paths


def write_outputs(images, out_arg, out_dir: Path, prov_name: str) -> list[Path]:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    uniq = uuid.uuid4().hex[:6]      # 并发 batch 同秒同渠道时防止文件名互相覆盖
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, (name, data) in enumerate(images):
        if out_arg:
            base = Path(out_arg).expanduser()
            if len(images) > 1:
                p = base.with_name(f"{base.stem}_{i + 1}{base.suffix or '.png'}")
            else:
                p = base if base.suffix else base.with_suffix(".png")
        else:
            p = out_dir / f"{stamp}-{prov_name}-{uniq}-{i + 1}{sniff_ext(data)}"
        if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            p = p.with_suffix(sniff_ext(data))   # 无扩展名/占位名按内容定型
        p.parent.mkdir(parents=True, exist_ok=True)
        # 原子写入：先写临时文件再替换，避免并发/中断留下半张图
        tmp = p.with_name(p.stem + f".tmp-{uuid.uuid4().hex[:6]}{p.suffix}")
        tmp.write_bytes(data)
        os.replace(tmp, p)
        paths.append(p)
    return paths


def dry_run_report(prov_name: str, prov: dict, kind: str, args):
    key_env = prov.get("api_key_env")
    env_key = os.environ.get(key_env) if key_env else None
    cfg_key = prov.get("api_key")
    has_key = bool(env_key or cfg_key)
    wh = parse_size(args.size)
    print(json.dumps({
        "dry_run": True,
        "profile": prov_name,
        "kind": kind,
        "mode": ("mj (reserved)" if kind == "mj"
                 else "image-edit" if args.ref else "text-to-image"),
        "endpoint_kind": ("mj (not implemented)" if kind == "mj" else
                          "images/edits (multipart)" if (kind == "openai" and args.ref)
                          else "images/generations" if kind == "openai"
                          else "models.generateContent"),
        "base_url": prov.get("base_url"),
        "base_url_candidates": base_url_variants(prov.get("base_url", "")),
        "model": args.model or prov.get("model"),
        "api_key_source": ("env:" + key_env if env_key
                           else "config:api_key" if cfg_key else "MISSING"),
        "has_key": has_key,
        "prompt_chars": len(args.prompt or ""),
        "refs": args.ref or [],
        "mask": args.mask,
        "n": args.n or 1,
        "size_mapping": map_size_for(kind, wh),
        "quality": args.quality,
        "out": args.out,
    }, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- batch

BATCH_KEYS = {"prompt", "provider", "model", "size", "quality", "n",
              "ref", "mask", "format", "out", "out_dir", "timeout"}
BATCH_STR_KEYS = {"prompt", "provider", "model", "size", "mask", "out",
                  "out_dir"}
QUALITY_CHOICES = {"low", "medium", "high", "auto"}
FORMAT_CHOICES = {"png", "jpeg", "webp"}


def validate_batch_job(ln: int, job: dict):
    """batch 任务字段类型与取值校验（绕过 argparse 的入口必须补齐约束）。"""
    for key in BATCH_STR_KEYS:
        if key in job and job[key] is not None and not isinstance(job[key], str):
            raise UserError(f"batch 第 {ln} 行字段 '{key}' 应为字符串")
    for key in ("n", "timeout"):
        if key in job and job[key] is not None \
                and (not isinstance(job[key], int) or isinstance(job[key], bool)
                     or job[key] < 1):
            raise UserError(f"batch 第 {ln} 行字段 '{key}' 应为 >=1 的整数")
    if job.get("quality") is not None \
            and job["quality"] not in QUALITY_CHOICES:
        raise UserError(f"batch 第 {ln} 行 quality 非法：{job['quality']!r}"
                        f"（可选 {'/'.join(sorted(QUALITY_CHOICES))}）")
    if job.get("format") is not None and job["format"] not in FORMAT_CHOICES:
        raise UserError(f"batch 第 {ln} 行 format 非法：{job['format']!r}"
                        f"（可选 {'/'.join(sorted(FORMAT_CHOICES))}）")
    ref = job.get("ref")
    if ref is not None:
        if not isinstance(ref, list) or \
                not all(isinstance(p, str) for p in ref):
            raise UserError(f"batch 第 {ln} 行 ref 应为字符串数组")


def do_batch(cfg: dict, args) -> int:
    jobs_path = Path(args.input).expanduser()
    if not jobs_path.is_file():
        raise UserError(f"batch 输入不存在：{jobs_path}")
    jobs: list[dict] = []
    for ln, line in enumerate(jobs_path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            job = json.loads(line)
        except Exception as exc:
            raise UserError(f"batch 第 {ln} 行不是合法 JSON：{exc}")
        unknown = set(job) - BATCH_KEYS
        if unknown:
            raise UserError(f"batch 第 {ln} 行含未知字段：{sorted(unknown)}")
        validate_batch_job(ln, job)
        jobs.append(job)
    if not jobs:
        raise UserError("batch 输入没有任务")
    conc = max(1, min(args.concurrency, len(jobs)))

    out_dir = Path(args.out_dir or cfg.get("output_dir", "output/imagegen"))
    ok_count, fail_count = 0, 0

    def run_one(idx: int, job: dict) -> tuple[int, str]:
        ns = argparse.Namespace(
            prompt=job.get("prompt"), provider=job.get("provider"),
            model=job.get("model"), size=job.get("size"),
            quality=job.get("quality"), n=job.get("n"),
            ref=list(job.get("ref") or []), mask=job.get("mask"),
            format=job.get("format"), out=job.get("out"),
            # 任务级 out_dir 优先，其次 batch 全局 out-dir，最后配置默认
            out_dir=job.get("out_dir") or args.out_dir
            or cfg.get("output_dir", "output/imagegen"),
            timeout=int(job.get("timeout") or DEFAULT_TIMEOUT),
            dry_run=False)
        try:
            paths = do_generate(cfg, ns)
            return idx, "; ".join(str(p) for p in paths)
        except (UserError, GenError) as exc:
            return idx, f"FAILED: {exc}"
        except Exception as exc:      # 意外异常兜底：不终止整个 batch
            return idx, f"FAILED: 意外错误 {type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=conc) as pool:
        futures = [pool.submit(run_one, i, j) for i, j in enumerate(jobs)]
        for fut in as_completed(futures):
            idx, result = fut.result()
            if result.startswith("FAILED:"):
                fail_count += 1
                err_file = out_dir / f"job-{idx + 1}.error.txt"
                err_file.parent.mkdir(parents=True, exist_ok=True)
                err_file.write_text(result, encoding="utf-8")
                eprint(f"[{PROG}] job {idx + 1}/{len(jobs)} 失败 → {err_file}")
            else:
                ok_count += 1
                print(f"job {idx + 1}/{len(jobs)} → {result}")
    eprint(f"[{PROG}] batch 完成：成功 {ok_count}，失败 {fail_count}")
    return 1 if fail_count else 0


# ---------------------------------------------------------------- cli

def cmd_providers(cfg: dict, _args) -> int:
    default = cfg.get("default_provider")
    rows = []
    for name, prov in cfg["providers"].items():
        env_name = prov.get("api_key_env")
        key_state = ("env:" + env_name if env_name and os.environ.get(env_name)
                     else "config" if prov.get("api_key") else "UNSET")
        rows.append({"profile": name + (" *" if name == default else ""),
                     "kind": prov.get("kind", "?"),
                     "model": prov.get("model", ""),
                     "base_url": prov.get("base_url", ""),
                     "key": key_state})
    widths = {k: max(len(str(r[k])) for r in rows + [{k: k}])
              for k in rows[0]}
    line = "  ".join(k.ljust(widths[k]) for k in rows[0])
    print(line)
    print("-" * len(line))
    for r in rows:
        print("  ".join(str(r[k]).ljust(widths[k]) for k in r))
    print("\n* = default_provider；key 仅显示来源，不显示值")
    return 0


def cmd_init_config(args) -> int:
    if args.path:
        target = Path(args.path).expanduser()
    else:
        # 与运行时同一套查找顺序（IMGGEN_CONFIG > CWD/.imggen.json > home）
        target = config_path()
    if target.exists() and not args.force:
        raise UserError(f"{target} 已存在；如需覆盖请加 --force")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2)
                      + "\n", encoding="utf-8")
    print(f"已写入配置模板：{target}")
    print("下一步：编辑该文件，或运行 python imggen.py setup 交互式向导"
          "（推荐，Key 不回显、自动连通测试）。")
    return 0


def cmd_models(cfg: dict, args) -> int:
    """列出渠道可用模型：远端 GET /models（免费）+ 内置图像模型清单。
    任一渠道远端不可达时返回非零。"""
    names = [args.provider] if args.provider else list(cfg["providers"])
    failed = False
    for name in names:
        prov = cfg["providers"].get(name)
        if prov is None:
            raise UserError(f"profile '{name}' 不存在；可用："
                            f"{', '.join(cfg['providers'])}")
        kind = prov.get("kind", "?")
        current = args.model or prov.get("model", "")
        print(f"\n== {name}  (kind={kind} · {prov.get('base_url', '')})")
        remote: list[str] | None = None
        err = ""
        try:
            remote, err = fetch_remote_models(prov, resolve_key(name, prov))
        except UserError as exc:
            err = str(exc)
        if remote is None:
            print(f"   远端模型列表不可达：{err or '未知错误'}；"
                  f"仅显示内置清单")
            failed = True
        else:
            hits = [m for m in remote
                    if any(h in m.lower() for h in IMAGE_MODEL_HINTS)]
            print(f"   远端 {len(remote)} 个模型，图像相关 {len(hits)} 个：")
            for m in hits:
                print(f"     - {m}" + ("   ← 当前" if m == current else ""))
        known = KNOWN_IMAGE_MODELS.get(kind, [])
        if known:
            print("   内置已知图像模型：")
            for m in known:
                print(f"     - {m}" + ("   ← 当前" if m == current else ""))
        print("   使用 -m <模型名> 单次覆盖；改 profile 的 model 字段设为默认")
    return 1 if failed else 0


def cmd_doctor(cfg: dict, args) -> int:
    """渠道体检：配置/Key/路由探测/输出目录。--fix 自动修正 base_url 写回。"""
    issues = ready = 0
    path = config_path()
    cfg_state = ("✓ 存在" if path.is_file()
                 else "✗ 缺失（运行 setup 向导或 init-config）")
    print(f"[config] {path} — {cfg_state}")
    env_f = env_path()
    env_state = ("✓ 存在" if env_f.is_file() else "— 不存在（可选）")
    print(f"[.env  ] {env_f} — {env_state}")
    out_dir = cfg.get("output_dir", "output/imagegen")
    # 只读检查：不创建目录。目录存在则查写入位；不存在则查最近存在的祖先
    od = Path(out_dir)
    anc = od
    while not anc.exists() and anc != anc.parent:
        anc = anc.parent
    out_ok = bool(anc.exists()) and os.access(anc, os.W_OK)
    if out_ok:
        print(f"[output] {out_dir} — ✓ 可写")
    else:
        print(f"[output] {out_dir} — ✗ 不可写（父目录 {anc} 不存在或无写权限）")
        issues += 1
    for name, prov in cfg["providers"].items():
        kind = prov.get("kind", "?")
        if kind == "mj":
            print(f"[{name}] MJ 预留位，跳过")
            continue
        try:
            key = resolve_key(name, prov)
        except UserError:
            print(f"[{name}] ✗ Key 未配置（期望 env {prov.get('api_key_env')}"
                  f" 或 config api_key 字段）")
            issues += 1
            continue
        key_desc = (f"env:{prov['api_key_env']}"
                    if prov.get("api_key_env")
                    and os.environ.get(prov["api_key_env"]) else "config")
        results = [(v, *probe_base(v, kind, key))
                   for v in base_url_variants(prov.get("base_url", ""))]
        cur = results[0]
        if cur[1]:
            print(f"[{name}] ✓ {cur[0]}（{cur[2]}）· Key {key_desc}")
            ready += 1
            continue
        good = next((r for r in results if r[1]), None)
        if good:
            print(f"[{name}] ✗ {cur[0]} 不可用（{cur[2]}）；"
                  f"{good[0]} 可用（{good[2]}）")
            if args.fix and remember_base(name, results[0][0], good[0]):
                ready += 1
                print("       → 已自动修正并写回配置")
            elif args.fix:
                print("       → 写回配置失败（见 stderr），本次按可用地址"
                      "探测但下次仍会重试")
                issues += 1
            else:
                print("        → 运行 imggen doctor --fix 可自动写回")
                issues += 1
        else:
            print(f"[{name}] ✗ 所有变体均不可达："
                  + "；".join(f"{r[0]} → {r[2]}" for r in results))
            issues += 1
    print(f"\n汇总：{ready} 个渠道就绪，{issues} 个问题")
    return 1 if issues else 0


def cmd_setup(cfg: dict, args) -> int:
    """交互式配置向导：渠道 → base_url → Key(getpass) → 模型 → 连通测试 → 写配置。"""
    import getpass

    interactive = sys.stdin.isatty()
    if not interactive and not args.yes:
        raise UserError(
            "非交互终端未加 --yes：为防止静默覆盖配置，脚本场景必须显式"
            "确认（--yes）。同时请提供 --kind/--provider/--model 等参数。")

    def ask(prompt: str, default: str = "") -> str:
        try:
            v = input(prompt).strip()
        except EOFError:
            raise UserError("需要交互式终端；脚本场景请提供全部参数 + --yes"
                            ) from None
        return v or default

    # 1) 渠道类型
    if args.kind in ("openai", "gemini"):
        kind = args.kind
    elif interactive:
        print("选择渠道类型：")
        print("  1) OpenAI 兼容（官方 api.openai.com 或任意第三方中转站）")
        print("  2) Google Gemini（AI Studio Key，Nano Banana 有免费额度）")
        kind = "gemini" if ask("序号 [1]: ", "1") == "2" else "openai"
    else:
        raise UserError("非交互环境需要 --kind openai|gemini 与其余参数 + --yes")

    # 2) base_url
    default_base = ("https://generativelanguage.googleapis.com/v1beta"
                    if kind == "gemini" else "https://api.openai.com/v1")
    base = args.base_url
    if not base and interactive:
        base = ask(f"base_url（可不含 /v1，会自动探测）[{default_base}]: ",
                   default_base)
    base = (base or default_base).rstrip("/")

    # 3) profile 名
    name = args.provider
    if not name and interactive:
        name = ask("profile 名称: ", "gemini" if kind == "gemini" else "relay")
    if not name:
        raise UserError("--yes 非交互模式需要 --provider")
    if name in cfg.get("providers", {}) and not args.force:
        raise UserError(f"profile '{name}' 已存在；--force 可覆盖")

    # 4) Key：环境变量名 + 可选即时录入（getpass 不回显，写入 ~/.imggen/.env）
    env_name = args.api_key_env
    if not env_name and interactive:
        env_name = ask(f"Key 的环境变量名 [{name.upper()}_API_KEY]: ",
                       f"{name.upper()}_API_KEY")
    env_name = env_name or f"{name.upper()}_API_KEY"

    key_value = ""
    if os.environ.get(env_name):
        print(f"  环境变量 {env_name} 已有 Key，直接复用")
    elif interactive:
        key_value = getpass.getpass(
            "粘贴 API Key（不回显；回车跳过稍后自行配置）: ").strip()
        if key_value:
            env_file = env_path()
            upsert_env_key(env_file, env_name, key_value)
            print(f"  已写入 {env_file}（{env_name}=***）")

    # 5) 模型：远端列表优先（过滤图像相关），内置清单兜底
    model = args.model
    if not model:
        probe_key = key_value or os.environ.get(env_name, "")
        pool = list(KNOWN_IMAGE_MODELS.get(kind, []))
        if probe_key:
            ids, _err = fetch_remote_models({"kind": kind, "base_url": base},
                                            probe_key)
            hits = [m for m in (ids or [])
                    if any(h in m.lower() for h in IMAGE_MODEL_HINTS)]
            if hits:
                pool = hits
        if interactive and pool:
            print("选择默认模型：")
            for i, m in enumerate(pool, 1):
                print(f"  {i}) {m}")
            sel = ask("序号 [1]: ", "1")
            model = (pool[int(sel) - 1]
                     if sel.isdigit() and 1 <= int(sel) <= len(pool) else sel)
        elif pool:
            model = pool[0]
        else:
            raise UserError("--yes 非交互模式需要 --model")

    # 6) 免费连通测试（GET /models；自动尝试 base_url 变体并采纳可用值）
    probe_key = key_value or os.environ.get(env_name, "")
    if probe_key:
        found = None
        for v in base_url_variants(base):
            ok, detail = probe_base(v, kind, probe_key)
            if ok:
                found = (v, detail)
                break
        if found:
            base = found[0]
            print(f"  连通测试 ✓ {base}（{found[1]}）")
        else:
            print(f"  连通测试 ✗ 所有变体均不可达（Key 或网络问题？）；"
                  f"仍按 {base} 保存")
    else:
        print("  未提供 Key，跳过连通测试（之后可运行 imggen doctor 复查）")

    # 7) 写配置 + 摘要
    providers = cfg.setdefault("providers", {})
    providers[name] = {"kind": kind, "base_url": base,
                       "model": model, "api_key_env": env_name}
    set_default = args.yes or (interactive
                               and ask("设为默认渠道？[Y]/n: ",
                                       "y").lower() != "n")
    if set_default or not cfg.get("default_provider"):
        cfg["default_provider"] = name
    path = config_path()
    atomic_write_text(path, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")

    print("\n配置完成：")
    print(f"  配置文件  {path}")
    print(f"  渠道      {name} (kind={kind} · {base})")
    print(f"  默认模型  {model}")
    print(f"  Key       env:{env_name}"
          + ("" if (key_value or os.environ.get(env_name))
             else "（尚未配置！写入 .env 或设置环境变量后可用）"))
    print(f"  默认渠道  {'是' if cfg['default_provider'] == name else '否'}")
    print('试一下: python imggen.py gen -p "测试" --dry-run')
    return 0


def add_generate_args(sp):
    sp.add_argument("-p", "--prompt", help="提示词（编辑指令）")
    sp.add_argument("--provider", help="渠道 profile 名（缺省用 default_provider）")
    sp.add_argument("-m", "--model", help="覆盖 profile 默认模型")
    sp.add_argument("-o", "--out", help="输出路径（n>1 时自动追加序号）")
    sp.add_argument("--out-dir", help=f"输出目录（缺省取配置 output_dir）")
    sp.add_argument("-s", "--size", help="WxH / square/portrait/landscape/tall/wide/auto")
    sp.add_argument("-q", "--quality", choices=["low", "medium", "high", "auto"],
                    help="质量档（OpenAI gpt-image 系列有效；缺省不传该字段）")
    sp.add_argument("-n", type=positive_int, help="生成张数")
    sp.add_argument("-r", "--ref", nargs="+", metavar="IMG",
                    help="参考图（触发编辑端点；Gemini 走多模态输入）")
    sp.add_argument("--mask", help="PNG mask（仅 OpenAI 编辑端点）")
    sp.add_argument("-f", "--format", choices=["png", "jpeg", "webp"],
                    help="输出格式（仅 OpenAI gpt-image 系列 output_format）")
    sp.add_argument("--timeout", type=positive_int, default=DEFAULT_TIMEOUT)
    sp.add_argument("--dry-run", action="store_true",
                    help="只打印请求计划（脱敏），不发网络请求")


# ---------------------------------------------------------------- env file

def load_env_file() -> int:
    """可选 .env 自动加载（路径与 setup 写入统一由 env_path() 决定）。
    绝不覆盖进程已有的环境变量；不打印任何值。"""
    path = env_path()
    if not path.is_file():
        return 0
    loaded = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name = name.strip()
            value = value.strip().strip('"').strip("'")
            if name and value and name not in os.environ:
                os.environ[name] = value
                loaded += 1
    except OSError as exc:
        eprint(f"[{PROG}] 警告：读取 {path} 失败：{exc}")
        return 0
    return loaded


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):     # Windows 控制台编码保险
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    n_env = load_env_file()
    if n_env:
        eprint(f"[{PROG}] 已从 .env 加载 {n_env} 个变量")

    ap = argparse.ArgumentParser(prog=PROG,
                                 description="多 provider 图像生成 CLI")
    ap.add_argument("--version", action="version", version=f"{PROG} {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def run_generate(cfg, a):
        do_generate(cfg, a)          # 路径打印由 do_generate 负责
        return 0

    def run_edit(cfg, a):
        # 图生图显式入口：强制参考图，防止"编辑请求"被静默降级成重新生成
        if not a.ref:
            raise UserError(
                "edit 命令必须提供至少一张 --ref 参考图；"
                "纯文字生成请使用 gen 命令。")
        do_generate(cfg, a)
        return 0

    g = sub.add_parser("generate", aliases=["gen"], help="生成/编辑图片")
    add_generate_args(g)
    g.set_defaults(fn=run_generate)

    e = sub.add_parser("edit", aliases=["i2i"],
                       help="图生图：基于参考图编辑/融合（强制 --ref）")
    add_generate_args(e)
    e.set_defaults(fn=run_edit)

    b = sub.add_parser("batch", help="JSONL 批量生成")
    b.add_argument("-i", "--input", required=True, help="JSONL 任务文件")
    b.add_argument("--out-dir", help="输出根目录")
    b.add_argument("--concurrency", type=positive_int, default=2)
    b.set_defaults(fn=do_batch)

    p = sub.add_parser("providers", help="列出渠道 profile")
    p.set_defaults(fn=cmd_providers)

    ic = sub.add_parser("init-config", help="写出默认配置模板")
    ic.add_argument("--path", help="目标路径（缺省 ~/.imggen/config.json）")
    ic.add_argument("--force", action="store_true")
    ic.set_defaults(fn=lambda _c, a: cmd_init_config(a))

    st = sub.add_parser("setup",
                        help="交互式配置向导：渠道/Key/模型/连通测试")
    st.add_argument("--provider", help="profile 名（缺省交互询问）")
    st.add_argument("--kind", choices=["openai", "gemini"],
                    help="渠道类型（缺省交互选择）")
    st.add_argument("--base-url", help="API 地址（可不含 /v1，会自动探测）")
    st.add_argument("--api-key-env", help="Key 的环境变量名")
    st.add_argument("--model", help="默认模型（缺省从远端/内置清单选择）")
    st.add_argument("--yes", action="store_true",
                    help="非交互模式：配合上述参数直接写入")
    st.add_argument("--force", action="store_true", help="覆盖同名 profile")
    st.set_defaults(fn=cmd_setup)

    md = sub.add_parser("models", help="列出渠道可用模型（远端 + 内置清单）")
    md.add_argument("--provider", help="只看指定 profile")
    md.add_argument("-m", "--model", help="额外标注的当前模型名")
    md.set_defaults(fn=cmd_models)

    doc = sub.add_parser("doctor", help="渠道体检：Key / 路由探测 / 输出目录")
    doc.add_argument("--fix", action="store_true",
                     help="发现路由问题时自动修正 base_url 并写回配置")
    doc.set_defaults(fn=cmd_doctor)

    args = ap.parse_args(argv)
    try:
        if getattr(args, "cmd", None) == "init-config":
            return args.fn({}, args)
        if getattr(args, "cmd", None) in ("setup", "doctor"):
            # 两者都应在"无配置"状态下可运行：setup 创建配置，doctor 报告缺失
            return args.fn(load_config(require=False), args)
        cfg = load_config()
        rc = args.fn(cfg, args)
        return int(rc or 0)
    except UserError as exc:
        eprint(f"[{PROG}] 配置/参数错误：{exc}")
        return 2
    except GenError as exc:
        eprint(f"[{PROG}] 生成失败：{exc}")
        return 1
    except KeyboardInterrupt:
        eprint(f"\n[{PROG}] 已中断")
        return 130
    except BrokenPipeError:      # 下游（管道/编辑器）提前关闭 stdout，静默退出
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0
    except Exception as exc:      # 防意外 traceback：任何未预期错误都友好退出
        eprint(f"[{PROG}] 未预期的错误（{type(exc).__name__}）：{exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
