"""Model speed preferences, independent of model and reasoning selections."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import tempfile

from fastapi import Request
from fastapi.responses import JSONResponse


def configurable(provider, model_id):
    model = str(model_id).lower().rsplit('/', 1)[-1]
    return bool(provider and provider.protocol in {'openai', 'openai-compat'}
                and model.startswith(('gpt-', 'codex-')))


class FastModeSettings:
    def __init__(self, path, registry):
        self.path, self.registry = Path(path), registry
        self.values = {}
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            for row in saved.get('models', []):
                if (isinstance(row.get('provider_key'), str) and isinstance(row.get('model_id'), str)
                        and row.get('service_tier') in {'fast', 'default'}):
                    self.values[row['provider_key'], row['model_id']] = row['service_tier']
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            logging.warning('Fast mode preferences could not be loaded')
        registry.configure_service_tiers(self.values)

    def state(self, provider_key, model_id, *, provider=None, client=None):
        if provider is None:
            record = self.registry.get(provider_key)
            provider = record.provider if record else None
        tier = self.registry.service_tier_for(provider_key, model_id,
            getattr(client, 'service_tier', None) or getattr(provider, 'service_tier', ''))
        return dict(provider_key=provider_key, model_id=model_id, configurable=configurable(provider, model_id),
                    enabled=tier in {'fast', 'priority'}, service_tier=tier or 'auto')

    def status(self, slot, client=None):
        value = self.state(slot.key if slot else '', slot.provider.model if slot else '',
                           provider=slot.provider if slot else None, client=client)
        return dict(fast_mode=value['enabled'], fast_mode_available=value['configurable'],
                    service_tier=value['service_tier'])

    def change(self, provider_key, model_id, enabled):
        if type(enabled) is not bool:
            raise ValueError('Fast 模式需要明确的开启或关闭选项')
        if not self.state(provider_key, model_id)['configurable']:
            raise ValueError('当前模型没有 Fast 设置')
        values = dict(self.values)
        values[provider_key, model_id] = 'fast' if enabled else 'default'
        payload = dict(models=[dict(provider_key=key, model_id=model, service_tier=tier)
                              for (key, model), tier in sorted(values.items())])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             delete=False) as stream:
                temporary = stream.name
                json.dump(payload, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
        self.values = values
        self.registry.configure_service_tiers(values)
        return self.state(provider_key, model_id)


def install(app, srv):
    @app.post('/api/settings/fast-mode')
    async def set_fast_mode(request: Request):
        try:
            origin = request.headers.get('origin')
            if (request.headers.get('x-mew-fast-mode') != '1' or request.headers.get('sec-fetch-site') == 'cross-site'
                    or origin and origin != str(request.base_url).rstrip('/')):
                return JSONResponse({'error': '请在当前模型设置中操作'}, status_code=403)
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError('Fast 设置需要对象格式')
            slot = srv.SESSION.slot
            if (body.get('conversation_id') != srv._active_conversation_id() or not slot
                    or (body.get('provider_key'), body.get('model_id')) != (slot.key, slot.provider.model)):
                raise ValueError('对话或当前模型已变化，请重新打开模型设置')
            value = srv.FAST_MODE_SETTINGS.change(slot.key, slot.provider.model, body.get('enabled'))
            return dict(value, status=srv.SESSION.status_payload())
        except (ValueError, TypeError, OSError) as exc:
            return JSONResponse({'error': str(exc)}, status_code=409)
