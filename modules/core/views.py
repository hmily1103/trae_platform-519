"""平台级（跨模块）设置：大模型配置单一真源。

所有业务模块（test_case / prd_audit / log_monitor / precision_test ...）的 LLM 配置
统一读写 config/llm_config.json，本蓝图提供全局唯一的配置读写端点与设置页，
消除各模块重复维护 /api/llm_config 的散点实现。

安全策略（与 utils.llm_client._resolve_api_key 对齐）：
- 若环境变量已提供密钥（LLM_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY），
  则接口不落盘明文 Key，仅保留文件已有值；
- 否则允许本地快速写入（历史行为），明文 Key 仅存在于本地配置文件。
"""
import os
import json

from flask import Blueprint, render_template, request

from utils.response import success_response, error_response
from utils.logger import platform_logger

core_bp = Blueprint('core', __name__, template_folder='templates')

CONFIG_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'config', 'llm_config.json')
)


@core_bp.route('/llm_settings')
def llm_settings_page():
    """平台级大模型设置页"""
    return render_template('llm_settings.html')


@core_bp.route('/api/llm_config', methods=['GET'])
def api_get_llm_config():
    """获取平台级 LLM 配置（config/llm_config.json），Key 脱敏返回"""
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                config = json.load(f)

            profiles = config.get('profiles') if isinstance(config.get('profiles'), dict) else {}
            default_profile = (config.get('default_profile') or config.get('llm_provider') or 'deepseek').strip()

            active = profiles.get(default_profile) if isinstance(profiles.get(default_profile), dict) else None
            if not active and isinstance(config.get('llm_provider'), str):
                active = {
                    'llm_provider': config.get('llm_provider'),
                    'base_url': config.get('base_url'),
                    'api_key': config.get('api_key'),
                    'model': config.get('model'),
                }
            if active:
                for k in ['llm_provider', 'base_url', 'api_key', 'model']:
                    if k in active and active.get(k) is not None:
                        config[k] = active.get(k)

            config['default_profile'] = default_profile
            config['profiles_meta'] = [
                {
                    'key': k,
                    'llm_provider': (v.get('llm_provider') or k),
                    'model': (v.get('model') or ''),
                    'base_url': (v.get('base_url') or ''),
                }
                for k, v in profiles.items()
                if isinstance(v, dict)
            ]
            if not config['profiles_meta'] and active:
                config['profiles_meta'] = [
                    {
                        'key': default_profile,
                        'llm_provider': (active.get('llm_provider') or default_profile),
                        'model': (active.get('model') or ''),
                        'base_url': (active.get('base_url') or ''),
                    }
                ]

            # 脱敏 API Key（不在前端暴露明文）
            if config.get('api_key'):
                config['api_key'] = config['api_key'][:3] + '****' + config['api_key'][-4:]
            if config.get('fallback_api_key'):
                config['fallback_api_key'] = config['fallback_api_key'][:3] + '****' + config['fallback_api_key'][-4:]
            # 其他敏感 token 脱敏（与 prd_audit 历史端点对齐）
            for _tk in ('case_system_push_token', 'feishu_event_verify_token'):
                if config.get(_tk):
                    _t = str(config[_tk])
                    config[_tk] = (_t[:3] + '****' + _t[-4:]) if len(_t) >= 8 else '****'

            return success_response(data=config)
        return success_response(data={})
    except Exception as e:
        platform_logger.exception('获取 LLM 配置失败')
        return error_response(str(e), status_code=500)


@core_bp.route('/api/llm_config', methods=['POST'])
def api_save_llm_config():
    """保存平台级 LLM 配置（写入 config/llm_config.json）"""
    try:
        data = request.get_json() or {}

        # 安全与可用性折中：环境变量已提供密钥时不落盘明文 Key
        has_env_key = any(
            (os.environ.get(k) or '').strip()
            for k in ('LLM_API_KEY', 'DEEPSEEK_API_KEY', 'OPENAI_API_KEY', 'GEMINI_API_KEY')
        )
        allow_plain = not has_env_key

        old_config = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                    old_config = json.load(f)
            except Exception:
                pass

        new_config = old_config.copy()
        profiles = new_config.get('profiles') if isinstance(new_config.get('profiles'), dict) else {}
        if not profiles and isinstance(old_config.get('llm_provider'), str) and old_config.get('llm_provider'):
            k0 = old_config.get('llm_provider')
            profiles[k0] = {
                'llm_provider': old_config.get('llm_provider'),
                'base_url': old_config.get('base_url'),
                'api_key': old_config.get('api_key'),
                'model': old_config.get('model'),
            }

        llm_provider = (data.get('llm_provider') or new_config.get('llm_provider') or 'deepseek').strip()
        profile_key = llm_provider
        profile_old = profiles.get(profile_key) if isinstance(profiles.get(profile_key), dict) else {}
        profile_new = profile_old.copy()
        for k in ['llm_provider', 'base_url', 'model']:
            if k in data:
                profile_new[k] = data.get(k)

        api_key = (data.get('api_key') or '').strip()
        if allow_plain:
            if api_key and '****' not in api_key:
                profile_new['api_key'] = api_key
            elif 'api_key' in profile_old:
                profile_new['api_key'] = profile_old.get('api_key')
        else:
            if 'api_key' in profile_old:
                profile_new['api_key'] = profile_old.get('api_key', '')

        fallback_key = (data.get('fallback_api_key') or '').strip()
        if allow_plain:
            if fallback_key and '****' not in fallback_key:
                new_config['fallback_api_key'] = fallback_key
            elif 'fallback_api_key' in old_config:
                new_config['fallback_api_key'] = old_config['fallback_api_key']
        else:
            if 'fallback_api_key' in old_config:
                new_config['fallback_api_key'] = old_config.get('fallback_api_key', '')

        profiles[profile_key] = profile_new
        new_config['profiles'] = profiles
        new_config['default_profile'] = profile_key

        for k in ['llm_provider', 'base_url', 'model', 'api_key']:
            if k in profile_new and profile_new.get(k) is not None:
                new_config[k] = profile_new.get(k)

        # 全平台共用一个 API Key：同步到所有 profile，切换 provider 时无需重复填 Key
        if allow_plain:
            canonical_key = (new_config.get('api_key') or '').strip()
            if canonical_key:
                for pk in list(profiles.keys()):
                    pd = profiles.get(pk)
                    if isinstance(pd, dict):
                        profiles[pk] = {**pd, 'api_key': canonical_key}
                new_config['profiles'] = profiles

        for k in ['fallback_enabled', 'fallback_provider', 'fallback_base_url', 'fallback_model']:
            if k in data:
                new_config[k] = data.get(k)

        # 通用字段透传：除已显式处理的 profiles/key/fallback 字段外，
        # payload 中的其他顶层字段（如 feishu_*/case_system_* 等模块专属配置）
        # 直接合并进 config；值含掩码(****)时保留旧值，避免误清空真实密钥。
        _handled = {
            'llm_provider', 'base_url', 'model', 'api_key', 'fallback_api_key',
            'fallback_enabled', 'fallback_provider', 'fallback_base_url', 'fallback_model',
        }
        for _k, _v in data.items():
            if _k in _handled:
                continue
            if isinstance(_v, str) and '****' in _v and _k in old_config:
                new_config[_k] = old_config[_k]
            else:
                new_config[_k] = _v

        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(new_config, f, ensure_ascii=False, indent=2)

        if allow_plain:
            return success_response(message='配置已保存')
        return success_response(message='配置已保存（检测到环境变量已配置密钥，本次未落盘覆盖 API Key）')
    except Exception as e:
        platform_logger.exception('保存 LLM 配置失败')
        return error_response(str(e), status_code=500)
