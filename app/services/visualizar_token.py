"""Tokens assinados para visualizacao publica de certidao.

Fonte unica dos helpers de token (serializer + geracao + carga), compartilhada
entre as rotas (blueprint `main`) e a camada de servico de emissao
(`emissao_service`, que devolve o token na resposta de download). Isolado aqui
para evitar dependencia circular rota <-> servico.
"""
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from flask import current_app


def _get_visualizar_serializer():
    secret = current_app.config.get('SECRET_KEY') or 'certidoes-secret'
    return URLSafeTimedSerializer(secret, salt='visualizar-certidao')


def _gerar_visualizar_token(certidao_id):
    return _get_visualizar_serializer().dumps({'cid': certidao_id})


def _carregar_visualizar_token(token, max_age=60 * 60 * 24):
    try:
        data = _get_visualizar_serializer().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    return data.get('cid') if isinstance(data, dict) else None
