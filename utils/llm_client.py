#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一 LLM 客户端 - 支持 OpenAI 兼容接口与 Google Gemini
"""

import os
import json
import requests
from typing import List, Dict, Any, Optional, Iterator

# 默认配置路径（用例管理模块）
DEFAULT_LLM_CONFIG = os.path.join(
    os.path.dirname(__file__), '..', 'modules', 'test_case', 'llm_config.json'
)

import threading
import time

_LLM_METRICS_LOCK = threading.Lock()


def _default_llm_metrics_path() -> str:
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "modules", "prd_audit", "learning_repo"))
    return os.path.join(base, "llm_metrics.jsonl")


def _resolve_llm_metrics_path() -> str:
    """
    仅在显式开启或 prd_audit learning_repo 已存在时记录指标，避免默认在所有环境落盘。
    - 优先使用环境变量 LLM_METRICS_PATH
    - 否则如果 prd_audit learning_repo 存在则使用默认路径
    - 否则返回空字符串表示不记录
    """
    p = (os.environ.get("LLM_METRICS_PATH") or "").strip()
    if p:
        return p
    default_p = _default_llm_metrics_path()
    parent = os.path.dirname(default_p)
    if os.path.exists(parent):
        return default_p
    return ""


def _safe_write_llm_metric(event: Dict[str, Any]) -> None:
    path = _resolve_llm_metrics_path()
    if not path:
        return
    try:
        parent = os.path.dirname(path) or "."
        os.makedirs(parent, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with _LLM_METRICS_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        return


def _messages_stats(messages: List[Dict[str, str]]) -> Dict[str, Any]:
    msg_count = 0
    total_chars = 0
    roles = {}
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        msg_count += 1
        role = (m.get("role") or "user").lower()
        roles[role] = int(roles.get(role, 0)) + 1
        content = m.get("content") or ""
        total_chars += len(content)
    return {"message_count": msg_count, "input_chars": total_chars, "roles": roles}


def _extract_first_json_object(text: str) -> dict:
    """从文本中抽取第一个完整 JSON 对象，避免配置文件多段/含注释导致解析失败。"""
    raw = (text or "").strip()
    if not raw:
        return {}
    start = raw.find("{")
    if start == -1:
        return {}
    depth = 0
    in_string = None
    escape = False
    for i in range(start, len(raw)):
        c = raw[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if in_string:
            if c == in_string:
                in_string = None
            continue
        if c in ('"', "'"):
            in_string = c
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start : i + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


def _merge_active_profile(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 profiles[default_profile] 的非空字段合并到顶层。
    与各模块 GET /api/llm_config 的展平规则一致，避免「文件里有 key、call_llm 读顶层却为空」。
    """
    if not isinstance(config, dict):
        return config
    profiles = config.get('profiles')
    if not isinstance(profiles, dict):
        return config
    default = (config.get('default_profile') or config.get('llm_provider') or 'deepseek').strip()
    active = profiles.get(default)
    if not isinstance(active, dict):
        return config
    merged = dict(config)
    for k in ('llm_provider', 'base_url', 'model'):
        v = active.get(k)
        if v is not None and str(v).strip() != '':
            merged[k] = v
    ak = (active.get('api_key') or '').strip()
    if ak:
        merged['api_key'] = ak
    return merged


def load_llm_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """加载 LLM 配置；若标准 json.load 失败则尝试抽取首段 JSON（兼容损坏/多段文件）。"""
    path = config_path or os.environ.get('LLM_CONFIG_PATH') or DEFAULT_LLM_CONFIG
    if not os.path.exists(path):
        raise FileNotFoundError(f'LLM 配置不存在: {path}')
    with open(path, 'r', encoding='utf-8') as f:
        raw = f.read()
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        obj = {}
    if not obj:
        obj = _extract_first_json_object(raw)
    if not obj:
        raise ValueError(f'LLM 配置文件不是合法 JSON: {path}')
    if not isinstance(obj, dict):
        raise ValueError(f'LLM 配置须为 JSON 对象: {path}')
    return _merge_active_profile(obj)


def _resolve_api_key(config: Dict[str, Any]) -> str:
    """
    安全优先：环境变量优先于配置文件，避免明文 key 落盘。
    支持：
    - LLM_API_KEY：通用
    - DEEPSEEK_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY：按 provider 细分
    - VOLCENGINE_API_KEY / ARK_API_KEY：火山引擎（方舟）API Key
    """
    provider = (config.get('llm_provider') or 'deepseek').lower()
    env_any = (os.environ.get('LLM_API_KEY') or '').strip()
    if env_any:
        return env_any
    if provider == 'gemini':
        env = (os.environ.get('GEMINI_API_KEY') or '').strip()
        if env:
            return env
    if provider in ('volcengine', 'ark', 'doubao'):
        env = (os.environ.get('VOLCENGINE_API_KEY') or '').strip()
        if env:
            return env
        env = (os.environ.get('ARK_API_KEY') or '').strip()
        if env:
            return env
    if provider in ('deepseek', 'openai'):
        env = (os.environ.get('DEEPSEEK_API_KEY') or '').strip()
        if env:
            return env
        env = (os.environ.get('OPENAI_API_KEY') or '').strip()
        if env:
            return env
    return (config.get('api_key') or '').strip()


def _default_base_url(provider: str) -> str:
    p = (provider or '').lower()
    if p in ('volcengine', 'ark', 'doubao'):
        return 'https://ark.cn-beijing.volces.com/api/v3'
    return 'https://api.deepseek.com/v1'


def _default_model(provider: str) -> str:
    p = (provider or '').lower()
    if p in ('volcengine', 'ark', 'doubao'):
        return 'doubao-pro-32k'
    return 'deepseek-chat'


def call_llm_with_retry(
    messages: List[Dict[str, str]],
    config_path: Optional[str] = None,
    config_override: Optional[Dict[str, Any]] = None,
    stream: bool = False,
    timeout: int = 60,
    max_tokens: Optional[int] = None,
    max_retries: int = 2,
    retry_delay: int = 2
) -> str:
    """
    带重试机制的 LLM 调用。解决网络波动、频率限制等暂时性问题。
    """
    import time
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            return call_llm(
                messages, 
                config_path=config_path, 
                config_override=config_override, 
                stream=stream, 
                timeout=timeout, 
                max_tokens=max_tokens
            )
        except Exception as e:
            last_err = e
            # 如果是 401/403 等鉴权错误，没必要重试
            err_str = str(e).lower()
            if "401" in err_str or "403" in err_str or "api key" in err_str:
                break
            
            if attempt < max_retries:
                # 指数退避：每次重试等待时间加倍
                wait_time = retry_delay * (2 ** attempt)
                import logging
                logging.getLogger(__name__).warning(f"LLM 调用失败 (尝试 {attempt+1}/{max_retries+1}): {e}。{wait_time}秒后重试...")
                time.sleep(wait_time)
            else:
                break
    
    # 所有重试都失败了
    raise last_err or Exception("LLM 调用失败且无具体错误信息")


def call_llm(
    messages: List[Dict[str, str]],
    config_path: Optional[str] = None,
    config_override: Optional[Dict[str, Any]] = None,
    stream: bool = False,
    timeout: int = 60,
    max_tokens: Optional[int] = None
) -> str:
    """
    调用 LLM，返回完整文本响应（非流式）或首个 chunk 后的完整文本（流式内部聚合）
    非流式时返回完整文本；流式时仍返回完整文本（内部聚合）。
    max_tokens: 可选，最大生成 token 数；不传则用 API 默认（长报告建议传 8192 或 16384 避免截断）。
    """
    if config_override:
        config = config_override
    else:
        config = load_llm_config(config_path)
    provider = (config.get('llm_provider') or 'deepseek').lower()
    api_key = _resolve_api_key(config)
    base_url = (config.get('base_url') or _default_base_url(provider)).rstrip('/')
    model = (config.get('model') or '').strip() or _default_model(provider)

    if not api_key:
        raise ValueError('API Key 未配置')

    if provider == 'gemini':
        return _call_gemini(messages, api_key, model, stream, timeout, max_tokens=max_tokens)
    else:
        return _call_openai_compatible(messages, api_key, base_url, model, stream, timeout, max_tokens=max_tokens)


def _call_gemini(
    messages: List[Dict[str, str]],
    api_key: str,
    model: str,
    stream: bool,
    timeout: int,
    max_tokens: Optional[int] = None
) -> str:
    """调用 Google Gemini API"""
    start = time.time()
    stats = _messages_stats(messages)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    if stream:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse"

    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json"
    }

    # 转换 messages 为 Gemini 格式
    system_text = None
    contents = []
    for m in messages:
        role = (m.get('role') or 'user').lower()
        content = (m.get('content') or '').strip()
        if not content:
            continue
        if role == 'system':
            system_text = content
        else:
            gemini_role = 'user' if role == 'user' else 'model'
            contents.append({"role": gemini_role, "parts": [{"text": content}]})

    if not contents:
        raise ValueError('无有效消息内容')

    # Gemini: system_instruction 单独传，contents 为对话
    payload = {"contents": contents}
    if system_text:
        payload["system_instruction"] = {"parts": [{"text": system_text}]}
    if max_tokens is not None:
        payload.setdefault("generationConfig", {})["maxOutputTokens"] = max_tokens

    session = requests.Session()
    session.trust_env = False

    # requests 的 timeout 若为单值，会同时作为 connect/read timeout。
    # LLM 推理常见是“连接快、首包慢/长输出慢”，因此这里将其拆分为 (connect, read)。
    # connect 固定 10s，read 由 timeout 参数控制（默认 60s，业务侧可传更大）。
    req_timeout = (10, int(timeout or 60))

    if stream:
        payload["contents"] = contents  # stream 不需要 role 在首条
        ok = False
        status = None
        out_text = ""
        err = None
        try:
            r = session.post(url, json=payload, headers=headers, stream=True, timeout=req_timeout)
            status = r.status_code
            r.raise_for_status()
            full_text = []
            for line in r.iter_lines():
                if line:
                    line_str = line.decode('utf-8', errors='ignore')
                    if line_str.startswith('data: '):
                        data_str = line_str[6:].strip()
                        if data_str and data_str != '[DONE]':
                            try:
                                data = json.loads(data_str)
                                cands = data.get('candidates', [])
                                if cands:
                                    parts = cands[0].get('content', {}).get('parts', [])
                                    if parts:
                                        full_text.append(parts[0].get('text', ''))
                            except json.JSONDecodeError:
                                pass
            out_text = ''.join(full_text)
            ok = True
            return out_text
        except Exception as e:
            err = str(e)
            raise
        finally:
            _safe_write_llm_metric({
                "ts": int(time.time()),
                "provider": "gemini",
                "model": model,
                "stream": True,
                "timeout": timeout,
                "max_tokens": max_tokens,
                "http_status": status,
                "ok": ok,
                "duration_ms": int((time.time() - start) * 1000),
                "output_chars": len(out_text or ""),
                "error": err,
                **stats,
            })

    ok = False
    status = None
    out_text = ""
    usage = None
    err = None
    try:
        r = session.post(url, json=payload, headers=headers, timeout=req_timeout)
        status = r.status_code
        r.raise_for_status()
        data = r.json()
        usage = data.get("usageMetadata") if isinstance(data, dict) else None
        cands = data.get('candidates', [])
        if not cands:
            raise ValueError('Gemini 返回无有效内容')
        parts = cands[0].get('content', {}).get('parts', [])
        if not parts:
            raise ValueError('Gemini 返回无文本')
        out_text = (parts[0].get('text') or '').strip()
        ok = True
        return out_text
    except Exception as e:
        err = str(e)
        raise
    finally:
        event = {
            "ts": int(time.time()),
            "provider": "gemini",
            "model": model,
            "stream": False,
            "timeout": timeout,
            "max_tokens": max_tokens,
            "http_status": status,
            "ok": ok,
            "duration_ms": int((time.time() - start) * 1000),
            "output_chars": len(out_text or ""),
            "error": err,
            **stats,
        }
        if isinstance(usage, dict):
            event["usage"] = usage
        _safe_write_llm_metric(event)


def _call_openai_compatible(
    messages: List[Dict[str, str]],
    api_key: str,
    base_url: str,
    model: str,
    stream: bool,
    timeout: int,
    max_tokens: Optional[int] = None
) -> str:
    """调用 OpenAI 兼容接口（DeepSeek、OpenAI 等）"""
    start = time.time()
    stats = _messages_stats(messages)
    url = base_url if 'chat/completions' in base_url else f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    session = requests.Session()
    session.trust_env = False

    # requests 的 timeout 若为单值，会同时作为 connect/read timeout。
    # LLM 推理常见是“连接快、首包慢/长输出慢”，因此这里将其拆分为 (connect, read)。
    req_timeout = (10, int(timeout or 60))

    if stream:
        ok = False
        status = None
        out_text = ""
        err = None
        try:
            r = session.post(url, json=payload, headers=headers, stream=True, timeout=req_timeout)
            status = r.status_code
            r.raise_for_status()
            full_text = []
            for line in r.iter_lines():
                if line:
                    line_str = line.decode('utf-8', errors='ignore')
                    if line_str.startswith('data: '):
                        data_str = line_str[6:].strip()
                        if data_str == '[DONE]':
                            break
                        try:
                            data = json.loads(data_str)
                            delta = data.get('choices', [{}])[0].get('delta', {})
                            content = delta.get('content', '')
                            if content:
                                full_text.append(content)
                        except json.JSONDecodeError:
                            pass
            out_text = ''.join(full_text)
            ok = True
            return out_text
        except Exception as e:
            err = str(e)
            raise
        finally:
            _safe_write_llm_metric({
                "ts": int(time.time()),
                "provider": "openai_compatible",
                "base_url": base_url,
                "model": model,
                "stream": True,
                "timeout": timeout,
                "max_tokens": max_tokens,
                "http_status": status,
                "ok": ok,
                "duration_ms": int((time.time() - start) * 1000),
                "output_chars": len(out_text or ""),
                "error": err,
                **stats,
            })

    ok = False
    status = None
    out_text = ""
    usage = None
    err = None
    try:
        r = session.post(url, json=payload, headers=headers, timeout=req_timeout)
        status = r.status_code
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage") if isinstance(data, dict) else None
        choice = data.get('choices', [{}])[0]
        out_text = (choice.get('message') or {}).get('content', '').strip()
        ok = True
        return out_text
    except Exception as e:
        err = str(e)
        raise
    finally:
        event = {
            "ts": int(time.time()),
            "provider": "openai_compatible",
            "base_url": base_url,
            "model": model,
            "stream": False,
            "timeout": timeout,
            "max_tokens": max_tokens,
            "http_status": status,
            "ok": ok,
            "duration_ms": int((time.time() - start) * 1000),
            "output_chars": len(out_text or ""),
            "error": err,
            **stats,
        }
        if isinstance(usage, dict):
            event["usage"] = usage
        _safe_write_llm_metric(event)


def stream_llm(
    messages: List[Dict[str, str]],
    config_path: Optional[str] = None,
    timeout: int = 60,
    config_override: Optional[Dict[str, Any]] = None
) -> Iterator[Dict[str, str]]:
    """流式调用 LLM，产出 {"type":"thinking"|"content", "text":"..."}
    config_override: 可选，直接使用此配置替代从文件加载（用于 fallback）"""
    if config_override:
        config = config_override
    else:
        config = load_llm_config(config_path)
    provider = (config.get('llm_provider') or 'deepseek').lower()
    api_key = _resolve_api_key(config)
    base_url = (config.get('base_url') or _default_base_url(provider)).rstrip('/')
    model = (config.get('model') or '').strip() or _default_model(provider)

    if not api_key:
        raise ValueError('API Key 未配置')

    if provider == 'gemini':
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse"
        headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
        system_text = None
        contents = []
        for m in messages:
            role = (m.get('role') or 'user').lower()
            content = (m.get('content') or '').strip()
            if not content:
                continue
            if role == 'system':
                system_text = content
            else:
                gemini_role = 'user' if role == 'user' else 'model'
                contents.append({"role": gemini_role, "parts": [{"text": content}]})
        payload = {"contents": contents}
        if system_text:
            payload["system_instruction"] = {"parts": [{"text": system_text}]}

        session = requests.Session()
        session.trust_env = False
        req_timeout = (10, int(timeout or 60))
        r = session.post(url, json=payload, headers=headers, stream=True, timeout=req_timeout)
        r.raise_for_status()
        for line in r.iter_lines():
            if line:
                line_str = line.decode('utf-8', errors='ignore')
                if line_str.startswith('data: '):
                    data_str = line_str[6:].strip()
                    if data_str and data_str != '[DONE]':
                        try:
                            data = json.loads(data_str)
                            cands = data.get('candidates', [])
                            if cands:
                                parts = cands[0].get('content', {}).get('parts', [])
                                if parts:
                                    t = parts[0].get('text', '')
                                    if t:
                                        yield {"type": "content", "text": t}
                        except json.JSONDecodeError:
                            pass
    else:
        url = base_url if 'chat/completions' in base_url else f"{base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": messages, "stream": True}
        session = requests.Session()
        session.trust_env = False
        req_timeout = (10, int(timeout or 60))
        r = session.post(url, json=payload, headers=headers, stream=True, timeout=req_timeout)
        r.raise_for_status()
        for line in r.iter_lines():
            if line:
                line_str = line.decode('utf-8', errors='ignore')
                if line_str.startswith('data: '):
                    data_str = line_str[6:].strip()
                    if data_str == '[DONE]':
                        break
                    try:
                        data = json.loads(data_str)
                        delta = data.get('choices', [{}])[0].get('delta', {})
                        reasoning = delta.get('reasoning_content', '') or delta.get('reasoning', '')
                        content = delta.get('content', '')
                        if reasoning:
                            yield {"type": "thinking", "text": reasoning}
                        if content:
                            yield {"type": "content", "text": content}
                    except json.JSONDecodeError:
                        pass
