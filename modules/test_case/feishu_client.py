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
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# 飞书文档 URL 正则：支持 docx 与旧版 doc
FEISHU_DOCX_PATTERN = re.compile(
    r'https?://[^/]+\.feishu\.cn/docx/([A-Za-z0-9]+)',
    re.IGNORECASE
)
FEISHU_DOC_PATTERN = re.compile(
    r'https?://[^/]+\.feishu\.cn/docs/([A-Za-z0-9]+)',
    re.IGNORECASE
)

# 内存缓存 tenant_access_token，避免频繁请求
_token_cache = {}
_token_cache_ttl = 0
TOKEN_CACHE_SECONDS = 7000  # 约 2 小时，飞书 token 一般 2 小时有效


def _load_feishu_config() -> Tuple[Optional[str], Optional[str]]:
    """从环境变量或 feishu_config.json 读取 app_id, app_secret"""
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
    """获取 tenant_access_token，带简单内存缓存"""
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
    """新版 docx 文档：GET raw_content"""
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
    """旧版 doc 文档：GET raw_content"""
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


def extract_document_id_from_url(url: str) -> Optional[Tuple[str, str]]:
    """
    从飞书文档 URL 解析出 (document_id, type)。
    type 为 'docx' 或 'doc'。
    """
    url = (url or '').strip()
    if not url:
        return None
    m = FEISHU_DOCX_PATTERN.search(url)
    if m:
        return m.group(1), 'docx'
    m = FEISHU_DOC_PATTERN.search(url)
    if m:
        return m.group(1), 'doc'
    return None


def is_feishu_doc_url(text: str) -> bool:
    """判断是否为飞书文档链接（单行）"""
    line = (text or '').strip()
    if '\n' in line:
        return False
    return extract_document_id_from_url(line) is not None


def fetch_feishu_doc_content(url_or_content: str) -> Tuple[bool, str]:
    """
    若输入为飞书文档链接则拉取正文并返回 (True, 正文)；
    否则返回 (False, 原字符串)。
    拉取失败时返回 (False, 错误信息) 供上层提示用户。
    """
    url = (url_or_content or '').strip()
    if not url or '\n' in url:
        return False, url_or_content or ''

    parsed = extract_document_id_from_url(url)
    if not parsed:
        return False, url_or_content

    doc_id, doc_type = parsed
    app_id, app_secret = _load_feishu_config()
    if not app_id or not app_secret:
        return False, '未配置飞书应用（FEISHU_APP_ID / FEISHU_APP_SECRET 或 feishu_config.json）'

    token = _get_tenant_access_token(app_id, app_secret)
    if not token:
        return False, '获取飞书访问令牌失败，请检查应用凭证与网络'

    if doc_type == 'docx':
        content = _fetch_docx_raw_content(doc_id, token)
    else:
        content = _fetch_doc_raw_content(doc_id, token)

    if content is None:
        return False, '无法读取该飞书文档，请检查链接与应用权限（文档是否对应用可见）'
    return True, (content or '').strip()
