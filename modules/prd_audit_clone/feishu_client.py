# -*- coding: utf-8 -*-
"""
飞书文档内容拉取：根据飞书文档链接获取正文纯文本，用于 PRD 漏洞分析等。
需配置 FEISHU_APP_ID、FEISHU_APP_SECRET（环境变量或 feishu_config.json）。
"""

import os
import re
import json
import time
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

FEISHU_DOCX_PATTERN = re.compile(
    r'https?://[^/]+\.feishu\.cn/docx/([A-Za-z0-9]+)',
    re.IGNORECASE
)
FEISHU_DOC_PATTERN = re.compile(
    r'https?://[^/]+\.feishu\.cn/docs/([A-Za-z0-9]+)',
    re.IGNORECASE
)
FEISHU_WIKI_PATTERN = re.compile(
    r'https?://[^/]+\.(?:feishu|larksuite)\.cn/wiki/([A-Za-z0-9]+)',
    re.IGNORECASE
)

_token_cache = {}
_token_cache_ttl = 0
TOKEN_CACHE_SECONDS = 7000


def _load_feishu_config() -> Tuple[Optional[str], Optional[str]]:
    app_id = os.environ.get('FEISHU_APP_ID', '').strip()
    app_secret = os.environ.get('FEISHU_APP_SECRET', '').strip()
    if app_id and app_secret:
        return app_id, app_secret
    try:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'feishu_config.json')
        if os.path.isfile(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
            app_id = (cfg.get('app_id') or cfg.get('FEISHU_APP_ID') or '').strip()
            app_secret = (cfg.get('app_secret') or cfg.get('FEISHU_APP_SECRET') or '').strip()
            return app_id or None, app_secret or None
    except Exception as e:
        logger.debug("Feishu config load failed: %s", e)
    return None, None


def _get_tenant_access_token(app_id: str, app_secret: str) -> Optional[str]:
    global _token_cache, _token_cache_ttl
    now = time.time()
    if _token_cache.get('token') and now < _token_cache_ttl:
        return _token_cache['token']
    try:
        import urllib.request
        req = urllib.request.Request(
            'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
            data=json.dumps({'app_id': app_id, 'app_secret': app_secret}).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        if data.get('code') != 0:
            logger.warning("Feishu token response code: %s", data.get('code'))
            return None
        token = data.get('tenant_access_token')
        if token:
            _token_cache['token'] = token
            _token_cache_ttl = now + TOKEN_CACHE_SECONDS
        return token
    except Exception as e:
        logger.warning("Feishu get token failed: %s", e)
        return None


def _fetch_docx_raw_content(document_id: str, token: str) -> Optional[str]:
    try:
        import urllib.request
        url = f'https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/raw_content'
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'}, method='GET')
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        if data.get('code') != 0:
            logger.warning("Feishu docx raw_content code: %s msg: %s", data.get('code'), data.get('msg'))
            return None
        return (data.get('data') or {}).get('content') or None
    except Exception as e:
        logger.warning("Feishu docx raw_content request failed: %s", e)
        return None


def _fetch_doc_raw_content(doc_token: str, token: str) -> Optional[str]:
    try:
        import urllib.request
        url = f'https://open.feishu.cn/open-apis/doc/v2/{doc_token}/raw_content'
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'}, method='GET')
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        if data.get('code') != 0:
            logger.warning("Feishu doc raw_content code: %s msg: %s", data.get('code'), data.get('msg'))
            return None
        return (data.get('data') or {}).get('content') or None
    except Exception as e:
        logger.warning("Feishu doc raw_content request failed: %s", e)
        return None


def _fetch_doc_markdown_content(doc_token: str, token: str) -> Optional[str]:
    try:
        import urllib.parse
        import urllib.request
        params = urllib.parse.urlencode({
            "doc_token": doc_token,
            "doc_type": "docx",
            "content_type": "markdown",
            "lang": "zh",
        })
        url = f'https://open.feishu.cn/open-apis/docs/v1/content?{params}'
        req = urllib.request.Request(
            url,
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json; charset=utf-8',
            },
            method='GET',
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        if data.get('code') != 0:
            logger.warning("Feishu docs markdown code: %s msg: %s", data.get('code'), data.get('msg'))
            return None
        return (data.get('data') or {}).get('content') or None
    except Exception as e:
        logger.warning("Feishu docs markdown request failed: %s", e)
        return None


def _find_obj_token_and_type(payload: Any) -> Optional[Tuple[str, str]]:
    stack = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            obj_token = cur.get("obj_token")
            obj_type = cur.get("obj_type")
            if isinstance(obj_token, str) and obj_token and isinstance(obj_type, str) and obj_type:
                return obj_token, obj_type
            for v in cur.values():
                stack.append(v)
        elif isinstance(cur, list):
            for v in cur:
                stack.append(v)
    return None


def _resolve_wiki_node(wiki_token: str, token: str) -> Optional[Tuple[str, str]]:
    try:
        import urllib.parse
        import urllib.request
        query = urllib.parse.urlencode({"token": wiki_token})
        candidates = [
            f'https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node?{query}',
            f'https://open.feishu.cn/open-apis/wiki/v2/nodes/{wiki_token}',
        ]
        for url in candidates:
            try:
                req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'}, method='GET')
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                if data.get('code') != 0:
                    continue
                parsed = _find_obj_token_and_type(data.get('data') or {})
                if parsed:
                    return parsed
            except Exception:
                continue
    except Exception as e:
        logger.warning("Feishu wiki resolve failed: %s", e)
    return None


def _fetch_public_page_text(url: str) -> Optional[str]:
    try:
        import html
        import urllib.request
        req = urllib.request.Request(url, method='GET')
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode('utf-8', errors='ignore')
        if not raw:
            return None
        if any(k in raw for k in ["扫码登录", "切换至Lark登录", "还没有账号", "先进团队 先用飞书"]):
            return None
        text = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.IGNORECASE)
        text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 80:
            return None
        return text
    except Exception:
        return None


def extract_document_id_from_url(url: str) -> Optional[Tuple[str, str]]:
    url = (url or '').strip()
    if not url:
        return None
    m = FEISHU_DOCX_PATTERN.search(url)
    if m:
        return m.group(1), 'docx'
    m = FEISHU_DOC_PATTERN.search(url)
    if m:
        return m.group(1), 'doc'
    m = FEISHU_WIKI_PATTERN.search(url)
    if m:
        return m.group(1), 'wiki'
    return None


def is_feishu_doc_url(text: str) -> bool:
    line = (text or '').strip()
    if '\n' in line:
        return False
    return extract_document_id_from_url(line) is not None


def fetch_feishu_doc_content(url_or_content: str) -> Tuple[bool, str]:
    url = (url_or_content or '').strip()
    if not url or '\n' in url:
        return False, url_or_content or ''

    parsed = extract_document_id_from_url(url)
    if not parsed:
        return False, url_or_content

    doc_id, doc_type = parsed
    app_id, app_secret = _load_feishu_config()
    if not app_id or not app_secret:
        public_text = _fetch_public_page_text(url)
        if public_text:
            return True, public_text
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'feishu_config.json')
        return False, (
            f'未配置飞书应用。请设置 FEISHU_APP_ID / FEISHU_APP_SECRET，或创建配置文件：{cfg_path}。'
            '飞书 app_id / app_secret 可在飞书开放平台 -> 自建应用 -> 凭证与基础信息 中获取。'
        )

    token = _get_tenant_access_token(app_id, app_secret)
    if not token:
        return False, '获取飞书访问令牌失败，请检查应用凭证与网络'

    if doc_type == 'wiki':
        node = _resolve_wiki_node(doc_id, token)
        content = None
        if node:
            obj_token, obj_type = node
            if obj_type == "docx":
                content = _fetch_docx_raw_content(obj_token, token) or _fetch_doc_markdown_content(obj_token, token)
            elif obj_type == "doc":
                content = _fetch_doc_raw_content(obj_token, token)
        if content is None:
            content = _fetch_docx_raw_content(doc_id, token) or _fetch_doc_markdown_content(doc_id, token)
    elif doc_type == 'docx':
        content = _fetch_docx_raw_content(doc_id, token)
    else:
        content = _fetch_doc_raw_content(doc_id, token)

    if content is None:
        return False, '无法读取该飞书文档/知识库页面，请检查链接、应用权限及文档可见性'
    return True, (content or '').strip()
