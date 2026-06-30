from urllib.parse import urlparse


def is_ipm_atende(url):
    """True se a URL pertence a um portal IPM Atende.Net (host atende.net).

    Detecta pelo host (nao por substring no path/query) para evitar falso
    positivo. Entradas invalidas/None retornam False sem lancar.
    """
    if not url or not isinstance(url, str):
        return False
    try:
        host = (urlparse(url).hostname or '').lower()
    except (ValueError, TypeError):
        return False
    return host == 'atende.net' or host.endswith('.atende.net')


SITES_CERTIDOES = {
    'FEDERAL': {
        'url': 'https://servicos.receitafederal.gov.br/servico/certidoes/#/home/cnpj',
        'cnpj_field_id': 'input[id^="id"][name="niContribuinte"]',
        'by': 'css_selector'
    },
    'FGTS': {
        'url': 'https://consulta-crf.caixa.gov.br/consultacrf/pages/consultaEmpregador.jsf',
        'cnpj_field_id': 'mainForm:txtInscricao1',
        'by': 'id',
        # passos executados depois de preencher o CNPJ
        'steps_after_cnpj': ['fgts_emitir_pdf']
    },
    
    'ESTADUAL': {
        'RS': {
            'url': 'https://www.sefaz.rs.gov.br/sat/CertidaoSitFiscalSolic.aspx',
            'login_cert_url': 'https://www.sefaz.rs.gov.br/Login/LoginEcacCert.aspx',
            'cnpj_field_id': 'campoCnpj',
            'by': 'name'
        },
         'SP': {
            'url': 'https://www10.fazenda.sp.gov.br/CertidaoNegativaDeb/Pages/EmissaoCertidaoNegativa.aspx',
            'pre_fill_click_id': "input[value='cnpjradio']",
            'pre_fill_click_by': 'css_selector',
            'cnpj_field_id': 'MainContent_txtDocumento',
            'by': 'id'
        },
        'MT': {
            'url': 'https://www.sefaz.mt.gov.br/cnd/certidao/servlet/ServletRotdAberto?origem=60',
            'pre_fill_click_id': "input[value='CNPJ']",
            'pre_fill_click_by': 'css_selector',
            'cnpj_field_id': 'numeroDocumento',
            'by': 'name',
            'slow_typing': True
        },
        'MS': {
            'url': 'https://servicos.efazenda.ms.gov.br/pndfis/home/emissao',
            'cnpj_field_id':'Numero',
            'by':'id',
            'tipo_select_id': 'Tipo',
            'tipo_select_by': 'id',
            'tipo_select_value': 'CNPJ'
        }
    },
    
    'TRABALHISTA': {
        'url': 'https://cndt-certidao.tst.jus.br/inicio.faces',
        'pre_fill_click_id': "input[value='Emitir Certidão']",
        'pre_fill_click_by': 'css_selector',                  
        'cnpj_field_id': 'gerarCertidaoForm:cpfCnpj',
        'by': 'id'
    }
}


VALIDADES_CERTIDOES = {
    # federal nao tem automatizacao - deixar aqui para futuro
    'FEDERAL': {
        'validade_dias_padrao': None
    },
    'FGTS': {
        # atualmente sistema usa scraping para pegar validade
        'validade_dias_padrao': None
    },
    'TRABALHISTA': {
        'validade_dias_padrao': 180
    },
    'ESTADUAL': {
        'RS': {
            'validade_dias_padrao': 59
        },
        'SP': {
            'validade_dias_padrao': 180
        },
        'MT': {
            'validade_dias_padrao': 59
        },
        'MS': {
            'validade_dias_padrao': 60
        }
    }
}