"""Testes para as otimizações de performance do dashboard (perf/dashboard-filters).

Cobre:
- _get_config_cached: caching por contexto de app (flask.g)
- Rota GET / (dashboard): renderiza sem erro, filtro de cidade funciona
- Rota GET /certidao/<id>/token-visualizar: lazy token
"""
import os
import re
import tempfile
from datetime import date, timedelta

import pytest

from app import db
from app.models import (
    Certidao,
    ConfiguracaoSistema,
    Empresa,
    StatusEspecial,
    TipoCertidao,
    _get_config_cached,
    get_a_vencer_dias,
)


# ---------------------------------------------------------------------------
# Fixtures auxiliares
# ---------------------------------------------------------------------------

@pytest.fixture()
def empresa_sp(app, ids):
    """Segunda empresa em São Paulo para testar filtro de cidade."""
    with app.app_context():
        emp = Empresa(nome='Empresa SP', cnpj='33.333.333/3333-33',
                      estado='SP', cidade='São Paulo')
        db.session.add(emp)
        db.session.flush()
        db.session.add(Certidao(tipo=TipoCertidao.FEDERAL, empresa=emp))
        db.session.commit()
        yield emp.id
        # conftest já faz drop_all, mas remove explicitamente para não poluir outros testes
        db.session.delete(Empresa.query.get(emp.id))
        db.session.commit()


# ---------------------------------------------------------------------------
# _get_config_cached
# ---------------------------------------------------------------------------

class TestGetConfigCached:
    def test_retorna_none_sem_config_no_banco(self, app):
        """Sem ConfiguracaoSistema semeada, retorna None sem lançar exceção."""
        with app.app_context():
            resultado = _get_config_cached()
            assert resultado is None

    def test_retorna_config_quando_existe(self, app, ids):
        # ids garante que db.create_all() foi chamado
        with app.app_context():
            config = ConfiguracaoSistema(id=1, a_vencer_dias=10)
            db.session.add(config)
            db.session.commit()
            resultado = _get_config_cached()
            assert resultado is not None
            assert resultado.a_vencer_dias == 10
            db.session.delete(config)
            db.session.commit()

    def test_mesmo_objeto_em_request_context(self, app, client, ids):
        """Dentro de um request, _get_config_cached deve retornar o mesmo objeto."""
        with app.test_request_context('/'):
            with app.app_context():
                r1 = _get_config_cached()
                r2 = _get_config_cached()
                assert r1 is r2

    def test_get_a_vencer_dias_usa_default_sem_config(self, app):
        with app.app_context():
            assert get_a_vencer_dias() == 7

    def test_get_a_vencer_dias_retorna_valor_configurado(self, app, ids):
        with app.app_context():
            config = ConfiguracaoSistema(id=1, a_vencer_dias=14)
            db.session.add(config)
            db.session.commit()
            assert get_a_vencer_dias() == 14
            db.session.delete(config)
            db.session.commit()


# ---------------------------------------------------------------------------
# Rota dashboard GET /
# ---------------------------------------------------------------------------

class TestDashboard:
    def test_dashboard_renderiza_ok(self, client, ids):
        r = client.get('/')
        assert r.status_code == 200
        assert b'Empresa Teste' in r.data

    def test_dashboard_filtro_cidade_existente(self, client, ids):
        r = client.get('/?cidade=Tramandai')
        assert r.status_code == 200
        assert b'Empresa Teste' in r.data

    def test_dashboard_cidade_deeplink_renderiza_todas_e_marca_chip(self, client, ids, empresa_sp):
        """Estado/cidade agora filtram no cliente: todas as empresas continuam no
        HTML (o filtro efetivo é no navegador) e o chip da cidade vem pré-marcado."""
        r = client.get('/?cidade=S%C3%A3o+Paulo')
        assert r.status_code == 200
        assert b'Empresa SP' in r.data
        assert b'Empresa Teste' in r.data  # não é mais removida no servidor
        # algum chip de cidade vem pré-marcado (deep-link)
        assert re.search(rb'id="cidade-[^"]+"[^>]*\bchecked\b', r.data)

    def test_dashboard_filtro_cidade_inexistente_nao_quebra(self, client, ids):
        """Cidade inexistente: sem chip para pré-marcar, a página renderiza tudo."""
        r = client.get('/?cidade=CidadeQueNaoExiste')
        assert r.status_code == 200
        assert b'Empresa Teste' in r.data

    def test_dashboard_estado_deeplink_renderiza_todas_e_marca_chip(self, client, ids, empresa_sp):
        r = client.get('/?estado=SP')
        assert r.status_code == 200
        assert b'Empresa SP' in r.data
        assert b'Empresa Teste' in r.data  # filtro é client-side, não remove no servidor
        # chip do estado SP pré-marcado
        assert re.search(rb'id="estado-SP"[^>]*\bchecked\b', r.data)

    def test_dashboard_cidade_variantes_agrupadas_em_um_chip(self, app, client, ids):
        """'Imbé' e 'IMBE' (acento/caixa) devem gerar um único chip de cidade,
        e cada card recebe a mesma data-cidade-key (agrupamento canônico)."""
        with app.app_context():
            for nome, cid in [('Imbe Um', 'Imbé'), ('Imbe Dois', 'IMBE')]:
                emp = Empresa(nome=nome, cnpj=f'44.444.444/444{len(nome)}-44',
                              estado='RS', cidade=cid)
                db.session.add(emp)
                db.session.flush()
                db.session.add(Certidao(tipo=TipoCertidao.MUNICIPAL, empresa=emp))
            db.session.commit()
        try:
            r = client.get('/')
            assert r.status_code == 200
            html = r.data.decode('utf-8')
            # ambas as empresas renderizam (filtro client-side)
            assert 'Imbe Um' in html and 'Imbe Dois' in html
            # um único chip-count de cidade para a variante Imbé
            chaves_imbe = set(re.findall(r'data-cidade="([^"]*imb[^"]*)"', html, re.IGNORECASE))
            assert len(chaves_imbe) == 1, chaves_imbe
            # os dois cards compartilham a mesma data-cidade-key
            keys_cards = set(re.findall(r'company-card[^>]*data-cidade-key="([^"]*imb[^"]*)"',
                                        html, re.IGNORECASE))
            assert keys_cards == chaves_imbe
        finally:
            with app.app_context():
                for emp in Empresa.query.filter(Empresa.nome.in_(['Imbe Um', 'Imbe Dois'])).all():
                    db.session.delete(emp)
                db.session.commit()

    def test_dashboard_data_attributes_presentes(self, client, ids):
        """Os data-* de contadores devem aparecer no HTML."""
        r = client.get('/')
        assert b'data-count-total=' in r.data
        assert b'data-menor-validade=' in r.data
        assert b'data-status-cert=' in r.data

    def test_dashboard_certidao_status_cert_pendente(self, app, client, ids):
        """Certidão PENDENTE deve aparecer com data-status-cert='pendentes'."""
        with app.app_context():
            cert = Certidao.query.get(ids['trabalhista'])
            cert.status_especial = StatusEspecial.PENDENTE
            db.session.commit()
        r = client.get('/')
        assert b"data-status-cert=\"pendentes\"" in r.data
        with app.app_context():
            cert = Certidao.query.get(ids['trabalhista'])
            cert.status_especial = None
            db.session.commit()

    def test_dashboard_certidao_vencida(self, app, client, ids):
        with app.app_context():
            cert = Certidao.query.get(ids['trabalhista'])
            cert.data_validade = date.today() - timedelta(days=1)
            db.session.commit()
        r = client.get('/')
        assert b"data-status-cert=\"vencidas\"" in r.data
        with app.app_context():
            cert = Certidao.query.get(ids['trabalhista'])
            cert.data_validade = None
            db.session.commit()


# ---------------------------------------------------------------------------
# Rota GET /certidao/<id>/token-visualizar
# ---------------------------------------------------------------------------

class TestGerarTokenVisualizar:
    def test_certidao_inexistente_retorna_404(self, client, ids):
        r = client.get('/certidao/99999/token-visualizar')
        assert r.status_code == 404

    def test_sem_arquivo_retorna_404(self, client, ids):
        """Certidão sem caminho_arquivo e sem arquivo em disco retorna 404."""
        r = client.get(f'/certidao/{ids["trabalhista"]}/token-visualizar')
        assert r.status_code == 404
        assert r.get_json()['erro'] == 'sem_arquivo'

    def test_com_arquivo_retorna_url(self, app, client, ids):
        """Com caminho_arquivo válido retorna JSON com 'url'."""
        fd, caminho = tempfile.mkstemp(suffix='.pdf')
        os.close(fd)
        try:
            with app.app_context():
                cert = Certidao.query.get(ids['trabalhista'])
                cert.caminho_arquivo = caminho
                db.session.commit()
            r = client.get(f'/certidao/{ids["trabalhista"]}/token-visualizar')
            assert r.status_code == 200
            data = r.get_json()
            assert 'url' in data
            assert '/certidao/visualizar/' in data['url']
        finally:
            os.unlink(caminho)
            with app.app_context():
                cert = Certidao.query.get(ids['trabalhista'])
                cert.caminho_arquivo = None
                db.session.commit()

    def test_url_retornada_e_acessivel(self, app, client, ids):
        """A URL retornada pelo token deve ser acessível (200) enquanto o arquivo existir."""
        fd, caminho = tempfile.mkstemp(suffix='.pdf')
        os.write(fd, b'%PDF-1.4 fake')
        os.close(fd)
        try:
            with app.app_context():
                cert = Certidao.query.get(ids['trabalhista'])
                cert.caminho_arquivo = caminho
                db.session.commit()
            r_token = client.get(f'/certidao/{ids["trabalhista"]}/token-visualizar')
            assert r_token.status_code == 200
            url = r_token.get_json()['url']
            r_pdf = client.get(url)
            assert r_pdf.status_code == 200
        finally:
            with app.app_context():
                cert = Certidao.query.get(ids['trabalhista'])
                cert.caminho_arquivo = None
                db.session.commit()
            # Windows pode manter o arquivo aberto após send_file; ignora erro de deleção
            try:
                os.unlink(caminho)
            except OSError:
                pass
