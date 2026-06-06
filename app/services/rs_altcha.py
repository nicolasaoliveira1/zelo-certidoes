import json
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from app.captcha_solver import (AltchaSolverConfigError, AltchaSolverRuntimeError,
                                solve_altcha)
from app.utils import to_bool as _to_bool


def _normalizar_json_altcha(raw_value):
    if isinstance(raw_value, (dict, list)):
        return json.dumps(raw_value, separators=(',', ':'))

    if not raw_value:
        return None

    texto = str(raw_value).strip()
    if not texto:
        return None

    texto = texto.replace('&quot;', '"').replace('&#34;', '"')
    try:
        parsed = json.loads(texto)
    except Exception:
        return texto
    return json.dumps(parsed, separators=(',', ':'))


def _coletar_contexto_altcha(driver):
    script = """
        const widgets = Array.from(document.querySelectorAll('altcha-widget'));
        const widget = widgets.length ? widgets[0] : null;

        function firstNonEmpty(values) {
            for (const value of values) {
                if (value && String(value).trim()) {
                    return String(value).trim();
                }
            }
            return null;
        }

        let challengeJson = null;
        let challengeUrl = null;

        if (widget) {
            challengeJson = firstNonEmpty([
                widget.getAttribute('challengejson'),
                widget.getAttribute('challenge_json'),
                widget.getAttribute('data-challenge-json'),
                widget.getAttribute('data-challengejson'),
                widget.getAttribute('challenge')
            ]);

            challengeUrl = firstNonEmpty([
                widget.getAttribute('challengeurl'),
                widget.getAttribute('challenge_url'),
                widget.getAttribute('data-challenge-url'),
                widget.getAttribute('data-challengeurl')
            ]);
        }

        if (!challengeJson && !challengeUrl) {
            const host = document.querySelector('[data-challenge-url], [data-challenge-json], [data-altcha]');
            if (host) {
                challengeJson = firstNonEmpty([
                    host.getAttribute('data-challenge-json'),
                    host.getAttribute('data-challengejson'),
                    host.getAttribute('data-challenge')
                ]);
                challengeUrl = firstNonEmpty([
                    host.getAttribute('data-challenge-url'),
                    host.getAttribute('data-challengeurl')
                ]);
            }
        }

        return {
            hasWidget: widgets.length > 0,
            widgetCount: widgets.length,
            challengeJson,
            challengeUrl,
            currentUrl: window.location.href
        };
    """
    try:
        result = driver.execute_script(script) or {}
    except Exception:
        return {
            'hasWidget': False,
            'widgetCount': 0,
            'challengeJson': None,
            'challengeUrl': None,
            'currentUrl': None,
        }

    return {
        'hasWidget': bool(result.get('hasWidget')),
        'widgetCount': int(result.get('widgetCount') or 0),
        'challengeJson': result.get('challengeJson'),
        'challengeUrl': result.get('challengeUrl'),
        'currentUrl': result.get('currentUrl'),
    }


def _injetar_resposta_altcha(driver, token):
    script = """
        const token = arguments[0];
        if (!token) {
            return { updated: 0, details: [], checked: 0, created: 0, verified: 0 };
        }

        const selectors = [
            "input[name='altcha']",
            "input[name='altcha-token']",
            "input[name='altchaToken']",
            "textarea[name='altcha']",
            "input[type='hidden'][id*='altcha' i]",
            "input[type='hidden'][name*='altcha' i]",
            "textarea[id*='altcha' i]",
            "textarea[name*='altcha' i]"
        ];

        const touched = new Set();
        const details = [];
        let checked = 0;
        let created = 0;
        let verified = 0;

        for (const selector of selectors) {
            const elements = Array.from(document.querySelectorAll(selector));
            for (const el of elements) {
                if (touched.has(el)) {
                    continue;
                }
                touched.add(el);
                el.value = token;
                el.setAttribute('value', token);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                details.push(selector);
            }
        }

        const widgets = Array.from(document.querySelectorAll('altcha-widget'));
        widgets.forEach(widget => {
            widget.setAttribute('solution', token);
            widget.setAttribute('data-solution', token);

            if ('solution' in widget) {
                try {
                    widget.solution = token;
                } catch (e) {
                    // noop
                }
            }

            const stateHost = widget.querySelector('.altcha');
            if (stateHost) {
                stateHost.setAttribute('data-state', 'verified');
                verified += 1;
            }

            const checkbox = widget.querySelector("input[type='checkbox']");
            if (checkbox) {
                checkbox.checked = true;
                checkbox.removeAttribute('required');
                checkbox.dispatchEvent(new Event('input', { bubbles: true }));
                checkbox.dispatchEvent(new Event('change', { bubbles: true }));
                checked += 1;
            }

            const form = widget.closest('form');
            if (form) {
                let hidden = form.querySelector("input[type='hidden'][name='altcha']");
                if (!hidden) {
                    hidden = document.createElement('input');
                    hidden.type = 'hidden';
                    hidden.name = 'altcha';
                    form.appendChild(hidden);
                    created += 1;
                }

                hidden.value = token;
                hidden.setAttribute('value', token);
                hidden.dispatchEvent(new Event('input', { bubbles: true }));
                hidden.dispatchEvent(new Event('change', { bubbles: true }));

                form.dispatchEvent(new Event('input', { bubbles: true }));
                form.dispatchEvent(new Event('change', { bubbles: true }));
            }

            widget.dispatchEvent(new CustomEvent('altcha-solved', {
                bubbles: true,
                detail: { token }
            }));
            widget.dispatchEvent(new Event('input', { bubbles: true }));
            widget.dispatchEvent(new Event('change', { bubbles: true }));
        });

        window.__certidoes_altcha_token = token;

        return {
            updated: touched.size,
            widgetCount: widgets.length,
            details,
            checked,
            created,
            verified
        };
    """
    try:
        return driver.execute_script(script, token) or {}
    except Exception:
        return {
            'updated': 0,
            'widgetCount': 0,
            'details': [],
            'checked': 0,
            'created': 0,
            'verified': 0
        }


def resolver_altcha_rs_com_2captcha(driver, config, allow_solver=False):
    if not allow_solver:
        return {
            'attempted': False,
            'status': 'disabled',
            'enabled_raw': 'only_in_batch'
        }

    valor_raw = config.get('RS_ALTCHA_AUTOSOLVE_ENABLED', False)
    flag_enabled = _to_bool(valor_raw, False)

    if not flag_enabled:
        return {
            'attempted': False,
            'status': 'disabled',
            'enabled_raw': str(valor_raw)
        }

    contexto = _coletar_contexto_altcha(driver)
    if not contexto.get('hasWidget'):
        return {
            'attempted': False,
            'status': 'not_found',
            'widget_count': contexto.get('widgetCount', 0)
        }

    challenge_json = _normalizar_json_altcha(contexto.get('challengeJson'))
    challenge_url = (contexto.get('challengeUrl') or '').strip() or None

    if not challenge_json and not challenge_url:
        return {
            'attempted': True,
            'status': 'missing_challenge',
            'widget_count': contexto.get('widgetCount', 0)
        }

    try:
        solved = solve_altcha(
            config,
            page_url=contexto.get('currentUrl') or driver.current_url,
            challenge_json=challenge_json,
            challenge_url=challenge_url
        )
    except AltchaSolverConfigError as exc:
        return {
            'attempted': True,
            'status': 'config_error',
            'message': str(exc),
            'widget_count': contexto.get('widgetCount', 0)
        }
    except AltchaSolverRuntimeError as exc:
        return {
            'attempted': True,
            'status': 'solve_error',
            'message': str(exc),
            'widget_count': contexto.get('widgetCount', 0)
        }

    token = solved.get('code')
    injecao = _injetar_resposta_altcha(driver, token)

    return {
        'attempted': True,
        'status': 'solved',
        'widget_count': contexto.get('widgetCount', 0),
        'injected_fields': int(injecao.get('updated') or 0),
        'injected_widgets': int(injecao.get('widgetCount') or 0),
        'checked_boxes': int(injecao.get('checked') or 0),
        'created_hidden_fields': int(injecao.get('created') or 0),
        'verified_states': int(injecao.get('verified') or 0)
    }


def clicar_enviar_estadual_rs(driver, timeout=8, retries=4, post_wait=0.5):
    metodos_tentados = []

    for tentativa in range(1, max(1, int(retries)) + 1):
        if tentativa > 1:
            time.sleep(0.7)

        try:
            btn = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((By.ID, 'btnEnviar'))
            )
            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            except Exception:
                pass

            try:
                WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.ID, 'btnEnviar')))
                btn.click()
                time.sleep(post_wait)
                return {
                    'clicked': True,
                    'method': 'selenium_click',
                    'attempt': tentativa
                }
            except Exception:
                metodos_tentados.append('selenium_click')

            try:
                driver.execute_script("arguments[0].click();", btn)
                time.sleep(post_wait)
                return {
                    'clicked': True,
                    'method': 'js_click',
                    'attempt': tentativa
                }
            except Exception:
                metodos_tentados.append('js_click')
        except Exception:
            metodos_tentados.append('find_btn')

        try:
            executou = driver.execute_script(
                "if (typeof EnviarSolCer === 'function') { EnviarSolCer(); return true; } return false;"
            )
            if executou:
                time.sleep(post_wait)
                return {
                    'clicked': True,
                    'method': 'js_function',
                    'attempt': tentativa
                }
        except Exception:
            metodos_tentados.append('js_function')

    return {
        'clicked': False,
        'method': 'none',
        'attempt': max(1, int(retries)),
        'tried': ','.join(metodos_tentados[-8:])
    }
