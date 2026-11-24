#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code 会话列表服务 —— 单文件版

扫描 ~/.claude/projects/*/*.jsonl，提供 Web 界面浏览/搜索会话，
查看最新内容，复制带 cd 的 resume 命令。

运行：python app.py
然后浏览器访问 http://127.0.0.1:8765
"""

import json
import os
import sys
import glob
import re
import shutil
import subprocess
import time
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------- 配置 ----------
HOST = "127.0.0.1"
PORT = 8765
CLAUDE_DIR = os.path.join(os.path.expanduser("~"), ".claude")
PROJECTS_DIR = os.path.join(CLAUDE_DIR, "projects")
MODEL = "glm-5.4"  # 默认模型；可在页面「设置」里覆盖（存 settings.json）
DEFAULT_PAGE_SIZE = 8
# 每个会话内存中保留的最近消息条数（供"查看"弹窗显示最新 N 条用）
DETAIL_KEEP = 8
DETAIL_FIRST = 5     # 详情弹窗首次加载的消息条数
DETAIL_MORE = 20     # 「加载更多」每次向前取的条数
SEARCH_MIN = 2       # 全文搜索最短字符数
SEARCH_BUDGET = 4.0  # 全文搜索的时间预算（秒），超时返回部分结果并标记 partial
RETENTION_DAYS = 30  # Claude Code 默认的会话保留天数（用于「即将清理」提示）

HERE = os.path.dirname(os.path.abspath(__file__))
FAV_FILE = os.path.join(HERE, "favorites.json")
ARCHIVE_DIR = os.path.join(HERE, "archive")  # 收藏会话的备份副本

# ---------- 浏览器自动打开 ----------
# Chrome 可能的安装路径（优先 64 位，回退 32 位）
_CHROME_CANDIDATES = [
    os.path.join(os.environ.get("ProgramFiles", ""), "Google", "Chrome", "Application", "chrome.exe"),
    os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
]


def find_chrome():
    """返回 chrome.exe 路径，找不到返回 None。"""
    for p in _CHROME_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return None


def open_browser(url):
    """优先用 Chrome 打开 url，没有 Chrome 则用系统默认浏览器。（当前由 VBS 启动器调用，保留备用）"""
    chrome = find_chrome()
    try:
        if chrome:
            subprocess.Popen([chrome, url])
        else:
            # Windows 默认浏览器
            os.startfile(url)  # noqa: S606
    except OSError:
        pass


# ---------- 数据读取 ----------
def _extract_text(content):
    """从 message.content 提取纯文本（content 可能是 str 或 list）。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "tool_result":
                # 工具结果跳过，对会话预览意义不大
                continue
        return "\n".join(p for p in parts if p)
    return ""


def parse_session(jsonl_path):
    """解析单个会话 jsonl，返回会话摘要 dict 或 None。

    性能：只线性扫描一次，但不把全部消息存进内存——
    列表只需摘要 + 最近 DETAIL_KEEP 条消息（供"查看"弹窗用），其余仅计数。
    """
    name = None          # summary 行
    ai_title = None      # aiTitle（终端窗口标题，最优先）
    first_user_text = None
    last_ts = None
    cwd = None
    session_id = os.path.splitext(os.path.basename(jsonl_path))[0]
    msg_count = 0
    # 用 deque 保留最近 N 条，避免大文件占内存
    recent = deque(maxlen=DETAIL_KEEP)

    try:
        # errors="replace"：个别坏字节不应导致整个会话解析失败
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # 终端窗口标题（每行都可能带，取最后一次）
                if o.get("aiTitle"):
                    ai_title = o["aiTitle"]

                t = o.get("type")
                # 会话名：取最后一条 summary
                if t == "summary":
                    name = o.get("summary") or name
                    continue

                ts = o.get("timestamp")
                if ts:
                    last_ts = ts  # 逐行覆盖，最后一条即最新

                if t in ("user", "assistant"):
                    m = o.get("message") or {}
                    text = _extract_text(m.get("content"))
                    if t == "user":
                        if cwd is None and o.get("cwd"):
                            cwd = o.get("cwd")
                        # 跳过系统/命令注入（<local-command-caveat>、<command-message> 等）
                        # 只把真实用户文本记为会话名/预览
                        if text and not first_user_text and not text.lstrip().startswith("<"):
                            first_user_text = text
                    if cwd is None and o.get("cwd"):
                        cwd = o.get("cwd")
                    if text:
                        msg_count += 1
                        recent.append({"r": t, "t": text})
    except OSError:
        return None

    if msg_count == 0 and not name and not ai_title:
        return None

    # 会话名优先级：aiTitle（终端窗口标题）> summary > 首条用户消息截断
    if ai_title:
        name = ai_title.strip()
    elif not name:
        # 取首条消息的第一行作为标题，不再在标点处截断；超出部分交给界面省略
        lines = [ln.strip() for ln in (first_user_text or "").splitlines() if ln.strip()]
        base = lines[0] if lines else "(无标题会话)"
        name = base[:60] + ("…" if len(base) > 60 else "")

    preview = (first_user_text or "").strip().replace("\n", " ")
    if len(preview) > 100:
        preview = preview[:100] + "…"

    # 项目名 = cwd 或文件夹名的最后一层
    if cwd:
        proj = os.path.basename(cwd.rstrip("\\/")) or cwd
    else:
        proj = os.path.basename(os.path.dirname(jsonl_path))

    return {
        "id": session_id,
        "name": name,
        "proj": proj,
        "path": cwd or "",
        "preview": preview,
        "msgs": msg_count,
        "time": last_ts or "",
        "_recent": list(recent),
        "_path": jsonl_path,  # 原始 jsonl 路径，归档/详情/搜索用，不输出到接口
    }


def load_all_sessions():
    """扫描所有项目目录，返回会话列表（按时间倒序）。

    带 mtime 增量缓存：未修改的 jsonl 复用上次解析结果，
    只有变化的文件才重新解析（避免每次请求重读上百 MB）。
    """
    pattern = os.path.join(PROJECTS_DIR, "*", "*.jsonl")
    cached = _CACHE["by_path"]  # path -> (mtime, session_dict)
    current_paths = set()
    sessions = []

    for path in glob.glob(pattern):
        current_paths.add(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        hit = cached.get(path)
        if hit and hit[0] == mtime:
            sessions.append(hit[1])
        else:
            s = parse_session(path)
            if s:
                cached[path] = (mtime, s)
                sessions.append(s)
            elif path in cached:
                del cached[path]

    # 清理已删除的文件
    for stale in list(cached):
        if stale not in current_paths:
            del cached[stale]

    sessions.sort(key=lambda x: x["time"], reverse=True)
    return sessions


# ---------- 收藏（持久化到 favorites.json，存 {id: 备注}）----------
def load_favorites():
    """返回 dict: {session_id: 备注}。备注可为空字符串。"""
    try:
        with open(FAV_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return {str(k): (v or "") for k, v in data.items()}
            # 兼容旧的 list 格式
            if isinstance(data, list):
                return {str(k): "" for k in data}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_favorites(favs):
    try:
        with open(FAV_FILE, "w", encoding="utf-8") as f:
            json.dump(favs, f, ensure_ascii=False)
    except OSError:
        pass


# ---------- 收藏归档（收藏的会话备份到 archive/，防被 Claude Code 的 30 天清理删掉） ----------
# 会话 id 一定是 UUID；接口里拿 id 拼 archive 路径前先校验，防路径穿越
_SID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _archive_file(sid):
    """返回 sid 的归档副本路径；不存在或 id 非法返回 None。"""
    if not _SID_RE.match(sid or ""):
        return None
    p = os.path.join(ARCHIVE_DIR, sid + ".jsonl")
    return p if os.path.isfile(p) else None


def load_archive_meta():
    """返回 {id: {name, proj, path, msgs, time, orig, archived_at}}；文件损坏时返回空 dict。"""
    try:
        with open(os.path.join(ARCHIVE_DIR, "meta.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    return {}


def save_archive_meta(meta):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    with open(os.path.join(ARCHIVE_DIR, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)


def sync_archive():
    """把收藏且仍存活的会话备份到 archive/；源文件更新过就重新备份。

    在 refresh() 内调用（已持锁）。取消收藏不删备份——多留一份没有坏处。
    """
    favs = load_favorites()
    if not favs:
        return
    meta = load_archive_meta()
    changed = False
    for sid in favs:
        s = INDEX_CACHE.get(sid)
        if not s:
            continue  # 已被清理的会话没有源文件可备份，靠已有归档
        src = s.get("_path")
        dst = os.path.join(ARCHIVE_DIR, sid + ".jsonl")
        try:
            need = (not os.path.isfile(dst)
                    or os.path.getmtime(dst) < os.path.getmtime(src))
        except OSError:
            continue
        if not need:
            # 已有备份且是最新的：meta 里缺条目（老版本归档）则补上
            if sid not in meta:
                meta[sid] = _archive_meta_of(s, src)
                changed = True
            continue
        try:
            os.makedirs(ARCHIVE_DIR, exist_ok=True)
            shutil.copyfile(src, dst)
        except OSError:
            continue
        meta[sid] = _archive_meta_of(s, src)
        changed = True
    if changed:
        try:
            save_archive_meta(meta)
        except OSError:
            pass


def _archive_meta_of(s, src):
    return {
        "name": s["name"], "proj": s["proj"], "path": s["path"],
        "msgs": s["msgs"], "time": s["time"],
        "orig": src,  # 恢复时写回的原始位置
        "archived_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def restore_from_archive(sid):
    """把归档副本写回原位置，让会话重新可被 resume。返回 (ok, 错误信息)。"""
    if not _SID_RE.match(sid or ""):
        return False, "会话 id 不合法"
    meta = load_archive_meta()
    m = meta.get(sid)
    src = _archive_file(sid)
    if not m or not src:
        return False, "归档不存在"
    dst = m.get("orig") or ""
    if not dst or not os.path.isdir(os.path.dirname(dst)):
        return False, "找不到原始位置（projects 目录可能已移动）"
    try:
        shutil.copyfile(src, dst)
    except OSError as e:
        return False, f"写回失败：{e}"
    refresh()
    return True, ""


# ---------- 详情：按需读整个会话的消息（供弹窗「加载更多」翻历史） ----------
_FULL_CACHE = {}  # id -> (mtime, msgs)；LRU，容量 8


def _scan_messages(path):
    """流式读 jsonl，按顺序返回全部 user/assistant 消息文本 [{r, t}]。"""
    out = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = o.get("type")
                if t in ("user", "assistant"):
                    text = _extract_text((o.get("message") or {}).get("content"))
                    if text:
                        out.append({"r": t, "t": text})
    except (OSError, UnicodeDecodeError):
        pass
    return out


def full_messages(sid):
    """返回 sid 的完整消息列表（带 mtime 缓存）；会话既不在索引也没有归档时返回 None。"""
    s = INDEX_CACHE.get(sid)
    path = s["_path"] if s else _archive_file(sid)
    if not path:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    hit = _FULL_CACHE.get(sid)
    if hit and hit[0] == mtime:
        _FULL_CACHE[sid] = _FULL_CACHE.pop(sid)  # LRU 触碰
        return hit[1]
    msgs = _scan_messages(path)
    _FULL_CACHE[sid] = (mtime, msgs)
    while len(_FULL_CACHE) > 8:
        _FULL_CACHE.pop(next(iter(_FULL_CACHE)))
    return msgs


# ---------- 全文搜索 ----------
def search_all(q):
    """在所有会话的 jsonl 里找 q（已小写）。返回 ([{id, hits, snippets}], partial)。

    按会话时间从新到旧扫描，超过 SEARCH_BUDGET 秒停止并标记 partial。
    """
    results = []
    partial = False
    deadline = time.monotonic() + SEARCH_BUDGET
    order = sorted(INDEX_CACHE.items(), key=lambda kv: kv[1]["time"], reverse=True)
    for sid, s in order:
        if time.monotonic() > deadline:
            partial = True
            break
        path = s.get("_path")
        if not path:
            continue
        hits = 0
        snippets = []
        try:
            f = open(path, "r", encoding="utf-8")
        except OSError:
            continue
        try:
            for line in f:
                if q not in line.lower():
                    continue
                hits += 1
                if len(snippets) < 2:
                    snip = _snippet_of(line, q)
                    if snip:
                        snippets.append(snip)
        except (OSError, UnicodeDecodeError):
            pass
        finally:
            f.close()
        if hits:
            results.append({"id": sid, "hits": hits, "snippets": snippets})
    return results, partial


def _snippet_of(line, q):
    """从命中行提取一段可读文本（优先 message 正文），截取关键词前后各一段。"""
    text = ""
    try:
        o = json.loads(line)
        text = (_extract_text((o.get("message") or {}).get("content"))
                or o.get("summary") or "")
    except json.JSONDecodeError:
        text = ""
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    idx = text.lower().find(q)
    if idx < 0:
        idx = 0
    lo = max(0, idx - 40)
    out = text[lo:idx + len(q) + 90]
    return ("…" if lo > 0 else "") + out + ("…" if idx + len(q) + 90 < len(text) else "")


# ---------- 设置（持久化到 settings.json：{model, theme}）----------
SETTINGS_FILE = os.path.join(HERE, "settings.json")
# 模型名会被拼进 claude 命令行，只放行常见的模型 ID 字符，防止夹带其他参数
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:\[\]/-]+$")


def load_settings():
    """返回 {"model": str（空=用默认）, "theme": "light"|"dark"}；缺项或非法值回落到默认。"""
    out = {"model": "", "theme": "light"}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError):
        return out
    if isinstance(data, dict):
        m = str(data.get("model") or "").strip()
        t = str(data.get("theme") or "").strip()
        if _MODEL_RE.match(m):
            out["model"] = m
        if t in ("light", "dark"):
            out["theme"] = t
    return out


def save_settings(s):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)


def current_settings():
    """页面和「打开终端」实际使用的设置（model 已回落为默认值）。"""
    s = load_settings()
    return {"model": s["model"] or MODEL, "defaultModel": MODEL, "theme": s["theme"]}


# ---------- HTTP 服务 ----------
PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claude 会话管理</title>
<style>
  :root{
    --bg:#faf9f7; --surface:#ffffff; --surface2:#f4f2ee; --surface3:#ebe8e2;
    --border:#e7e3dc; --border-strong:#d3cdc3;
    --text:#1f1d1a; --text2:#4f4a44; --muted:#756f68;
    --accent:#d97757; --accent-ink:#b35a39; --accent-soft:#fbece5;
    --ring:0 0 0 3px rgba(217,119,87,.22);
    --shadow:0 1px 2px rgba(40,30,20,.05),0 12px 32px -8px rgba(40,30,20,.10);
    --r:10px;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{
    font-family:"Segoe UI Variable Text","Segoe UI","Noto Sans SC","Microsoft YaHei UI","Microsoft YaHei",sans-serif;
    font-size:14px;line-height:1.5;color:var(--text);background:var(--bg);min-height:100vh;
    padding:40px 28px 56px;-webkit-font-smoothing:antialiased;
  }
  button,input,select{font:inherit;color:inherit}
  svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex:none}
  .wrap{max-width:1120px;margin:0 auto}

  /* ---------- 页头 ---------- */
  .hd{display:flex;align-items:center;gap:16px;margin-bottom:14px}
  .hd-tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;flex:1;min-width:0}
  .hd-tools>#setBtn{margin-left:auto} /* 搜索、下拉靠左；设置和页签靠右 */
  .seg{display:inline-flex;background:var(--surface2);border:1px solid var(--border);border-radius:11px;padding:3px;gap:2px}
  .seg button{border:0;background:transparent;color:var(--text2);font-size:13px;font-weight:500;padding:6px 14px;border-radius:8px;
    cursor:pointer;display:inline-flex;align-items:center;gap:7px;transition:background .15s,color .15s}
  .seg button:hover{color:var(--text)}
  .seg button.active{background:var(--surface);color:var(--text);font-weight:600;
    box-shadow:0 1px 2px rgba(40,30,20,.10),0 0 0 1px rgba(40,30,20,.04)}
  .seg .badge{font-size:11.5px;font-weight:500;color:var(--muted);background:var(--surface3);border-radius:999px;padding:0 7px;
    line-height:1.6;font-variant-numeric:tabular-nums}
  .seg button.active .badge{background:var(--accent-soft);color:var(--accent-ink)}

  /* ---------- 工具栏 ---------- */
  .search{flex:0 1 300px;min-width:200px;position:relative;display:flex;align-items:center}
  .search>svg{position:absolute;left:12px;color:var(--muted);pointer-events:none}
  .search input{width:100%;height:40px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r);
    padding:0 14px 0 38px;font-size:13.5px;outline:none;transition:border-color .15s,box-shadow .15s}
  .search input::placeholder{color:var(--muted)}
  .search input:focus{border-color:var(--accent);box-shadow:var(--ring)}
  .search kbd{position:absolute;right:10px;font-family:inherit;font-size:11.5px;color:var(--muted);background:var(--surface2);
    border:1px solid var(--border);border-radius:5px;padding:0 6px;line-height:1.6}
  .search input:focus+kbd,.search input:not(:placeholder-shown)+kbd{display:none}
  .select{position:relative;display:flex;align-items:center}
  .select select{appearance:none;-webkit-appearance:none;height:40px;max-width:200px;background:var(--surface);border:1px solid var(--border);
    border-radius:var(--r);padding:0 34px 0 14px;font-size:13.5px;outline:none;cursor:pointer;
    text-overflow:ellipsis;transition:border-color .15s,box-shadow .15s}
  .select select:focus{border-color:var(--accent);box-shadow:var(--ring)}
  .select>svg{position:absolute;right:12px;color:var(--muted);pointer-events:none}

  /* ---------- 会话列表 ---------- */
  .list{background:var(--surface);border:1px solid var(--border);border-radius:14px;box-shadow:var(--shadow);overflow:hidden}
  .row{display:grid;grid-template-columns:28px minmax(0,1fr) 92px auto;gap:14px;align-items:center;
    padding:11px 14px 11px 10px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .12s}
  .row:last-child{border-bottom:0}
  .row:hover{background:#fcfbf9}
  .txt{min-width:0}
  .name{font-size:14.5px;font-weight:600;letter-spacing:-.005em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .meta{display:flex;align-items:center;gap:8px;margin-top:4px;line-height:20px;font-size:12.5px;color:var(--muted);white-space:nowrap;overflow:hidden}
  .meta .sep{color:var(--border-strong)}
  .proj{font-family:"Cascadia Mono",ui-monospace,Consolas,monospace;font-size:12px;color:var(--text2);background:var(--surface2);
    padding:0 7px;border-radius:5px;line-height:20px;overflow:hidden;text-overflow:ellipsis;max-width:280px}
  .time{font-size:12.5px;color:var(--muted);text-align:right;font-variant-numeric:tabular-nums}
  .ops{display:flex;gap:6px;align-items:center}

  .iconbtn{width:28px;height:28px;display:inline-flex;align-items:center;justify-content:center;border:0;border-radius:7px;
    background:transparent;color:var(--border-strong);cursor:pointer;transition:background .15s,color .15s}
  .iconbtn:hover{background:var(--surface2);color:var(--muted)}
  .row:hover .iconbtn:not(.on){color:var(--muted)}
  .iconbtn.on{color:var(--accent)}
  .iconbtn.on:hover{background:var(--accent-soft);color:var(--accent-ink)}
  .iconbtn.on svg{fill:currentColor}

  .btn{height:32px;display:inline-flex;align-items:center;gap:6px;padding:0 12px;border-radius:8px;border:1px solid var(--border);
    background:var(--surface);color:var(--text2);font-size:12.5px;font-weight:500;cursor:pointer;white-space:nowrap;
    transition:background .15s,border-color .15s,color .15s,transform .05s}
  .btn svg{width:15px;height:15px}
  .btn:hover{background:var(--surface2);border-color:var(--border-strong);color:var(--text)}
  .btn:active{transform:translateY(1px)}
  .btn.primary{background:var(--accent-soft);border-color:transparent;color:var(--accent-ink)}
  .btn.primary:hover{background:var(--accent);color:#fff}
  .btn:disabled{opacity:.55;cursor:default;transform:none}

  .favnote-row{grid-column:2 / -1;display:flex;align-items:center;gap:6px}
  .favnote-row svg{width:14px;height:14px;color:var(--muted)}
  .favnote-row input{flex:1;height:28px;background:transparent;border:1px solid transparent;border-radius:6px;padding:0 8px;
    font-size:12.5px;color:var(--text2);outline:none;transition:background .15s,border-color .15s,box-shadow .15s}
  .favnote-row input:hover{background:var(--surface2)}
  .favnote-row input:focus{background:var(--surface);border-color:var(--accent);box-shadow:var(--ring)}
  .favnote-row input::placeholder{color:var(--muted)}

  /* ---------- 底部分页 ---------- */
  .foot{display:flex;justify-content:space-between;align-items:center;margin-top:16px;color:var(--muted);font-size:13px;flex-wrap:wrap;gap:12px}
  .pager{display:flex;gap:4px;align-items:center}
  .pager button{min-width:32px;height:32px;padding:0 10px;border:1px solid transparent;border-radius:8px;background:transparent;
    color:var(--text2);font-size:13px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;
    transition:background .15s,border-color .15s,color .15s;font-variant-numeric:tabular-nums}
  .pager button:hover:not(:disabled):not(.on){background:var(--surface);border-color:var(--border);color:var(--text)}
  .pager button.on{background:var(--surface);border-color:var(--border);color:var(--text);font-weight:600}
  .pager button:disabled{opacity:.35;cursor:default}
  .pager .dots{color:var(--muted);padding:0 2px}
  .empty{text-align:center;padding:72px 20px;color:var(--muted);font-size:13.5px}
  .empty svg{width:28px;height:28px;stroke-width:1.4;color:var(--border-strong);display:block;margin:0 auto 10px}

  /* ---------- 详情弹窗 ---------- */
  .mask{position:fixed;inset:0;background:rgba(30,26,22,.45);display:none;align-items:center;justify-content:center;
    z-index:50;backdrop-filter:blur(3px);padding:24px}
  .mask.show{display:flex}
  .modal{background:var(--surface);border:1px solid var(--border);border-radius:16px;width:720px;max-width:100%;max-height:84vh;
    display:flex;flex-direction:column;box-shadow:0 24px 64px -12px rgba(30,20,10,.35);animation:pop .16s ease-out}
  @keyframes pop{from{opacity:0;transform:translateY(6px) scale(.985)}to{opacity:1;transform:none}}
  .mhd{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;padding:18px 18px 14px 20px;border-bottom:1px solid var(--border)}
  .mhd h3{font-size:16px;font-weight:650;line-height:1.35;word-break:break-word}
  .msub{display:flex;gap:8px;align-items:center;color:var(--muted);font-size:12.5px;margin-top:6px;flex-wrap:wrap}
  .msub .sep{color:var(--border-strong)}
  .bd{padding:18px 20px;overflow:auto;flex:1}
  .cap{font-size:12px;color:var(--muted);margin-bottom:12px}
  .msg{display:flex;flex-direction:column;margin-bottom:14px;max-width:86%}
  .msg .who{font-size:11.5px;color:var(--muted);margin-bottom:4px;font-weight:500}
  .msg .bubble{border-radius:12px;padding:10px 14px;font-size:13.5px;line-height:1.65;white-space:pre-wrap;word-break:break-word;
    background:var(--surface2);max-height:260px;overflow:auto}
  .msg.user{align-items:flex-end;margin-left:auto}
  .msg.user .who{color:var(--accent-ink)}
  .msg.user .bubble{background:var(--accent-soft);border-top-right-radius:4px}
  .msg.assistant .bubble{border-top-left-radius:4px}
  .mft{display:flex;justify-content:flex-end;gap:8px;padding:12px 20px;border-top:1px solid var(--border)}

  /* ---------- 提示 ---------- */
  .toast{position:fixed;left:50%;bottom:32px;transform:translate(-50%,12px);display:flex;align-items:center;gap:8px;
    background:#26221e;color:#f5f2ee;padding:9px 16px;border-radius:10px;font-size:13px;opacity:0;pointer-events:none;
    transition:opacity .2s,transform .2s;z-index:99;max-width:90vw;box-shadow:0 8px 24px rgba(0,0,0,.2)}
  .toast.show{opacity:1;transform:translate(-50%,0)}
  .toast::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--accent);flex:none}

  /* ---------- 全文命中 / 临期提示 / 归档 / 新会话通知 ---------- */
  .hitn{color:var(--accent-ink);background:var(--accent-soft);border-radius:5px;padding:0 6px;line-height:20px;font-size:11.5px}
  .aging{display:block;color:#b0621d;font-size:11px;margin-top:2px}
  html[data-theme="dark"] .aging{color:#d79a55}
  .arch{font-size:11px;color:var(--muted);border:1px solid var(--border-strong);border-radius:5px;padding:0 6px;margin-left:8px;vertical-align:2px;white-space:nowrap}
  .newbar{position:fixed;right:24px;bottom:28px;display:none;background:#26221e;color:#f5f2ee;padding:10px 16px;
    border-radius:10px;cursor:pointer;font-size:13px;box-shadow:0 8px 24px rgba(0,0,0,.25);z-index:90}
  .newbar.show{display:block}
  .newbar:hover{background:#38322c}
  .snips{background:var(--surface2);border-radius:10px;padding:10px 14px 12px;margin-bottom:16px}
  .snips .cap{margin-bottom:2px}
  .snip{font-size:12.5px;color:var(--text2);margin-top:7px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .morebtn{display:block;width:100%;margin-bottom:14px;padding:8px 0;border:1px dashed var(--border-strong);border-radius:8px;
    background:transparent;color:var(--muted);font-size:12.5px;cursor:pointer;transition:color .15s,border-color .15s}
  .morebtn:hover:not(:disabled){color:var(--accent-ink);border-color:var(--accent)}
  .morebtn:disabled{opacity:.6;cursor:default}

  /* ---------- 设置 ---------- */
  #setBtn{font-family:"Cascadia Mono",ui-monospace,Consolas,monospace;font-size:12px;padding:0 10px 0 9px}
  #setBtn svg{color:var(--muted)}
  .modal.small{width:460px}
  #smask{z-index:60}
  .field{margin-bottom:18px}
  .field:last-child{margin-bottom:0}
  .field>label{display:block;font-size:12.5px;font-weight:600;color:var(--text2);margin-bottom:8px}
  .field input{width:100%;height:38px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r);
    padding:0 12px;font-size:13.5px;outline:none;font-family:"Cascadia Mono",ui-monospace,Consolas,monospace;
    transition:border-color .15s,box-shadow .15s}
  .field input:focus{border-color:var(--accent);box-shadow:var(--ring)}
  .field input::placeholder{font-family:inherit;color:var(--muted)}
  .hint{font-size:12px;color:var(--muted);margin-top:6px;line-height:1.6}
  .ferr{font-size:12px;color:#c2452f;margin-top:6px;display:none}
  .ferr.show{display:block}
  html[data-theme="dark"] .ferr{color:#ff8f78}
  .toast{border:1px solid rgba(255,255,255,.08)}

  /* ---------- 夜晚模式 ---------- */
  html[data-theme="dark"]{
    color-scheme:dark;
    --bg:#171513; --surface:#1f1c19; --surface2:#282420; --surface3:#342e28;
    --border:#2e2924; --border-strong:#48413a;
    --text:#f0ece6; --text2:#cbc4ba; --muted:#9a9287;
    --accent:#d97757; --accent-ink:#f0a98d; --accent-soft:#3a2a22;
    --ring:0 0 0 3px rgba(217,119,87,.3);
    --shadow:0 1px 2px rgba(0,0,0,.3),0 12px 32px -8px rgba(0,0,0,.5);
  }
  html[data-theme="dark"] .row:hover{background:#24211d}
  html[data-theme="dark"] .seg button.active{box-shadow:0 1px 2px rgba(0,0,0,.4),0 0 0 1px rgba(255,255,255,.06)}
  html[data-theme="dark"] .mask{background:rgba(0,0,0,.6)}
  html[data-theme="dark"] .modal{box-shadow:0 24px 64px -12px rgba(0,0,0,.7)}

  @media (max-width:760px){
    body{padding:24px 16px 48px}
    .hd{flex-direction:column;align-items:stretch}
    .hd-tools>#setBtn{margin-left:0}
    .row{grid-template-columns:28px minmax(0,1fr) auto}
    .time{display:none}
    .btn .lbl{display:none}
    .btn{padding:0 9px}
  }
  @media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
</style>
</head>
<body>
<script>try{if(localStorage.getItem('theme')==='dark')document.documentElement.dataset.theme='dark'}catch(e){}</script>
<div class="wrap">
  <div class="hd">
    <div class="hd-tools">
      <div class="search">
        <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <input id="q" placeholder="搜索名称、内容或备注…" autocomplete="off">
        <kbd>/</kbd>
      </div>
      <div class="select">
        <select id="proj"><option value="">全部项目</option></select>
        <svg viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
      </div>
      <button class="btn" id="setBtn" onclick="openSettings()" title="设置：外观与模型">
        <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
        <span id="setModel">…</span>
      </button>
      <div class="seg">
        <button id="tabAll" class="active" onclick="switchView('all')">全部 <span class="badge" id="allBadge">0</span></button>
        <button id="tabFav" onclick="switchView('fav')">收藏 <span class="badge" id="favBadge">0</span></button>
        <button id="tabArch" onclick="switchView('arch')">归档 <span class="badge" id="archBadge">0</span></button>
      </div>
    </div>
  </div>

  <div class="list" id="table">
    <div id="body"><div class="empty">正在加载…</div></div>
  </div>

  <div class="foot">
    <span id="count"></span>
    <div class="pager" id="pager"></div>
  </div>
</div>

<div class="mask" id="mask">
  <div class="modal">
    <div class="mhd">
      <div style="min-width:0">
        <h3 id="mtitle"></h3>
        <div class="msub" id="msub"></div>
      </div>
      <button class="iconbtn" onclick="closeM()" title="关闭">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="bd" id="mbody"></div>
    <div class="mft">
      <button class="btn" id="mCopy" title="复制 resume 命令到剪贴板">
        <svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>复制命令
      </button>
      <button class="btn primary" id="mOpen" title="在最近使用的 Windows Terminal 窗口里新开标签页，用 PowerShell 7 直接 resume 这个会话">
        <svg viewBox="0 0 24 24"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>打开终端
      </button>
    </div>
  </div>
</div>
<div class="mask" id="smask">
  <div class="modal small">
    <div class="mhd">
      <div><h3>设置</h3></div>
      <button class="iconbtn" onclick="closeSettings()" title="关闭">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="bd">
      <div class="field">
        <label>外观</label>
        <div class="seg" id="themeSeg">
          <button data-t="light"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="6.34" y2="6.34"/><line x1="17.66" y1="17.66" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="6.34" y2="17.66"/><line x1="17.66" y1="6.34" x2="19.07" y2="4.93"/></svg>白天模式</button>
          <button data-t="dark"><svg viewBox="0 0 24 24"><path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/></svg>夜晚模式</button>
        </div>
      </div>
      <div class="field">
        <label for="modelInput">模型</label>
        <input id="modelInput" placeholder="留空则使用默认模型" autocomplete="off" spellcheck="false">
        <p class="hint">「复制命令」和「打开终端」都会使用这里填的模型。默认模型：<span id="defaultModel"></span></p>
        <p class="ferr" id="setErr"></p>
      </div>
    </div>
    <div class="mft">
      <button class="btn" onclick="closeSettings()">取消</button>
      <button class="btn primary" id="setSave">保存</button>
    </div>
  </div>
</div>
<div class="newbar" id="newbar" title="点击查看"></div>
<div class="toast" id="toast"></div><script>
const DEFAULT_MODEL="__MODEL__";
const PAGE_SIZE=__PAGESIZE__;
const MODEL_RE=/^[A-Za-z0-9._:[\]/-]*$/;
let model=DEFAULT_MODEL;           // 当前生效的模型（设置里可改），用于复制命令和打开终端
let theme='light', themeSaved='light';
let ALL=[], FAV=[], ARCH=[], filtered=[], page=1, detailsCache={}, toastTimer=null;
let SEARCH_HITS={}, searchSeq=0, knownIds=null; // 全文搜索结果；轮询用的新会话 id 集合
const $=id=>document.getElementById(id);
const ICON={
  star:'<svg viewBox="0 0 24 24"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>',
  note:'<svg viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>',
  inbox:'<svg viewBox="0 0 24 24"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>',
  left:'<svg viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg>',
  right:'<svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>',
  copy:'<svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
  term:'<svg viewBox="0 0 24 24"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>'
};
const TERM_TITLE='在最近使用的 Windows Terminal 窗口里新开标签页，用 PowerShell 7 直接 resume 这个会话';

function fmtTime(s){
  if(!s) return '';
  const d=new Date(s);
  if(isNaN(d)) return s;
  const diff=(new Date()-d)/1000;
  if(diff<60) return '刚刚';
  if(diff<3600) return Math.floor(diff/60)+'分钟前';
  if(diff<86400) return Math.floor(diff/3600)+'小时前';
  if(diff<86400*7) return Math.floor(diff/86400)+'天前';
  return fmtFull(d).slice(0,10);
}
function fmtFull(s){
  if(!s) return '';
  const d=new Date(s);
  if(isNaN(d)) return s;
  const p=n=>String(n).padStart(2,'0');
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

let VIEW='all'; // 'all' | 'fav' | 'arch'
const findD=id=>ALL.find(x=>x.id===id)||FAV.find(x=>x.id===id)||ARCH.find(x=>x.id===id);
function rowHtml(d){
  const dead=d.archived&&!d.live; // 原会话已被清理，只剩归档副本
  const noteRow = VIEW!=='all'&&d.fav
    ? `<div class="favnote-row">${ICON.note}
         <input value="${esc(d.favNote||'')}" placeholder="添加备注…" onclick="event.stopPropagation()" onblur="saveNote('${d.id}',this.value)">
       </div>` : '';
  const hit=SEARCH_HITS[d.id];
  const hitn=hit?`<span class="sep">·</span><span class="hitn" title="全文搜索命中">全文 ${hit.hits} 处</span>`:'';
  const ageDays=d.time?(Date.now()-new Date(d.time))/864e5:0;
  const aging=!d.archived&&ageDays>=24
    ?`<span class="aging" title="Claude Code 默认保留 ${30} 天不用就自动删除，收藏可自动备份">还有 ${Math.max(1,Math.ceil(30-ageDays))} 天被清理</span>`:'';
  const archTag=dead?`<span class="arch" title="原会话已被 Claude Code 清理，这是归档副本">已归档</span>`:'';
  const ops = dead
    ? `<button class="btn primary" title="把归档副本写回 ~/.claude/projects，之后即可正常 resume" onclick="restore('${d.id}',event)">
         <svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
         <span class="lbl">恢复会话</span></button>
       <button class="btn" title="删除这份备份（删了就找不回了）" onclick="delArchive('${d.id}',event)">
         <svg viewBox="0 0 24 24"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
         <span class="lbl">删除备份</span></button>`
    : `<button class="btn" title="复制 resume 命令到剪贴板" onclick="copyCmd('${d.id}',event)">${ICON.copy}<span class="lbl">复制命令</span></button>
       <button class="btn primary" title="${TERM_TITLE}" onclick="openTerm('${d.id}',event)">${ICON.term}<span class="lbl">打开终端</span></button>`;
  return `<div class="row" onclick="view('${d.id}')" title="点击查看最近的消息">
    <button class="iconbtn ${d.fav?'on':''}" title="${d.fav?'取消收藏':'收藏（自动备份，不怕 30 天清理）'}" onclick="toggleFav('${d.id}',${!d.fav},event)">${ICON.star}</button>
    <div class="txt">
      <div class="name">${esc(d.name)}${archTag}</div>
      <div class="meta">
        <span class="proj" title="${esc(d.path)}">${esc(d.proj)}</span>
        <span class="sep">·</span><span>${d.msgs} 条消息</span>${hitn}
      </div>
    </div>
    <div class="time" title="最后活动：${fmtFull(d.time)}">${fmtTime(d.time)}${aging}</div>
    <div class="ops">${ops}</div>
    ${noteRow}
  </div>`;
}
function pageList(cur,pages){
  if(pages<=7) return Array.from({length:pages},(_,i)=>i+1);
  const set=new Set([1,2,cur-1,cur,cur+1,pages-1,pages]);
  const arr=[...set].filter(p=>p>=1&&p<=pages).sort((a,b)=>a-b), out=[];
  arr.forEach((p,i)=>{ if(i&&p-arr[i-1]>1) out.push('…'); out.push(p); });
  return out;
}
function renderPage(){
  const total=filtered.length;
  const pages=Math.max(1,Math.ceil(total/PAGE_SIZE));
  if(page>pages) page=pages;
  const slice=filtered.slice((page-1)*PAGE_SIZE,page*PAGE_SIZE);
  $('body').innerHTML = slice.length
    ? slice.map(rowHtml).join('')
    : `<div class="empty">${ICON.inbox}${VIEW==='fav'?'还没有收藏的会话':VIEW==='arch'?'还没有归档备份（收藏的会话会自动备份到这里）':'没有匹配的会话'}</div>`;
  $('count').textContent=`共 ${total} 个会话`;
  $('pager').innerHTML =
    `<button id="prev" title="上一页" ${page<=1?'disabled':''}>${ICON.left}</button>` +
    pageList(page,pages).map(p=>p==='…'?'<span class="dots">…</span>':`<button class="pg ${p===page?'on':''}" data-p="${p}">${p}</button>`).join('') +
    `<button id="next" title="下一页" ${page>=pages?'disabled':''}>${ICON.right}</button>`;
}

async function toggleFav(id,fav,ev){
  ev.stopPropagation();
  const d=findD(id);
  if(!d) return;
  const note=fav?(d.favNote||''):'';
  await fetch('/api/fav',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,fav,note})});
  d.fav=fav;
  if(!fav) d.favNote='';
  FAVS_COUNT=Math.max(0,FAVS_COUNT+(fav?1:-1));
  updateBadges();
  // 收藏视图里取消收藏：从当前列表移除
  if(VIEW==='fav'&&!fav){
    FAV=FAV.filter(x=>x.id!==id);
    filtered=filtered.filter(x=>x.id!==id);
  }
  renderPage();
  showToast(fav?'已收藏，已自动备份':'已取消收藏');
}
async function saveNote(id,note){
  const d=findD(id);
  if(!d) return;
  if(d.favNote===note) return;
  d.favNote=note;
  await fetch('/api/fav',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,fav:true,note})});
  showToast('备注已保存');
}
let FAVS_COUNT=0; // 服务端收藏总数（含已归档的）
function updateBadges(){
  $('favBadge').textContent=Math.max(FAVS_COUNT,ALL.filter(d=>d.fav).length);
  $('allBadge').textContent=ALL.length;
}
async function switchView(v){
  VIEW=v;
  $('tabAll').classList.toggle('active',v==='all');
  $('tabFav').classList.toggle('active',v==='fav');
  $('tabArch').classList.toggle('active',v==='arch');
  if(v==='fav'){
    // 收藏视图从后端拉最新收藏（含备注），保证与服务端一致
    const r=await fetch('/api/favs').then(r=>r.json());
    FAV=r.sessions||[];
    FAVS_COUNT=FAV.length; // 以收藏视图为准（含已归档）
    updateBadges();
  }else if(v==='arch'){
    const r=await fetch('/api/archives').then(r=>r.json());
    ARCH=r.sessions||[];
    $('archBadge').textContent=ARCH.length;
  }
  applyFilter();
}

function applyFilter(){
  const q=$('q').value.trim();
  const ql=q.toLowerCase();
  const proj=$('proj').value;
  const pool=VIEW==='all'?ALL:VIEW==='fav'?FAV:ARCH;
  const base=pool.filter(d=>{
    if(proj && d.proj!==proj) return false;
    if(!ql) return true;
    const detail=(detailsCache[d.id]||{list:[]}).list.map(m=>m.t).join(' ');
    return (d.name+' '+d.preview+' '+(d.favNote||'')+' '+detail).toLowerCase().includes(ql);
  });
  page=1;
  if(ql.length>=2){
    filtered=base; renderPage(); // 先渲染标题/备注匹配，全文结果异步补上
    const seq=++searchSeq;
    fetch('/api/search?q='+encodeURIComponent(q)).then(r=>r.json()).then(d=>{
      if(seq!==searchSeq) return; // 查询已变，丢弃过期结果
      SEARCH_HITS={};
      (d.results||[]).forEach(r=>SEARCH_HITS[r.id]=r);
      const inBase=new Set(base.map(x=>x.id));
      const extra=Object.keys(SEARCH_HITS)
        .map(id=>pool.find(x=>x.id===id))
        .filter(x=>x&&(!proj||x.proj===proj)&&!inBase.has(x.id))
        .sort((a,b)=>SEARCH_HITS[b.id].hits-SEARCH_HITS[a.id].hits);
      filtered=[...base,...extra];
      renderPage();
      if(d.partial) showToast('会话较多，只搜索了最近的一部分','err');
    }).catch(()=>{});
  }else{
    searchSeq++;
    SEARCH_HITS={};
    filtered=base;
    renderPage();
  }
}

let curId=null, curD=null;
async function view(id){
  const d=findD(id);
  if(!d) return;
  curId=id; curD=d;
  $('mtitle').textContent=d.name;
  $('msub').innerHTML=`<span class="proj">${esc(d.proj)}</span><span class="sep">·</span><span>${d.msgs} 条消息</span>
    <span class="sep">·</span><span>最后活动 ${fmtFull(d.time)}</span>${d.archived?'<span class="sep">·</span><span>已归档（只读副本）</span>':''}`;
  $('mbody').innerHTML='<div class="empty">正在加载…</div>';
  $('mask').classList.add('show');
  if(!detailsCache[id]){
    try{
      const r=await fetch('/api/detail?id='+id).then(r=>r.json());
      detailsCache[id]={list:r.msgs||[],total:r.total||0};
    }catch(e){
      if(curId===id) $('mbody').innerHTML='<div class="empty">加载失败，请关闭后重试</div>';
      return;
    }
  }
  if(curId!==id) return; // 期间已切到别的会话或关闭
  renderModal();
}
function renderModal(){
  const id=curId, d=curD, ent=detailsCache[id];
  if(!d||!ent) return;
  const hit=SEARCH_HITS[id];
  const snips=hit?`<div class="snips"><div class="cap">全文命中 ${hit.hits} 处</div>${
    (hit.snippets||[]).map(s=>`<div class="snip" title="${esc(s)}">${esc(s)}</div>`).join('')}</div>`:'';
  $('mbody').innerHTML=snips+(ent.list.length?msgHtml(ent):'<div class="empty">这个会话还没有可显示的消息</div>');
}
function msgHtml(ent){
  const rest=ent.total-ent.list.length;
  const more=rest>0?`<button class="morebtn" onclick="loadMore(this)">加载更早的消息（还有 ${rest} 条）</button>`:'';
  return `<div class="cap">已显示最近 ${ent.list.length} / ${ent.total} 条</div>`+more+ent.list.map(m=>
    `<div class="msg ${m.r}"><div class="who">${m.r==='user'?'你':'Claude'}</div><div class="bubble">${esc(m.t)}</div></div>`
  ).join('');
}
async function loadMore(btn){
  const id=curId, ent=detailsCache[id];
  if(!ent) return;
  btn.disabled=true; btn.textContent='加载中…';
  try{
    const r=await fetch(`/api/detail?id=${id}&offset=${ent.list.length}&limit=20`).then(r=>r.json());
    ent.list=(r.msgs||[]).concat(ent.list);
    ent.total=r.total??ent.total;
    if(curId===id) renderModal();
  }catch(e){
    btn.disabled=false; btn.textContent='加载失败，点击重试';
  }
}
async function restore(id,ev){
  ev.stopPropagation();
  const btn=ev.target.closest('button');
  if(btn) btn.disabled=true;
  try{
    const r=await fetch('/api/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})}).then(r=>r.json());
    showToast(r.ok?'已恢复原位，可以正常 resume 了':'恢复失败：'+(r.error||'未知错误'),r.ok?'':'err');
    if(r.ok){
      knownIds=null; // 让轮询重建 id 集，避免把恢复回来的会话当成“新会话”
      await switchView(VIEW==='arch'?'arch':'fav');
    }
  }catch(e){showToast('恢复失败：'+e,'err');}
  if(btn) btn.disabled=false;
}
async function delArchive(id,ev){
  ev.stopPropagation();
  if(!confirm('确定删除这份备份？原会话已被清理，删了就找不回了。')) return;
  const btn=ev.target.closest('button');
  if(btn) btn.disabled=true;
  try{
    const r=await fetch('/api/archive/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})}).then(r=>r.json());
    showToast(r.ok?'备份已删除':'删除失败：'+(r.error||'未知错误'),r.ok?'':'err');
    if(r.ok){
      ARCH=ARCH.filter(x=>x.id!==id);
      filtered=filtered.filter(x=>x.id!==id);
      $('archBadge').textContent=ARCH.length;
      renderPage();
    }
  }catch(e){showToast('删除失败：'+e,'err');}
  if(btn) btn.disabled=false;
}
function closeM(){curId=null;curD=null;$('mask').classList.remove('show');}

function copyCmd(id,ev){
  if(ev) ev.stopPropagation();
  const d=findD(id);
  if(!d) return;
  // 用分号分隔，兼容 PowerShell(旧版不支持 &&)；路径加引号防空格/中文
  const pathPart=d.path?`cd "${d.path}"; `:'';
  const cmd=`${pathPart}claude --dangerously-skip-permissions --model ${model} --resume ${id}`;
  navigator.clipboard.writeText(cmd)
    .then(()=>showToast('已复制 resume 命令'))
    .catch(()=>showToast('复制失败，请手动复制','err'));
}
async function openTerm(id,ev){
  if(ev) ev.stopPropagation();
  const btn=ev&&ev.target.closest('button');
  if(btn) btn.disabled=true;
  try{
    const r=await fetch('/api/open',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})}).then(r=>r.json());
    showToast(r.ok?'已在 Windows Terminal 中打开新标签页':('打开失败：'+(r.error||'未知错误')),r.ok?'':'err');
  }catch(e){showToast('打开失败：'+e,'err');}
  if(btn) btn.disabled=false;
}
function showToast(m,kind){
  const el=$('toast');
  el.textContent=m;
  el.classList.toggle('err',kind==='err');
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer=setTimeout(()=>el.classList.remove('show'),kind==='err'?3500:2000);
}

/* ---------- 设置：外观 + 模型 ---------- */
function applyTheme(t){document.documentElement.dataset.theme=t==='dark'?'dark':'';}
function applySettings(st){
  model=st.model||DEFAULT_MODEL;
  theme=themeSaved=st.theme==='dark'?'dark':'light';
  applyTheme(theme);
  $('setModel').textContent=model;
  $('setModel').title=`当前模型：${model}（点击修改）`;
  $('defaultModel').textContent=st.defaultModel||DEFAULT_MODEL;
}
async function openSettings(){
  try{
    const st=await fetch('/api/settings').then(r=>r.json());
    if(st.model) applySettings(st); // 以服务端为准，顺便校正本地状态
  }catch(e){}
  $('modelInput').value=(model===$('defaultModel').textContent)?'':model;
  $('setErr').classList.remove('show');
  paintThemeSeg();
  $('smask').classList.add('show');
  $('modelInput').focus();
}
function paintThemeSeg(){
  $('themeSeg').querySelectorAll('button').forEach(b=>b.classList.toggle('active',b.dataset.t===theme));
}
function closeSettings(){
  applyTheme(themeSaved); // 未保存的选择不生效
  $('smask').classList.remove('show');
}
$('themeSeg').addEventListener('click',e=>{
  const b=e.target.closest('button');
  if(!b) return;
  theme=b.dataset.t;applyTheme(theme);paintThemeSeg(); // 先预览，点「保存」才记住
});
$('smask').addEventListener('click',e=>{if(e.target.id==='smask')closeSettings();});
$('modelInput').addEventListener('input',()=>$('setErr').classList.remove('show'));
$('modelInput').addEventListener('keydown',e=>{if(e.key==='Enter')$('setSave').click();});
$('setSave').addEventListener('click',async()=>{
  const m=$('modelInput').value.trim();
  if(!MODEL_RE.test(m)){
    $('setErr').textContent='模型名只能包含字母、数字和 . _ : / - [ ]';
    $('setErr').classList.add('show');
    return;
  }
  const btn=$('setSave');btn.disabled=true;
  try{
    const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({model:m,theme})}).then(r=>r.json());
    if(!r.ok){
      $('setErr').textContent=r.error||'保存失败';
      $('setErr').classList.add('show');
      return;
    }
    applySettings(r);
    try{localStorage.setItem('theme',theme);}catch(e){}
    $('smask').classList.remove('show');
    showToast(`已保存，当前模型 ${model}`);
  }catch(e){
    $('setErr').textContent='保存失败：无法连接服务';
    $('setErr').classList.add('show');
  }finally{
    btn.disabled=false; // 出错时也要恢复，否则按钮永远点不了
  }
});

$('q').addEventListener('input',applyFilter);
$('proj').addEventListener('change',applyFilter);
$('pager').addEventListener('click',e=>{
  const b=e.target.closest('button');
  if(!b||b.disabled) return;
  if(b.id==='prev') page--;
  else if(b.id==='next') page++;
  else page=+b.dataset.p;
  renderPage();
});
$('mask').addEventListener('click',e=>{if(e.target.id==='mask')closeM();});
$('mCopy').addEventListener('click',()=>{if(curId)copyCmd(curId);});
$('mOpen').addEventListener('click',()=>{if(curId)openTerm(curId);});
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){
    if($('smask').classList.contains('show')) closeSettings(); else closeM();
    return;
  }
  const t=document.activeElement.tagName;
  if(e.key==='/' && t!=='INPUT' && t!=='TEXTAREA' && t!=='SELECT'){e.preventDefault();$('q').focus();}
});

(async function init(){
  let r;
  [r]=await Promise.all([
    fetch('/api/list').then(r=>r.json()).catch(()=>null),
    fetch('/api/settings').then(r=>r.json()).then(applySettings).catch(()=>{})
  ]);
  if(!r){
    $('body').innerHTML=`<div class="empty">${ICON.inbox}加载会话失败，请刷新页面重试</div>`;
    $('count').textContent='';
    return;
  }
  ALL=r.sessions||[];
  FAVS_COUNT=ALL.filter(s=>s.fav).length;
  $('archBadge').textContent=r.archCount??0;
  knownIds=new Set(ALL.map(s=>s.id));
  updateBadges();
  // 项目下拉
  const projs=[...new Set(ALL.map(d=>d.proj))].sort();
  const sel=$('proj');
  projs.forEach(p=>{const o=document.createElement('option');o.value=p;o.textContent=p;sel.appendChild(o);});
  applyFilter();
})();

/* ---------- 轮询发现新会话（30 秒一次；页面不可见或开着弹窗时跳过） ---------- */
async function poll(){
  if(document.visibilityState!=='visible') return;
  try{
    const r=await fetch('/api/list').then(x=>x.json());
    const list=r.sessions||[];
    FAVS_COUNT=list.filter(s=>s.fav).length;
    $('archBadge').textContent=r.archCount??0;
    if(!knownIds){ knownIds=new Set(list.map(s=>s.id)); ALL=list; updateBadges(); return; }
    const fresh=list.filter(s=>!knownIds.has(s.id));
    ALL=list;
    updateBadges();
    if(fresh.length && !$('q').value.trim() && !$('mask').classList.contains('show')){
      $('newbar').textContent=`发现 ${fresh.length} 个新会话，点击查看`;
      $('newbar').classList.add('show');
    }
  }catch(e){/* 服务暂时不可达就等下一轮 */}
}
$('newbar').addEventListener('click',async()=>{
  if(!ALL.length) return;
  knownIds=new Set(ALL.map(s=>s.id));
  $('newbar').classList.remove('show');
  if(VIEW!=='all') await switchView('all'); else applyFilter();
});
setInterval(poll,30000);
</script>
</body>
</html>"""


def build_index():
    """构建 session_id -> 解析结果 的索引。"""
    index = {}
    sessions = load_all_sessions()
    for s in sessions:
        index[s["id"]] = s
    return sessions, index


# 全局缓存：by_path 供 load_all_sessions 做 mtime 增量；
# SESSIONS_CACHE / INDEX_CACHE 为最近一次结果，供请求复用。
_CACHE = {"by_path": {}}
SESSIONS_CACHE = []
INDEX_CACHE = {}
_REFRESH_LOCK = threading.Lock()


def refresh():
    """刷新缓存。带 mtime 增量，未修改文件不重读，所以可安全每次调用。

    加锁：ThreadingHTTPServer 下多个请求并发刷新时不重复扫描、缓存不被写坏。
    """
    global SESSIONS_CACHE, INDEX_CACHE
    with _REFRESH_LOCK:
        SESSIONS_CACHE, INDEX_CACHE = build_index()
        sync_archive()  # 顺手备份收藏的会话（源有更新才真正复制）


def json_resp(handler, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def build_wt_args(path, cmd):
    """生成在最近使用的 Windows Terminal 窗口里新开标签页执行 cmd（参数列表）的命令行。

    -w last：追加到最后使用的那个 Windows Terminal 窗口；没有已开窗口时 wt 会新建一个。
    """
    args = ["wt.exe", "-w", "last", "new-tab", "--title", "claude"]
    if path and os.path.isdir(path):
        args += ["-d", path]  # 目录已不存在时不传，避免 wt 启动失败
    return args + list(cmd)


def _clean_child_env():
    """返回剔除 CLAUDE_CODE_* 内部变量后的环境副本。

    服务进程可能继承 Claude Code 的内部标记（如 CLAUDE_CODE_CHILD_SESSION=1，
    表示子代理会话、transcript 不落盘）。这些标记会被终端里的 claude 再次继承，
    导致 resume 出来的会话提示 "Transcript saving is off" 且不再保存记录，必须剔除。
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_CODE_")}


def open_in_terminal(sid):
    """在最近使用的 Windows Terminal 窗口里新开标签页 resume 指定会话。返回 (ok, 错误信息)。"""
    s = INDEX_CACHE.get(sid)  # 只认缓存里存在的会话，路径取自缓存而非请求
    if not s:
        return False, "会话不存在"
    # 与用户现有窗口一致：Windows Terminal 里跑 PowerShell 7，并加载用户 profile
    cmd = ["pwsh.exe", "-NoExit", "-Command",
           "claude", "--dangerously-skip-permissions", "--model",
           current_settings()["model"], "--resume", sid]
    try:
        r = subprocess.run(build_wt_args(s["path"], cmd),
                           capture_output=True, timeout=15,
                           env=_clean_child_env())
    except Exception as e:
        return False, f"启动终端失败：{e}"
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()
        return False, err or f"wt.exe 退出码 {r.returncode}"
    return True, ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # 静默日志

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            refresh()  # 每次打开页面刷新一次数据
            page = PAGE_HTML.replace("__MODEL__", MODEL).replace(
                "__PAGESIZE__", str(DEFAULT_PAGE_SIZE)
            )
            data = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        elif path == "/api/list":
            refresh()
            favs = load_favorites()  # {id: 备注}
            items = [
                {
                    "id": s["id"],
                    "name": s["name"],
                    "proj": s["proj"],
                    "path": s["path"],
                    "preview": s["preview"],
                    "msgs": s["msgs"],
                    "time": s["time"],
                    "fav": s["id"] in favs,
                    "favNote": favs.get(s["id"], ""),
                }
                for s in SESSIONS_CACHE
            ]
            meta = load_archive_meta()
            arch_count = sum(1 for sid in meta if _archive_file(sid))
            json_resp(self, {"sessions": items, "favs": favs, "archCount": arch_count})

        elif path == "/api/detail":
            # 参数：offset=已显示条数（从末尾算），limit=本次条数。
            # 默认返回最近 5 条；带 offset 时返回更早的一段（供弹窗「加载更多」）。
            sid = qs.get("id", [""])[0]
            try:
                offset = max(0, int(qs.get("offset", ["0"])[0]))
                limit = max(1, min(100, int(qs.get("limit", ["0"])[0] or DETAIL_FIRST)))
            except ValueError:
                offset, limit = 0, DETAIL_FIRST
            msgs = full_messages(sid)
            if msgs is None:
                json_resp(self, {"msgs": [], "total": 0, "has_more": False})
                return
            end = max(0, len(msgs) - offset)
            start = max(0, end - limit)
            json_resp(self, {"msgs": msgs[start:end], "total": len(msgs),
                             "has_more": start > 0})

        elif path == "/api/favs":
            # 返回收藏列表（合并会话信息 + 备注），按收藏的会话时间倒序。
            # 原会话已被 30 天清理删掉的，用归档副本顶上并标记 archived。
            refresh()
            favs = load_favorites()
            idx = INDEX_CACHE
            meta = load_archive_meta()
            items = []
            for sid, note in favs.items():
                s = idx.get(sid)
                if s:
                    items.append({
                        "id": s["id"], "name": s["name"], "proj": s["proj"],
                        "path": s["path"], "preview": s["preview"], "msgs": s["msgs"],
                        "time": s["time"], "fav": True, "favNote": note,
                        "archived": False,
                    })
                elif sid in meta and _archive_file(sid):
                    m = meta[sid]
                    items.append({
                        "id": sid, "name": m.get("name", "(未知会话)"),
                        "proj": m.get("proj", ""), "path": m.get("path", ""),
                        "preview": "", "msgs": m.get("msgs", 0),
                        "time": m.get("time", ""), "fav": True, "favNote": note,
                        "archived": True,
                    })
            items.sort(key=lambda x: x["time"], reverse=True)
            json_resp(self, {"sessions": items})

        elif path == "/api/search":
            # 全文搜索所有会话的 jsonl；q 至少 SEARCH_MIN 个字符
            refresh()
            q = qs.get("q", [""])[0].strip().lower()
            if len(q) < SEARCH_MIN:
                json_resp(self, {"results": [], "partial": False})
                return
            results, partial = search_all(q)
            json_resp(self, {"results": results, "partial": partial})

        elif path == "/api/archives":
            # 归档视图：列出 archive/ 里所有备份（无论是否还在收藏）。
            # 原会话仍存活的用实时数据并标 live，已清理的用归档时的快照。
            refresh()
            favs = load_favorites()
            meta = load_archive_meta()
            idx = INDEX_CACHE
            items = []
            for sid, m in meta.items():
                if not _archive_file(sid):
                    continue
                s = idx.get(sid) or {}
                items.append({
                    "id": sid,
                    "name": s.get("name") or m.get("name") or "(未知会话)",
                    "proj": s.get("proj", m.get("proj", "")),
                    "path": s.get("path", m.get("path", "")),
                    "preview": s.get("preview", ""),
                    "msgs": s.get("msgs", m.get("msgs", 0)),
                    "time": s.get("time", m.get("time", "")),
                    "fav": sid in favs,
                    "favNote": favs.get(sid, ""),
                    "archived": True,
                    "live": sid in idx,
                })
            items.sort(key=lambda x: x["time"], reverse=True)
            json_resp(self, {"sessions": items})

        elif path == "/api/settings":
            json_resp(self, current_settings())

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            body = {}

        if path == "/api/fav":
            # body: {id, fav(bool), note(str 可选)}
            sid = str(body.get("id", ""))
            favs = load_favorites()
            if body.get("fav"):
                favs[sid] = str(body.get("note", "") or "")
            else:
                favs.pop(sid, None)
            save_favorites(favs)
            refresh()  # 刷新索引并立即归档新收藏（不必等下次请求）
            json_resp(self, {"ok": True, "fav": sid in favs})

        elif path == "/api/restore":
            # 把归档副本写回 ~/.claude/projects 原位置，让被清理的会话可再次 resume
            ok, err = restore_from_archive(str(body.get("id", "")))
            json_resp(self, {"ok": ok, "error": err})

        elif path == "/api/open":
            ok, err = open_in_terminal(str(body.get("id", "")))
            json_resp(self, {"ok": ok, "error": err})

        elif path == "/api/settings":
            # body: {model(str，可空=用默认), theme('light'|'dark')}
            m = str(body.get("model", "") or "").strip()
            t = str(body.get("theme", "") or "").strip()
            if m and not _MODEL_RE.match(m):
                json_resp(self, {"ok": False, "error": "模型名只能包含字母、数字和 . _ : / - [ ]"})
            elif t not in ("light", "dark"):
                json_resp(self, {"ok": False, "error": "请选择白天模式或夜晚模式"})
            else:
                try:
                    save_settings({"model": m, "theme": t})
                except OSError:
                    json_resp(self, {"ok": False, "error": "写入 settings.json 失败"})
                else:
                    json_resp(self, {"ok": True, **current_settings()})

        elif path == "/api/archive/delete":
            # 删除一份归档备份。仍存活的收藏会在下次刷新时自动重新备份，
            # 所以这实际只对“原会话已被清理”的备份有意义（删了就找不回了）。
            sid = str(body.get("id", ""))
            if not _SID_RE.match(sid):
                json_resp(self, {"ok": False, "error": "会话 id 不合法"})
                return
            p = _archive_file(sid)
            if not p:
                json_resp(self, {"ok": False, "error": "备份不存在"})
                return
            try:
                os.remove(p)
            except OSError as e:
                json_resp(self, {"ok": False, "error": f"删除失败：{e}"})
                return
            meta = load_archive_meta()
            meta.pop(sid, None)
            try:
                save_archive_meta(meta)
            except OSError:
                pass
            json_resp(self, {"ok": True})

        else:
            self.send_response(404)
            self.end_headers()


def _is_running():
    """探测端口是否已有实例在监听（单例检测）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect((HOST, PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ---------- 开机自启（注册表 Run 键 + 健壮 VBS）----------
# 注：本机任务计划程序被禁用（拒绝访问）；启动文件夹方式实测开机不执行。
# 改用注册表 HKCU\...\Run 键 + wscript 调用 VBS（与 PM2 自启同款可靠方式）。
# VBS 文件放在 app.py 同目录（不放启动文件夹），由 Run 键触发 wscript 执行。
VBS_NAME = "claude_message_view_startup.vbs"
VBS_PATH = os.path.join(HERE, VBS_NAME)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ClaudeMessageView"


def find_pythonw():
    """返回 pythonw.exe 完整路径（写死到 VBS，避免开机时 PATH 不一致）。"""
    exe = sys.executable  # 当前 python.exe
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(cand):
        return cand
    return exe  # 回退：用当前解释器（会有控制台窗口，但能跑）


# 健壮的 VBS 启动器：
# 1. 启动前先探测 8765 端口，若服务已在跑则跳过（避免重复启动/端口冲突）
# 2. 启动后再探测，若没起来则重试（最多 3 次，每次间隔 3 秒）
# 用 Chr(34) 生成双引号，避免 VBS 连续引号导致 Python 字符串提前闭合
VBS_TEMPLATE = (
    "' Claude Message View - startup launcher (auto-generated, do not edit)\n"
    "Option Explicit\n"
    "Dim sh, fso, url, i, logPath, logFile\n"
    "Set sh = CreateObject(\"WScript.Shell\")\n"
    "Set fso = CreateObject(\"Scripting.FileSystemObject\")\n"
    "url = \"http://127.0.0.1:8765/\"\n"
    "logPath = \"{log}\"\n"
    "On Error Resume Next\n"
    "Set logFile = fso.OpenTextFile(logPath, 8, True)\n"
    "logFile.WriteLine Now() & \" [START] vbs begin\"\n"
    "On Error GoTo 0\n"
    "' skip if already running\n"
    "If isUp() Then\n"
    "  logFile.WriteLine Now() & \" [SKIP] already running\"\n"
    "  logFile.Close\n"
    "  WScript.Quit 0\n"
    "End If\n"
    "' launch service (no window)\n"
    "sh.Run Chr(34) & \"{pythonw}\" & Chr(34) & \" \" & Chr(34) & \"{app_py}\" & Chr(34), 0, False\n"
    "logFile.WriteLine Now() & \" [START] pythonw invoked\"\n"
    "' poll and retry\n"
    "For i = 1 To 6\n"
    "  WScript.Sleep 3000\n"
    "  If isUp() Then\n"
    "    logFile.WriteLine Now() & \" [OK] service up, opening chrome\"\n"
    "    logFile.Close\n"
    "    sh.Run Chr(34) & \"{chrome}\" & Chr(34) & \" \" & url, 3, False\n"
    "    WScript.Quit 0\n"
    "  End If\n"
    "Next\n"
    "logFile.WriteLine Now() & \" [FAIL] service not up after retries\"\n"
    "logFile.Close\n"
    "' ===== functions (must be after main flow) =====\n"
    "Function isUp()\n"
    "  Dim xhr\n"
    "  On Error Resume Next\n"
    "  Set xhr = CreateObject(\"MSXML2.XMLHTTP\")\n"
    "  xhr.Open \"GET\", url, False\n"
    "  xhr.send\n"
    "  isUp = (Err.Number = 0 And xhr.Status = 200)\n"
    "  On Error GoTo 0\n"
    "End Function\n"
)


def install_startup():
    """注册开机自启：写注册表 Run 键 + 生成健壮 VBS（由 wscript 执行）。"""
    import winreg

    pythonw = find_pythonw()
    chrome = find_chrome() or ""
    log_path = os.path.join(HERE, "startup.log")
    content = VBS_TEMPLATE.format(
        pythonw=pythonw,
        app_py=os.path.join(HERE, "app.py"),
        chrome=chrome,
        log=log_path,
    )
    # VBS 用 ASCII/ANSI 写入（纯英文，避免 wscript 用 GBK 读取时中文乱码导致语法错误）
    with open(VBS_PATH, "w", encoding="ascii") as f:
        f.write(content)

    # 写注册表 Run 键：wscript.exe "VBS路径"（与 PM2 自启同款方式）
    run_value = f'wscript.exe "{VBS_PATH}"'
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, run_value)

    # 清理旧的启动文件夹 VBS（迁移）
    startup_dir = os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
    )
    old_vbs = os.path.join(startup_dir, "claude_message_view.vbs")
    if os.path.isfile(old_vbs):
        try:
            os.remove(old_vbs)
        except OSError:
            pass

    print("已配置开机自启 ✓（注册表 Run 键）")
    print(f"  Python：{pythonw}")
    print(f"  VBS：{VBS_PATH}")
    print(f"  注册表：HKCU\\{RUN_KEY} → {RUN_VALUE}")
    print(f"  浏览器：{chrome or '默认浏览器（未找到 Chrome）'}")
    print(f"  策略：登录时启动，端口探测 + 失败重试")
    print("下次开机/登录将自动启动服务并用 Chrome 打开会话列表。")


def uninstall_startup():
    """取消开机自启：删除注册表 Run 键 + VBS 文件。"""
    import winreg
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, RUN_VALUE)
        print(f"已取消开机自启，删除注册表项：{RUN_VALUE}")
    except FileNotFoundError:
        print("注册表中未找到自启项（可能从未安装或已卸载）")
    except OSError as e:
        print(f"删除注册表项失败：{e}", file=sys.stderr)

    if os.path.isfile(VBS_PATH):
        try:
            os.remove(VBS_PATH)
            print(f"已删除 VBS：{VBS_PATH}")
        except OSError:
            pass


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Claude 会话管理服务")
    parser.add_argument("--install", action="store_true", help="注册开机自启")
    parser.add_argument("--uninstall", action="store_true", help="取消开机自启")
    args = parser.parse_args()

    if args.uninstall:
        uninstall_startup()
        return
    if args.install:
        install_startup()
        return

    if not os.path.isdir(PROJECTS_DIR):
        print(f"找不到会话目录：{PROJECTS_DIR}", file=sys.stderr)
        sys.exit(1)

    # 单例检测：若端口已有实例在跑，则不重复启动（避免端口冲突）
    if _is_running():
        print(f"服务已在运行（{HOST}:{PORT}），本次启动跳过。")
        return

    refresh()
    url = f"http://{HOST}:{PORT}"
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Claude 会话管理服务已启动")
    print(f"访问：{url}")
    print(f"数据目录：{PROJECTS_DIR}")
    chrome = find_chrome()
    print(f"浏览器：{chrome or '默认浏览器（未找到 Chrome）'}")
    print("按 Ctrl+C 停止")
    # 注：浏览器统一由 VBS 启动器打开（服务就绪后），app.py 自身不再打开，
    # 避免两条路径各开一次导致出现两个标签页。
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
